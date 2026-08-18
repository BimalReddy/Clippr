import os
import re
import hashlib
import asyncio
import argparse
from pathlib import Path
from urllib.parse import urljoin, urlparse
import httpx
from bs4 import BeautifulSoup
import html2text

# Modern user-agent headers to bypass basic anti-bot / 403 blocks
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

class MarkdownVaultManager:
    def __init__(self, vault_dir="vault", max_concurrent_checks=15):
        self.vault_dir = Path(vault_dir)
        self.images_dir = self.vault_dir / "images"
        self.max_concurrent_checks = max_concurrent_checks
        
        # Ensure default directories exist
        self.vault_dir.mkdir(parents=True, exist_ok=True)
        self.images_dir.mkdir(parents=True, exist_ok=True)

    def sanitize_filename(self, text):
        """Removes illegal characters and trailing dots/spaces for safe saving."""
        text = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', text)
        text = re.sub(r'\s+', ' ', text).strip('. ')
        return text[:100] if text else "Untitled_Article"

    def clean_html(self, soup):
        """Strips out ads, navbars, scripts, footers, and interactive widgets."""
        for tag in soup(["nav", "footer", "aside", "script", "style", "noscript", "header", "form", "iframe"]):
            tag.decompose()
        
        # Isolate the main article body if present
        core_content = soup.find('article') or soup.find('main') or soup.body or soup
        return core_content

    def process_images(self, soup, base_url, client, target_images_dir):
        """Downloads images safely to the specified image directory."""
        for img in soup.find_all('img'):
            src = img.get('src')
            if not src or src.startswith("data:"):
                continue
                
            img_url = urljoin(base_url, src)
            
            # Generate deterministic filename using MD5 hash
            url_hash = hashlib.md5(img_url.encode('utf-8')).hexdigest()[:10]
            parsed_path = urlparse(img_url).path
            ext = os.path.splitext(parsed_path)[1].lower()
            if not ext or len(ext) > 5 or ext not in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg']:
                ext = '.png'
                
            filename = f"img_{url_hash}{ext}"
            local_image_path = target_images_dir / filename
            
            # POSIX relative path for Markdown standard compatibility
            relative_md_path = f"images/{filename}"

            # Download image if missing
            if not local_image_path.exists():
                try:
                    print(f"  [+] Downloading image: {filename}")
                    response = client.get(img_url, timeout=10.0, follow_redirects=True)
                    response.raise_for_status()
                    with open(local_image_path, 'wb') as f:
                        f.write(response.content)
                except Exception as e:
                    print(f"  [!] Failed to download {img_url}: {e}")
                    continue
            
            # Update HTML source tag before html2text conversion
            img['src'] = relative_md_path

    def clip_webpage(self, url, custom_output_path=None):
        """Fetches a URL, cleans content, downloads images, and saves Markdown."""
        print(f"Clipping: {url}")
        try:
            with httpx.Client(headers=DEFAULT_HEADERS, timeout=15.0, follow_redirects=True) as client:
                response = client.get(url)
                response.raise_for_status()
                
                soup = BeautifulSoup(response.text, 'html.parser')
                
                title_tag = soup.find('title')
                title = title_tag.text.strip() if title_tag else "Untitled Article"
                safe_title = self.sanitize_filename(title)
                
                # Determine exact file path and where images should go
                if custom_output_path:
                    file_path = Path(custom_output_path)
                    
                    # If user provided a directory path, append the auto-generated title
                    if file_path.is_dir() or custom_output_path.endswith(('/', '\\')):
                        file_path = file_path / f"{safe_title}.md"
                    # If user provided a file without an extension, add .md
                    elif not file_path.suffix:
                        file_path = file_path.with_suffix('.md')
                        
                    # Create parent directories for the custom path
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    
                    # Route images to an 'images' folder next to the custom markdown file
                    target_images_dir = file_path.parent / "images"
                    target_images_dir.mkdir(parents=True, exist_ok=True)
                else:
                    # Default vault behavior
                    file_path = self.vault_dir / f"{safe_title}.md"
                    target_images_dir = self.images_dir
                
                core_content = self.clean_html(soup)
                self.process_images(core_content, url, client, target_images_dir)
                
                # Configure Markdown Converter
                h2t = html2text.HTML2Text()
                h2t.ignore_links = False
                h2t.inline_links = True
                h2t.body_width = 0
                
                markdown_content = f"# {title}\n\n**Source:** {url}\n\n---\n\n"
                markdown_content += h2t.handle(str(core_content))
                
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(markdown_content)
                    
                print(f"Success! Saved to: {file_path}")
                
        except Exception as e:
            print(f"Error clipping {url}: {e}")

    # ... (Rest of the Link Checker code remains identical to the previous version) ...
    async def _check_single_url(self, client, semaphore, url, files):
        async with semaphore:
            try:
                response = await client.head(url, timeout=10.0, follow_redirects=True)
                if response.status_code in (405, 403, 400): 
                    response = await client.get(url, timeout=10.0, follow_redirects=True)
                if response.status_code >= 400:
                    return (url, response.status_code, files)
            except Exception as e:
                return (url, str(e), files)
            return (url, 200, files)

    async def _run_link_checker(self):
        link_pattern = re.compile(
            r'\[(?:[^\]\\]|\\.)*\]\(((?:[^\s()\\]|\\.|(?:\((?:[^\s()\\]|\\.)*\)))*)\)'
        )
        url_to_files = {}
        print(f"Scanning '{self.vault_dir}' for Markdown files...")
        for file_path in self.vault_dir.rglob('*.md'):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                    matches = link_pattern.findall(content)
                    for match in matches:
                        if match.startswith(('http://', 'https://')):
                            url_to_files.setdefault(match, set()).add(file_path.name)
            except Exception as e:
                print(f"Error reading {file_path}: {e}")

        if not url_to_files:
            print("No external HTTP/HTTPS links found to check.")
            return

        print(f"Found {len(url_to_files)} unique external link(s) across files. Checking...")
        semaphore = asyncio.Semaphore(self.max_concurrent_checks)
        async with httpx.AsyncClient(headers=DEFAULT_HEADERS, verify=False) as client:
            tasks = [
                self._check_single_url(client, semaphore, url, files) 
                for url, files in url_to_files.items()
            ]
            results = await asyncio.gather(*tasks)
            
        broken_links = [res for res in results if res[1] != 200]
        
        if not broken_links:
            print("Awesome! No broken links found in your vault.")
        else:
            print(f"Found {len(broken_links)} broken link(s):")
            print("=" * 60)
            for url, status, files in broken_links:
                file_list = ", ".join(sorted(files))
                print(f"URL:    {url}")
                print(f"Status: {status}")
                print(f"In:     {file_list}")
                print("-" * 60)

    def check_links(self):
        asyncio.run(self._run_link_checker())

def main():
    parser = argparse.ArgumentParser(description="Local Markdown Web Clipper & Broken Link Checker")
    parser.add_argument('action', choices=['clip', 'check'], help="Action: 'clip' a URL or 'check' vault for dead links.")
    parser.add_argument('--url', type=str, help="The URL to clip (Required if action is 'clip')")
    parser.add_argument('--vault', type=str, default="vault", help="Path to your markdown vault (Default: ./vault)")
    # NEW ARGUMENT HERE:
    parser.add_argument('--output', '-o', type=str, help="Specific file path or directory to save the clipped Markdown file.")
    
    args = parser.parse_args()
    
    manager = MarkdownVaultManager(vault_dir=args.vault)
    
    if args.action == 'clip':
        if not args.url:
            print("Error: --url is required when using the 'clip' action.")
            return
        # Pass the output argument to the clipping function
        manager.clip_webpage(args.url, custom_output_path=args.output)
        
    elif args.action == 'check':
        manager.check_links()

if __name__ == "__main__":
    main()
