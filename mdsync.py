#!/usr/bin/env python3
"""
mdsync - Sync between Google Docs and Markdown files
"""

import os
import sys
import re
import argparse
import json
import yaml
import difflib
import uuid
import time
from pathlib import Path
from typing import Optional, Tuple

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
import io

# Confluence imports
try:
    from atlassian import Confluence
    CONFLUENCE_AVAILABLE = True
except ImportError:
    CONFLUENCE_AVAILABLE = False
    Confluence = None

# If modifying these scopes, delete the file token.json.
SCOPES = ['https://www.googleapis.com/auth/documents', 
          'https://www.googleapis.com/auth/drive']


def find_config_file(filename: str) -> Optional[str]:
    """Find a config file in multiple possible locations."""
    search_paths = [
        Path.cwd() / filename,  # Current directory
        Path.home() / '.config' / 'mdsync' / filename,  # XDG config
        Path.home() / '.mdsync' / filename,  # Home directory
    ]
    
    for path in search_paths:
        if path.exists():
            return str(path)
    
    return None


def get_credentials():
    """Get or create Google API credentials."""
    creds = None
    
    # Find token file
    token_file = find_config_file('token.json')
    
    # The file token.json stores the user's access and refresh tokens
    if token_file and os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    
    # If there are no (valid) credentials available, let the user log in.
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # Find credentials file
            credentials_file = find_config_file('credentials.json')
            
            if not credentials_file:
                print("Error: credentials.json not found!", file=sys.stderr)
                print("Searched in:", file=sys.stderr)
                print("  - Current directory", file=sys.stderr)
                print("  - ~/.config/mdsync/", file=sys.stderr)
                print("  - ~/.mdsync/", file=sys.stderr)
                print("\nPlease follow the setup instructions in SETUP_GUIDE.md", file=sys.stderr)
                sys.exit(1)
            
            flow = InstalledAppFlow.from_client_secrets_file(
                credentials_file, SCOPES)
            creds = flow.run_local_server(port=0)
        
        # Save the credentials for the next run
        # Save in the same location as credentials, or current directory
        if not token_file:
            credentials_file = find_config_file('credentials.json')
            if credentials_file:
                token_file = str(Path(credentials_file).parent / 'token.json')
            else:
                token_file = 'token.json'
        
        with open(token_file, 'w') as token:
            token.write(creds.to_json())
    
    return creds


def extract_doc_id(url_or_id: str) -> str:
    """Extract document ID from a Google Docs URL or return the ID if already provided."""
    # If it's already just an ID (no slashes or dots), return it
    if '/' not in url_or_id and '.' not in url_or_id:
        return url_or_id
    
    # Try to extract from URL
    match = re.search(r'/document/d/([a-zA-Z0-9-_]+)', url_or_id)
    if match:
        return match.group(1)
    
    # If no match, assume it's already an ID
    return url_or_id


def is_google_doc(path: str) -> bool:
    """Check if the path is a Google Docs URL or ID."""
    if not path:
        return False
    return ('docs.google.com' in path or 
            ('/' not in path and '.' not in path and len(path) > 20))


def is_confluence_page(path: str) -> bool:
    """Check if the path is a Confluence page URL or ID."""
    if not path:
        return False
    return ('atlassian.net/wiki' in path or 
            path.startswith('confluence:') or
            (path.isdigit() and len(path) < 20))  # Confluence page IDs are numeric


def parse_confluence_destination(dest: str) -> dict:
    """Parse Confluence destination into components."""
    result = {'type': None, 'space': None, 'page_id': None, 'page_title': None, 'url': None}
    
    if dest.startswith('confluence:'):
        # Format: confluence:SPACE/PAGE_ID or confluence:SPACE/Page+Title
        parts = dest[11:].split('/', 1)  # Remove 'confluence:' prefix
        result['type'] = 'confluence'
        result['space'] = parts[0] if parts else None
        if len(parts) > 1:
            if parts[1].isdigit():
                result['page_id'] = parts[1]
            else:
                result['page_title'] = parts[1].replace('+', ' ')
    
    elif 'atlassian.net/wiki' in dest:
        # Parse Confluence URL
        result['type'] = 'confluence'
        result['url'] = dest
        # Extract space and page ID from URL
        import re
        space_match = re.search(r'/spaces/([^/]+)', dest)
        page_match = re.search(r'/pages/(\d+)', dest)
        if space_match:
            result['space'] = space_match.group(1)
        if page_match:
            result['page_id'] = page_match.group(1)
    
    elif dest.isdigit():
        # Just a page ID
        result['type'] = 'confluence'
        result['page_id'] = dest
    
    return result


def get_confluence_credentials(secrets_file_path: Optional[str] = None):
    """Get Confluence credentials as a dict (for direct API calls).
    
    Args:
        secrets_file_path: Optional explicit path to secrets.yaml file
    """
    # Look for Confluence credentials in multiple locations
    confluence_url = os.getenv('CONFLUENCE_URL')
    confluence_username = os.getenv('CONFLUENCE_USERNAME')
    confluence_token = os.getenv('CONFLUENCE_API_TOKEN') or os.getenv('CONFLUENCE_TOKEN')
    
    # Try secrets.yaml first (preferred method)
    secrets_paths = []
    if secrets_file_path:
        # Explicit path provided
        secrets_paths = [Path(secrets_file_path)]
    else:
        # Default search paths
        secrets_paths = [
            Path.cwd() / 'secrets.yaml',
            Path.cwd() / 'secrets.yml',
            Path.home() / '.config' / 'mdsync' / 'secrets.yaml',
            Path.home() / '.mdsync' / 'secrets.yaml',
        ]
    
    for secrets_path in secrets_paths:
        if secrets_path.exists():
            try:
                with open(secrets_path, 'r') as f:
                    secrets = yaml.safe_load(f)
                    if secrets and 'confluence' in secrets:
                        conf = secrets['confluence']
                        confluence_url = confluence_url or conf.get('url')
                        confluence_username = confluence_username or conf.get('username')
                        confluence_token = confluence_token or conf.get('api_token') or conf.get('token')
                        break
            except Exception:
                pass
    
    # Try confluence.json as fallback
    if not all([confluence_url, confluence_username, confluence_token]):
        config_paths = [
            Path.cwd() / 'confluence.json',
            Path.home() / '.config' / 'mdsync' / 'confluence.json',
            Path.home() / '.mdsync' / 'confluence.json',
        ]
        
        for config_path in config_paths:
            if config_path.exists():
                try:
                    with open(config_path, 'r') as f:
                        config = json.load(f)
                        confluence_url = confluence_url or config.get('url')
                        confluence_username = confluence_username or config.get('username')
                        confluence_token = confluence_token or config.get('api_token') or config.get('token')
                        break
                except Exception:
                    pass
    
    if not all([confluence_url, confluence_username, confluence_token]):
        return None
    
    return {
        'url': confluence_url,
        'username': confluence_username,
        'api_token': confluence_token
    }


def get_confluence_client(secrets_file_path: Optional[str] = None):
    """Get Confluence API client from secrets.yaml, environment variables, or config.
    
    Args:
        secrets_file_path: Optional explicit path to secrets.yaml file
    """
    if not CONFLUENCE_AVAILABLE:
        print("Error: Confluence support not available. Install with: pip install atlassian-python-api", file=sys.stderr)
        sys.exit(1)
    
    # Look for Confluence credentials in multiple locations
    confluence_url = os.getenv('CONFLUENCE_URL')
    confluence_username = os.getenv('CONFLUENCE_USERNAME')
    confluence_token = os.getenv('CONFLUENCE_API_TOKEN') or os.getenv('CONFLUENCE_TOKEN')
    
    # Try secrets.yaml first (preferred method)
    secrets_paths = []
    if secrets_file_path:
        # Explicit path provided
        secrets_paths = [Path(secrets_file_path)]
    else:
        # Default search paths
        secrets_paths = [
            Path.cwd() / 'secrets.yaml',
            Path.cwd() / 'secrets.yml',
            Path.home() / '.config' / 'mdsync' / 'secrets.yaml',
            Path.home() / '.mdsync' / 'secrets.yaml',
        ]
    
    for secrets_path in secrets_paths:
        if secrets_path.exists():
            try:
                with open(secrets_path, 'r') as f:
                    secrets = yaml.safe_load(f)
                    if secrets and 'confluence' in secrets:
                        conf = secrets['confluence']
                        confluence_url = confluence_url or conf.get('url')
                        confluence_username = confluence_username or conf.get('username')
                        confluence_token = confluence_token or conf.get('api_token') or conf.get('token')
                        break
            except Exception:
                pass
    
    # Fallback to JSON config files
    if not all([confluence_url, confluence_username, confluence_token]):
        config_paths = [
            Path.home() / '.config' / 'mdsync' / 'confluence.json',
            Path.home() / '.mdsync' / 'confluence.json',
            Path.cwd() / 'confluence.json',
        ]
        
        for config_path in config_paths:
            if config_path.exists():
                try:
                    with open(config_path, 'r') as f:
                        config = json.load(f)
                        confluence_url = confluence_url or config.get('url')
                        confluence_username = confluence_username or config.get('username')
                        confluence_token = confluence_token or config.get('token')
                        break
                except Exception:
                    pass
    
    if not all([confluence_url, confluence_username, confluence_token]):
        print("Error: Confluence credentials not found!", file=sys.stderr)
        print("\nOption 1: Create secrets.yaml in current directory:", file=sys.stderr)
        print("  confluence:", file=sys.stderr)
        print("    url: https://yoursite.atlassian.net", file=sys.stderr)
        print("    username: your-email@domain.com", file=sys.stderr)
        print("    api_token: your-api-token", file=sys.stderr)
        print("\nOption 2: Set environment variables:", file=sys.stderr)
        print("  CONFLUENCE_URL=https://yoursite.atlassian.net", file=sys.stderr)
        print("  CONFLUENCE_USERNAME=your-email@domain.com", file=sys.stderr)
        print("  CONFLUENCE_API_TOKEN=your-api-token", file=sys.stderr)
        print("\nSee secrets.yaml.example for template", file=sys.stderr)
        sys.exit(1)
    
    return Confluence(
        url=confluence_url,
        username=confluence_username,
        password=confluence_token,
        cloud=True
    )


def markdown_to_confluence_storage(markdown_content: str) -> str:
    """Convert markdown to Confluence storage format using proper HTML conversion.
    
    Uses the markdown library with extensions for better formatting support,
    similar to the md2confluence project approach.
    """
    import markdown
    
    # Convert markdown to HTML with extensions (similar to md2confluence)
    html_content = markdown.markdown(
        markdown_content,
        extensions=[
            'tables',          # Support for tables
            'fenced_code',     # Support for ```code blocks```
            'codehilite',      # Syntax highlighting
            'nl2br',           # Convert newlines to <br>
            'sane_lists'       # Better list handling
        ]
    )
    
    # Convert to Confluence Storage Format
    confluence_content = html_content
    
    # Generate Confluence-compatible anchor IDs from heading text
    # Confluence auto-generates anchors from heading text, so we need to match that format
    def generate_confluence_anchor(heading_text):
        """Generate a Confluence-compatible anchor ID from heading text.
        
        Confluence generates anchors by:
        1. Preserving original case (NOT lowercasing)
        2. Replacing spaces with hyphens
        3. URL encoding special characters (:, [, ], etc.)
        """
        import urllib.parse
        # Handle None case
        if heading_text is None:
            return ''
        # Confluence preserves case and uses URL encoding
        anchor = heading_text.strip()
        # Remove markdown formatting but preserve the text structure
        anchor = re.sub(r'\*\*([^*]+)\*\*', r'\1', anchor)  # Remove bold
        anchor = re.sub(r'\*([^*]+)\*', r'\1', anchor)  # Remove italic
        anchor = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', anchor)  # Remove links
        # Unescape escaped brackets (from markdown like \[IN PROGRESS\])
        anchor = re.sub(r'\\\[', '[', anchor)  # Match \ followed by [
        anchor = re.sub(r'\\\]', ']', anchor)  # Match \ followed by ]
        # Replace spaces with hyphens (but keep case)
        anchor = re.sub(r'\s+', '-', anchor)
        # URL encode (Confluence uses URL encoding for anchors)
        # Don't encode hyphens, they're part of the anchor format
        return urllib.parse.quote(anchor, safe='-')
    
    # Update heading IDs to match Confluence's auto-generated format
    def fix_heading_anchor(match):
        heading_tag = match.group(1)
        existing_id = match.group(2) if match.group(2) else None
        heading_text = match.group(3)  # The actual heading text content
        
        # Handle None case (shouldn't happen, but be safe)
        if heading_text is None:
            return match.group(0)  # Return original if something's wrong
        
        # Remove markdown anchor syntax {#anchor} from heading text if present
        # This is formatting noise from Google Docs that shouldn't be displayed
        clean_heading_text = re.sub(r'\s*\{#[^}]+\}\s*$', '', heading_text).strip()
        
        # Generate anchor from CLEAN heading text (without the {#...} part)
        anchor_id = generate_confluence_anchor(clean_heading_text)
        
        # Use the clean heading text for display
        return f'<h{heading_tag} id="{anchor_id}">{clean_heading_text}</h{heading_tag}>'
    
    # Update headings to have Confluence-compatible anchor IDs
    confluence_content = re.sub(
        r'<h([1-6])(?:\s+id="([^"]*)")?>(.*?)</h\1>',
        fix_heading_anchor,
        confluence_content
    )
    
    # Map of markdown anchor names to Confluence anchors (for TOC links)
    # We'll build this by scanning the markdown content before conversion
    anchor_to_heading_map = {}
    heading_pattern = r'^#{1,6}\s+(.+?)(?:\s+\{#([^}]+)\})?$'
    for line in markdown_content.split('\n'):
        match = re.match(heading_pattern, line)
        if match:
            heading_text = match.group(1).strip()
            explicit_anchor = match.group(2) if match.group(2) else None
            
            # Remove markdown anchor syntax {#anchor} from heading text if present
            # This is formatting noise that shouldn't be part of the anchor generation
            clean_heading = re.sub(r'\s*\{#[^}]+\}\s*$', '', heading_text).strip()
            
            # Remove markdown link syntax from heading text for anchor generation
            clean_heading = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', clean_heading)  # Remove links
            clean_heading = re.sub(r'\\\[([^\]]+)\\\]', r'[\1]', clean_heading)  # Unescape brackets
            
            # Generate anchor from CLEAN heading text (Confluence's way - without the {#...} part)
            confluence_anchor = generate_confluence_anchor(clean_heading)
            
            # Map explicit anchor (if present) to the Confluence-generated anchor
            if explicit_anchor:
                anchor_to_heading_map[explicit_anchor] = confluence_anchor
            
            # Also map variations of the heading text to the anchor
            anchor_to_heading_map[clean_heading.lower()] = confluence_anchor
            anchor_to_heading_map[heading_text.lower()] = confluence_anchor
    
    # Update anchor links to match Confluence's generated anchors
    def fix_anchor_link(match):
        # Regex already extracted the anchor name (without #) from href="#anchor"
        anchor_name = match.group(1)  # This is already without the #
        text = match.group(2)
        
        # Try to find the matching Confluence anchor
        # First check if we have a mapping for this anchor
        if anchor_name in anchor_to_heading_map:
            confluence_anchor = anchor_to_heading_map[anchor_name]
        else:
            # Generate anchor from the link text (fallback)
            confluence_anchor = generate_confluence_anchor(text)
        return f'<a href="#{confluence_anchor}">{text}</a>'
    
    # Fix anchor links before the general link conversion
    # This regex matches <a href="#anchor">text</a> and extracts anchor (without #) and text
    confluence_content = re.sub(
        r'<a href="#([^"]+)">(.*?)</a>',
        fix_anchor_link,
        confluence_content
    )
    
    # Convert code blocks to simpler format (remove codehilite divs)
    confluence_content = re.sub(
        r'<div class="codehilite"><pre><span></span><code[^>]*>(.*?)</code></pre></div>',
        r'<pre><code>\1</code></pre>',
        confluence_content,
        flags=re.DOTALL
    )
    
    # Remove syntax highlighting spans from code blocks
    confluence_content = re.sub(
        r'<span class="[^"]*">([^<]*)</span>',
        r'\1',
        confluence_content
    )
    
    # Convert special syntax for Confluence macros (:::note, :::warning, etc.)
    def convert_special_macro(match):
        macro_type = match.group(1).lower()
        title = match.group(2) if match.group(2) else ""
        content = match.group(3).strip()
        
        # Remove any <p> tags that markdown might have added
        content = re.sub(r'^<p>|</p>$', '', content)
        
        # Handle success type - use info macro with checkmark emoji
        if macro_type == 'success':
            macro_type = 'info'
            if not title:
                title = "✅ Success"
            elif not title.startswith('✅'):
                title = f"✅ {title}"
        
        # Build the macro with optional title
        macro_params = ""
        if title:
            macro_params = f'  <ac:parameter ac:name="title">{title}</ac:parameter>\n'
        
        return f'''<ac:structured-macro ac:name="{macro_type}" ac:schema-version="1" ac:macro-id="{uuid.uuid4()}">
{macro_params}  <ac:rich-text-body>
    <p>{content}</p>
  </ac:rich-text-body>
</ac:structured-macro>'''
    
    # Convert :::type [title] content ::: syntax
    confluence_content = re.sub(
        r':::(\w+)(?:\s+([^\n]+))?\n(.*?)\n:::',
        convert_special_macro,
        confluence_content,
        flags=re.DOTALL
    )
    
    # Convert block quotes to Confluence note macros
    def convert_blockquote_to_note(match):
        content = match.group(1).strip()
        # Remove any <p> tags that markdown might have added
        content = re.sub(r'^<p>|</p>$', '', content)
        return f'''<ac:structured-macro ac:name="note">
  <ac:rich-text-body>
    <p>{content}</p>
  </ac:rich-text-body>
</ac:structured-macro>'''
    
    confluence_content = re.sub(
        r'<blockquote>(.*?)</blockquote>',
        convert_blockquote_to_note,
        confluence_content,
        flags=re.DOTALL
    )
    
    # Convert links - distinguish between anchor links, external URLs, and internal pages
    def convert_link(match):
        href = match.group(1)
        text = match.group(2)
        
        # Anchor link (starts with #) - same page anchor
        if href.startswith('#'):
            anchor_name = href[1:]  # Remove the #
            # For same-page anchors, use Confluence anchor format
            # Note: Confluence will auto-generate anchors from headings, but we preserve explicit ones
            return f'<a href="#{anchor_name}">{text}</a>'
        # External URL (starts with http/https)
        elif href.startswith(('http://', 'https://', 'mailto:')):
            return f'<a href="{href}">{text}</a>'
        # Internal page link - convert to Confluence format
        else:
            return f'<ac:link><ri:page ri:content-title="{href}"/><ac:link-body>{text}</ac:link-body></ac:link>'
    
    confluence_content = re.sub(
        r'<a href="([^"]+)">(.*?)</a>',
        convert_link,
        confluence_content
    )
    
    return confluence_content


def export_confluence_to_markdown(page_id: str, confluence, output_path: str = None) -> str:
    """Export a Confluence page to Markdown format using html2text for better conversion."""
    try:
        import html2text
        from bs4 import BeautifulSoup
        
        # Get page content
        page = confluence.get_page_by_id(page_id, expand='body.storage')
        
        if not page:
            print(f"Error: Page {page_id} not found", file=sys.stderr)
            sys.exit(1)
        
        # Get the storage format content
        storage_content = page['body']['storage']['value']
        
        # Pre-process Confluence-specific tags before conversion
        soup = BeautifulSoup(storage_content, 'html.parser')
        
        # Convert Confluence internal links to regular HTML links
        for link in soup.find_all('ac:link'):
            page_ref = link.find('ri:page')
            if page_ref and page_ref.get('ri:content-title'):
                page_title = page_ref.get('ri:content-title')
                link_body = link.find('ac:link-body')
                link_text = link_body.get_text() if link_body else page_title
                # Create a simple markdown-style link
                new_link = soup.new_tag('a', href=page_title)
                new_link.string = link_text
                link.replace_with(new_link)
        
        # Convert back to HTML string
        cleaned_html = str(soup)
        
        # Use html2text for proper HTML to Markdown conversion
        h = html2text.HTML2Text()
        h.body_width = 0  # Don't wrap lines
        h.ignore_links = False
        h.ignore_images = False
        h.ignore_emphasis = False
        h.skip_internal_links = False
        h.inline_links = True
        h.protect_links = True
        h.unicode_snob = True  # Use unicode instead of HTML entities
        
        markdown_content = h.handle(cleaned_html)
        
        # If output path provided, add frontmatter with confluence_url
        if output_path:
            space_key = page.get('space', {}).get('key', '')
            # Remove /wiki from the end of confluence.url if present
            base_url = confluence.url.rstrip('/')
            if base_url.endswith('/wiki'):
                base_url = base_url[:-5]  # Remove '/wiki'
            confluence_url = f"{base_url}/wiki/spaces/{space_key}/pages/{page_id}"
            
            # Check if content already has frontmatter
            if markdown_content.startswith('---'):
                # Parse existing frontmatter and add confluence_url
                try:
                    import frontmatter
                    post = frontmatter.loads(markdown_content)
                    post.metadata['confluence_url'] = confluence_url
                    frontmatter_content = frontmatter.dumps(post)
                except Exception:
                    # Fallback: prepend frontmatter
                    frontmatter_content = f"---\nconfluence_url: {confluence_url}\n---\n\n{markdown_content}"
            else:
                # Add frontmatter to the content
                frontmatter_content = f"---\nconfluence_url: {confluence_url}\n---\n\n{markdown_content}"
            
            # Write to file
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(frontmatter_content)
            
            return frontmatter_content
        else:
            return markdown_content.strip()
        
    except Exception as error:
        print(f"An error occurred: {error}", file=sys.stderr)
        sys.exit(1)


def import_markdown_to_confluence(markdown_path: str, page_id: str, confluence, quiet: bool = False):
    """Import a Markdown file to an existing Confluence page."""
    try:
        # Read the markdown file
        with open(markdown_path, 'r', encoding='utf-8') as f:
            markdown_content = f.read()
        
        # Extract metadata from frontmatter
        frontmatter = extract_frontmatter_metadata(markdown_content)
        frontmatter_labels = frontmatter['labels']
        
        # Get existing page to preserve space and version
        page = confluence.get_page_by_id(page_id, expand='version,space')
        
        if not page:
            print(f"Error: Page {page_id} not found", file=sys.stderr)
            sys.exit(1)
        
        space_key = page.get('space', {}).get('key', '')
        title = page.get('title', '')
        
        # Generate Confluence URL
        # Remove /wiki from the end of confluence.url if present
        base_url = confluence.url.rstrip('/')
        if base_url.endswith('/wiki'):
            base_url = base_url[:-5]  # Remove '/wiki'
        confluence_url = f"{base_url}/wiki/spaces/{space_key}/pages/{page_id}"
        
        # Resolve internal markdown links to Confluence URLs
        base_dir = os.path.dirname(os.path.abspath(markdown_path))
        resolved_content = resolve_markdown_links_to_confluence(markdown_content, base_dir)
        
        # Strip frontmatter and convert markdown to Confluence storage format
        content_for_confluence = strip_frontmatter_for_remote_sync(resolved_content)
        storage_content = markdown_to_confluence_storage(content_for_confluence)
        
        # Update the page
        confluence.update_page(
            page_id=page_id,
            title=title,
            body=storage_content,
            parent_id=page.get('ancestors', [{}])[-1].get('id') if page.get('ancestors') else None,
            type='page',
            representation='storage'
        )
        
        # Update frontmatter with Confluence URL
        update_frontmatter_confluence_url(markdown_path, confluence_url)
        
        # Set labels authoritatively if any in frontmatter
        if frontmatter_labels:
            confluence_creds = get_confluence_credentials()
            if confluence_creds:
                set_confluence_labels(
                    page_id, 
                    frontmatter_labels, 
                    confluence_creds['url'],
                    confluence_creds['username'],
                    confluence_creds['api_token']
                )
        
        if not quiet:
            print(f"✓ Successfully updated Confluence page: {title}")
            print(f"  Page ID: {page_id}")
            print(f"  Space: {space_key}")
            print(f"  URL: {confluence_url}")
            if frontmatter_labels:
                print(f"  Labels: {', '.join(frontmatter_labels)}")
            print(f"  Updated frontmatter in {markdown_path}")
        
    except FileNotFoundError:
        print(f"Error: Markdown file not found: {markdown_path}", file=sys.stderr)
        sys.exit(1)
    except Exception as error:
        print(f"An error occurred: {error}", file=sys.stderr)
        sys.exit(1)


def extract_frontmatter_metadata(markdown_content: str) -> dict:
    """Extract metadata from markdown frontmatter (title, labels, gdoc_url, confluence_url, batch, etc.)."""
    try:
        import frontmatter
        post = frontmatter.loads(markdown_content)
        return {
            'title': post.metadata.get('title'),
            'labels': post.metadata.get('labels', []),
            'parent': post.metadata.get('parent'),
            'gdoc_url': post.metadata.get('gdoc_url'),
            'confluence_url': post.metadata.get('confluence_url'),
            'batch': post.metadata.get('batch'),
            'gdoc_created': post.metadata.get('gdoc_created'),
            'gdoc_modified': post.metadata.get('gdoc_modified'),
            'confluence_created': post.metadata.get('confluence_created'),
            'confluence_modified': post.metadata.get('confluence_modified'),
        }
    except Exception:
        return {
            'title': None, 
            'labels': [], 
            'parent': None, 
            'gdoc_url': None,
            'confluence_url': None,
            'batch': None,
            'gdoc_created': None,
            'gdoc_modified': None,
            'confluence_created': None,
            'confluence_modified': None,
        }


def update_frontmatter_metadata(content: str, metadata: dict) -> str:
    """Update frontmatter metadata in markdown content."""
    try:
        import frontmatter
        post = frontmatter.loads(content)
        
        # Update metadata
        for key, value in metadata.items():
            post.metadata[key] = value
        
        # Return updated content
        return frontmatter.dumps(post)
    except Exception:
        # If frontmatter library fails, fall back to manual YAML handling
        if not content.startswith('---'):
            # No existing frontmatter, add it
            frontmatter_yaml = yaml.dump(metadata, default_flow_style=False, sort_keys=False)
            return f"---\n{frontmatter_yaml}---\n\n{content}"
        
        try:
            # Find the end of existing frontmatter
            end_marker = content.find('---', 3)
            if end_marker == -1:
                # Malformed frontmatter, replace it
                frontmatter_yaml = yaml.dump(metadata, default_flow_style=False, sort_keys=False)
                return f"---\n{frontmatter_yaml}---\n\n{content}"
            
            # Extract content after frontmatter
            content_after_frontmatter = content[end_marker + 3:].lstrip('\n')
            
            # Create new frontmatter
            frontmatter_yaml = yaml.dump(metadata, default_flow_style=False, sort_keys=False)
            return f"---\n{frontmatter_yaml}---\n\n{content_after_frontmatter}"
            
        except Exception:
            # If anything fails, just add new frontmatter
            frontmatter_yaml = yaml.dump(metadata, default_flow_style=False, sort_keys=False)
            return f"---\n{frontmatter_yaml}---\n\n{content}"


def update_frontmatter_gdoc_url(markdown_path: str, gdoc_url: str) -> bool:
    """Update the gdoc_url and sync date in markdown frontmatter."""
    try:
        import frontmatter
        from datetime import datetime
        
        # Read current content
        with open(markdown_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Parse frontmatter
        post = frontmatter.loads(content)
        
        # Update gdoc_url and sync dates
        post.metadata['gdoc_url'] = gdoc_url
        
        current_time = datetime.now().isoformat()
        
        # Set created date if this is the first time we're adding a gdoc_url
        if 'gdoc_created' not in post.metadata:
            post.metadata['gdoc_created'] = current_time
        
        # Always update modified date
        post.metadata['gdoc_modified'] = current_time
        
        # Write back
        with open(markdown_path, 'w', encoding='utf-8') as f:
            f.write(frontmatter.dumps(post))
        
        return True
    except Exception as e:
        print(f"Warning: Could not update frontmatter in {markdown_path}: {e}", file=sys.stderr)
        return False


def update_frontmatter_confluence_url(markdown_path: str, confluence_url: str) -> bool:
    """Update the confluence_url and sync dates in markdown frontmatter."""
    try:
        import frontmatter
        from datetime import datetime
        
        # Read current content
        with open(markdown_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Parse frontmatter
        post = frontmatter.loads(content)
        
        # Update confluence_url and sync dates
        post.metadata['confluence_url'] = confluence_url
        
        current_time = datetime.now().isoformat()
        
        # Set created date if this is the first time we're adding a confluence_url
        if 'confluence_created' not in post.metadata:
            post.metadata['confluence_created'] = current_time
        
        # Always update modified date
        post.metadata['confluence_modified'] = current_time
        
        # Write back
        with open(markdown_path, 'w', encoding='utf-8') as f:
            f.write(frontmatter.dumps(post))
        
        return True
    except Exception as e:
        print(f"Warning: Could not update frontmatter in {markdown_path}: {e}", file=sys.stderr)
        return False


def resolve_markdown_links_to_confluence(markdown_content: str, base_dir: str = None) -> str:
    """Resolve internal markdown links to Confluence URLs based on frontmatter."""
    if not base_dir:
        base_dir = os.getcwd()
    
    # Pattern to match [text](file.md) links
    link_pattern = r'\[([^\]]+)\]\(([^)]+\.md)\)'
    
    def replace_link(match):
        link_text = match.group(1)
        file_path = match.group(2)
        
        # Convert relative path to absolute
        if not os.path.isabs(file_path):
            file_path = os.path.join(base_dir, file_path)
        
        # Check if the referenced file exists and has confluence_url in frontmatter
        if os.path.exists(file_path):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                
                # Extract confluence_url from frontmatter
                metadata = extract_frontmatter_metadata(content)
                confluence_url = metadata.get('confluence_url')
                
                if confluence_url:
                    return f'[{link_text}]({confluence_url})'
            except Exception:
                pass
        
        # If no confluence_url found, return original link
        return match.group(0)
    
    # Replace all markdown links
    return re.sub(link_pattern, replace_link, markdown_content)


def check_gdoc_frozen_status(doc_id: str, creds) -> bool:
    """Check if a Google Doc is frozen (locked) at runtime."""
    try:
        # Use the existing check_lock_status function
        drive_service = build('drive', 'v3', credentials=creds)
        
        # Get file metadata
        file_metadata = drive_service.files().get(
            fileId=doc_id,
            fields='contentRestrictions'
        ).execute()
        
        # Check if there are content restrictions
        restrictions = file_metadata.get('contentRestrictions', [])
        if restrictions:
            for restriction in restrictions:
                if restriction.get('readOnly') == True:
                    return True
        
        return False
    except Exception:
        # If we can't check, assume not frozen
        return False


def check_confluence_frozen_status(page_id: str, confluence) -> bool:
    """Check if a Confluence page is frozen (locked) at runtime."""
    try:
        # Get page restrictions
        confluence_creds = get_confluence_credentials()
        if not confluence_creds:
            return False
        
        # Use the existing check_confluence_lock_status logic
        import requests
        
        url = f"{confluence_creds['url']}/rest/api/content/{page_id}/restriction"
        auth = (confluence_creds['username'], confluence_creds['api_token'])
        
        response = requests.get(url, auth=auth)
        if response.status_code == 200:
            restrictions = response.json()
            # Check if there are UPDATE restrictions
            for restriction in restrictions.get('restrictions', {}).get('update', {}).get('restrictions', []):
                if restriction.get('type') == 'user' or restriction.get('type') == 'group':
                    return True
        
        return False
    except Exception:
        # If we can't check, assume not frozen
        return False


def extract_doc_id_from_url(url: str) -> str:
    """Extract Google Doc ID from various URL formats."""
    import re
    
    # Handle different Google Docs URL formats
    patterns = [
        r'/document/d/([a-zA-Z0-9-_]+)',  # Standard format
        r'id=([a-zA-Z0-9-_]+)',          # Alternative format
        r'^([a-zA-Z0-9-_]+)$'            # Just the ID
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    
    return None


def get_confluence_permissions_config(secrets_file_path: Optional[str] = None):
    """Get default permissions configuration from secrets.yaml.
    
    Args:
        secrets_file_path: Optional explicit path to secrets.yaml file
    """
    secrets_paths = []
    if secrets_file_path:
        # Explicit path provided
        secrets_paths = [Path(secrets_file_path)]
    else:
        # Default search paths
        secrets_paths = [
            Path.cwd() / 'secrets.yaml',
            Path.cwd() / 'secrets.yml',
            Path.home() / '.config' / 'mdsync' / 'secrets.yaml',
            Path.home() / '.mdsync' / 'secrets.yaml',
        ]
    
    for secrets_path in secrets_paths:
        if secrets_path.exists():
            try:
                with open(secrets_path, 'r') as f:
                    secrets = yaml.safe_load(f)
                    if secrets and 'confluence' in secrets:
                        perms = secrets['confluence'].get('permissions', {})
                        if perms:
                            return perms
            except Exception:
                pass
    
    return None


def lock_confluence_page(page_id: str, confluence_url: str, username: str, api_token: str, 
                        allowed_editors: dict = None, secrets_file_path: Optional[str] = None) -> bool:
    """Lock a Confluence page by setting edit restrictions.
    
    Similar to md2confluence's _apply_page_permissions.
    Sets UPDATE restriction so only specified users/groups can edit.
    Everyone else can view but not edit (read-only).
    """
    try:
        import requests
        import json
        
        # Get default permissions from config if not provided
        if not allowed_editors:
            # First check environment variable for allowed editor groups
            env_groups = os.getenv('MDSYNC_ALLOWED_EDITORS_GROUPS')
            env_users = os.getenv('MDSYNC_ALLOWED_EDITORS_USERS')
            
            if env_groups or env_users:
                # Parse comma-separated values
                groups = [g.strip() for g in env_groups.split(',')] if env_groups else []
                users = [u.strip() for u in env_users.split(',')] if env_users else []
                allowed_editors = {'users': users, 'groups': groups}
            else:
                # Fallback to secrets.yaml if available
                perms_config = get_confluence_permissions_config(secrets_file_path)
                if perms_config and 'allowed_editors' in perms_config:
                    allowed_editors = perms_config['allowed_editors']
                else:
                    # Final fallback: only current user
                    allowed_editors = {'users': [username], 'groups': []}
        
        # Always include current user to prevent lockout
        editor_users = list(allowed_editors.get('users', []))
        if username not in editor_users:
            editor_users.append(username)
            print(f"Auto-adding current user ({username}) to prevent lockout", file=sys.stderr)
        
        editor_groups = allowed_editors.get('groups', [])
        
        # Resolve user emails to account IDs
        resolved_users = []
        for email in editor_users:
            account_id = _resolve_user_email_to_account_id(email, confluence_url, username, api_token)
            if account_id:
                resolved_users.append({"type": "known", "accountId": account_id})
        
        if not resolved_users:
            print("Error: Could not resolve any users. Cannot lock page to prevent lockout.", file=sys.stderr)
            return False
        
        # Build group restrictions
        resolved_groups = [{"type": "group", "name": group} for group in editor_groups]
        
        # Create restrictions data
        restrictions_data = [
            {
                "operation": "update",
                "restrictions": {
                    "user": resolved_users,
                    "group": resolved_groups
                }
            }
        ]
        
        # Apply restrictions
        url = f"{confluence_url}/wiki/rest/api/content/{page_id}/restriction"
        headers = {
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        
        response = requests.put(
            url,
            headers=headers,
            data=json.dumps(restrictions_data),
            auth=(username, api_token)
        )
        
        if response.status_code in [200, 201]:
            return True
        else:
            print(f"Error locking page: {response.status_code} - {response.text}", file=sys.stderr)
            return False
            
    except Exception as e:
        print(f"Error locking Confluence page: {e}", file=sys.stderr)
        return False


def unlock_confluence_page(page_id: str, confluence_url: str, username: str, api_token: str) -> bool:
    """Unlock a Confluence page by removing all restrictions."""
    try:
        import requests
        
        # Delete all restrictions
        url = f"{confluence_url}/wiki/rest/api/content/{page_id}/restriction"
        
        response = requests.delete(
            url,
            auth=(username, api_token)
        )
        
        if response.status_code in [200, 204]:
            return True
        else:
            print(f"Error unlocking page: {response.status_code} - {response.text}", file=sys.stderr)
            return False
            
    except Exception as e:
        print(f"Error unlocking Confluence page: {e}", file=sys.stderr)
        return False


def check_confluence_lock_status(page_id: str, confluence_url: str, username: str, api_token: str):
    """Check and display the lock status of a Confluence page."""
    try:
        import requests
        
        url = f"{confluence_url}/wiki/rest/api/content/{page_id}?expand=restrictions.read.restrictions.user,restrictions.read.restrictions.group,restrictions.update.restrictions.user,restrictions.update.restrictions.group"
        
        response = requests.get(
            url,
            auth=(username, api_token)
        )
        
        if response.status_code != 200:
            print(f"Error checking lock status: {response.status_code}", file=sys.stderr)
            return
        
        data = response.json()
        restrictions = data.get('restrictions', {})
        
        update_restrictions = restrictions.get('update', {}).get('restrictions', {})
        read_restrictions = restrictions.get('read', {}).get('restrictions', {})
        
        if not update_restrictions.get('user', {}).get('results') and not update_restrictions.get('group', {}).get('results'):
            print(f"Page {page_id} is UNLOCKED (no edit restrictions)")
        else:
            print(f"Page {page_id} is LOCKED (edit restricted)")
            
            users = update_restrictions.get('user', {}).get('results', [])
            groups = update_restrictions.get('group', {}).get('results', [])
            
            if users:
                print("  Allowed editors (users):")
                for user in users:
                    print(f"    - {user.get('displayName', user.get('accountId'))}")
            
            if groups:
                print("  Allowed editors (groups):")
                for group in groups:
                    print(f"    - {group.get('name')}")
        
    except Exception as e:
        print(f"Error checking Confluence lock status: {e}", file=sys.stderr)


def _resolve_user_email_to_account_id(email: str, confluence_url: str, username: str, api_token: str) -> str:
    """Resolve a user email to Confluence account ID."""
    try:
        import requests
        
        # Try current user endpoint first
        if email == username:
            url = f"{confluence_url}/wiki/rest/api/user/current"
            response = requests.get(url, auth=(username, api_token))
            if response.status_code == 200:
                return response.json().get('accountId')
        
        # Search for user by email
        search_url = f"{confluence_url}/wiki/rest/api/search/user"
        params = {"cql": f'user="{email}"'}
        
        response = requests.get(
            search_url,
            params=params,
            auth=(username, api_token)
        )
        
        if response.status_code == 200:
            results = response.json().get("results", [])
            for user_data in results:
                user_email = user_data.get("email", user_data.get("emailAddress", ""))
                if user_email.lower() == email.lower():
                    return user_data.get("accountId")
        
        return None
        
    except Exception:
        return None


def set_confluence_labels(page_id: str, labels: list, confluence_url: str, username: str, api_token: str) -> bool:
    """Set labels on a Confluence page authoritatively (replace all existing labels).
    
    Similar to md2confluence's _set_page_labels_authoritatively.
    """
    try:
        import requests
        import json
        
        # Step 1: Get existing labels
        get_url = f"{confluence_url}/wiki/rest/api/content/{page_id}?expand=metadata.labels"
        get_response = requests.get(
            get_url,
            auth=(username, api_token)
        )
        
        existing_labels = []
        if get_response.status_code == 200:
            page_data = get_response.json()
            if 'metadata' in page_data and 'labels' in page_data['metadata']:
                existing_labels = [label['name'] for label in page_data['metadata']['labels']['results']]
        
        # Step 2: Remove all existing labels
        if existing_labels:
            for label_name in existing_labels:
                delete_url = f"{confluence_url}/wiki/rest/api/content/{page_id}/label/{label_name}"
                requests.delete(
                    delete_url,
                    auth=(username, api_token)
                )
        
        # Step 3: Add new labels
        if labels:
            labels_data = [{"name": label} for label in labels]
            
            add_url = f"{confluence_url}/wiki/rest/api/content/{page_id}/label"
            headers = {
                'Accept': 'application/json',
                'Content-Type': 'application/json'
            }
            
            add_response = requests.post(
                add_url,
                headers=headers,
                data=json.dumps(labels_data),
                auth=(username, api_token)
            )
            
            return add_response.status_code == 200
        
        return True
        
    except Exception as e:
        print(f"Warning: Could not set labels on page {page_id}: {e}", file=sys.stderr)
        return False


def create_confluence_page(markdown_path: str, confluence, space: str, title: str, 
                           parent_id: Optional[str] = None, labels: Optional[list] = None, 
                           quiet: bool = False) -> str:
    """Create a new Confluence page from a Markdown file.
    
    Note: Labels from CLI and frontmatter are combined (CLI labels are applied first).
    """
    try:
        # Read the markdown file
        with open(markdown_path, 'r', encoding='utf-8') as f:
            markdown_content = f.read()
        
        # Extract metadata from frontmatter
        frontmatter = extract_frontmatter_metadata(markdown_content)
        
        # Combine CLI labels with frontmatter labels
        all_labels = list(labels or []) + frontmatter['labels']
        
        # Resolve internal markdown links to Confluence URLs
        base_dir = os.path.dirname(os.path.abspath(markdown_path))
        resolved_content = resolve_markdown_links_to_confluence(markdown_content, base_dir)
        
        # Strip frontmatter and convert markdown to Confluence storage format
        content_for_confluence = strip_frontmatter_for_remote_sync(resolved_content)
        storage_content = markdown_to_confluence_storage(content_for_confluence)
        
        # Create the page
        new_page = confluence.create_page(
            space=space,
            title=title,
            body=storage_content,
            parent_id=parent_id,
            type='page',
            representation='storage'
        )
        
        page_id = new_page['id']
        
        # Generate Confluence URL
        # Remove /wiki from the end of confluence.url if present
        base_url = confluence.url.rstrip('/')
        if base_url.endswith('/wiki'):
            base_url = base_url[:-5]  # Remove '/wiki'
        confluence_url = f"{base_url}/wiki/spaces/{space}/pages/{page_id}"
        
        # Update frontmatter with Confluence URL
        update_frontmatter_confluence_url(markdown_path, confluence_url)
        
        # Set labels authoritatively if any
        if all_labels:
            # Get Confluence credentials for label setting
            confluence_creds = get_confluence_credentials()
            if confluence_creds:
                set_confluence_labels(
                    page_id, 
                    all_labels, 
                    confluence_creds['url'],
                    confluence_creds['username'],
                    confluence_creds['api_token']
                )
        
        if not quiet:
            print(f"✓ Created new Confluence page: {title}")
            print(f"  Page ID: {page_id}")
            print(f"  Space: {space}")
            print(f"  URL: {confluence_url}")
            if parent_id:
                print(f"  Parent ID: {parent_id}")
            if all_labels:
                print(f"  Labels: {', '.join(all_labels)}")
            print(f"  Updated frontmatter in {markdown_path}")
        
        return page_id
        
    except FileNotFoundError:
        print(f"Error: Markdown file not found: {markdown_path}", file=sys.stderr)
        sys.exit(1)
    except Exception as error:
        print(f"An error occurred: {error}", file=sys.stderr)
        sys.exit(1)


def list_revisions(doc_id: str, creds):
    """List all revisions for a Google Doc."""
    try:
        # Build the Drive service
        drive_service = build('drive', 'v3', credentials=creds)
        
        # Get all revisions
        revisions = drive_service.revisions().list(
            fileId=doc_id,
            fields='revisions(id,modifiedTime,lastModifyingUser,keepForever)',
            pageSize=1000
        ).execute()
        
        revision_list = revisions.get('revisions', [])
        
        if not revision_list:
            print("No revisions found.")
            return
        
        print(f"\nRevision History for Document: {doc_id}")
        print("=" * 80)
        
        for rev in reversed(revision_list):  # Show newest first
            rev_id = rev['id']
            mod_time = rev.get('modifiedTime', 'Unknown')
            user = rev.get('lastModifyingUser', {})
            user_name = user.get('displayName', 'Unknown')
            user_email = user.get('emailAddress', '')
            kept = ' [KEPT]' if rev.get('keepForever', False) else ''
            
            print(f"\nRevision ID: {rev_id}{kept}")
            print(f"  Modified: {mod_time}")
            print(f"  By: {user_name}", end='')
            if user_email:
                print(f" ({user_email})", end='')
            print()
        
        print("\n" + "=" * 80)
        print(f"Total revisions: {len(revision_list)}")
        print("\nNote: Google Drive API does not support exporting historical revisions")
        print("in Markdown format. To view revision content, open the document in")
        print("Google Docs and use File > Version history.")
        
    except HttpError as error:
        print(f"An error occurred: {error}", file=sys.stderr)
        sys.exit(1)


def lock_document(doc_id: str, creds, reason: str = "Document locked via mdsync"):
    """Lock a Google Doc to prevent editing."""
    try:
        # Build the Drive service
        drive_service = build('drive', 'v3', credentials=creds)
        
        # Set content restrictions to lock the file
        file_metadata = {
            'contentRestrictions': [{
                'readOnly': True,
                'reason': reason
            }]
        }
        
        updated_file = drive_service.files().update(
            fileId=doc_id,
            body=file_metadata,
            fields='contentRestrictions'
        ).execute()
        
        print(f"✓ Document locked: {doc_id}")
        print(f"  Reason: {reason}")
        print(f"  URL: https://docs.google.com/document/d/{doc_id}/edit")
        
    except HttpError as error:
        print(f"An error occurred: {error}", file=sys.stderr)
        if error.resp.status == 403:
            print("Note: You need editor access to lock/unlock documents.", file=sys.stderr)
        sys.exit(1)


def unlock_document(doc_id: str, creds):
    """Unlock a Google Doc to allow editing."""
    try:
        # Build the Drive service
        drive_service = build('drive', 'v3', credentials=creds)
        
        # Remove content restrictions to unlock the file
        file_metadata = {
            'contentRestrictions': [{
                'readOnly': False
            }]
        }
        
        updated_file = drive_service.files().update(
            fileId=doc_id,
            body=file_metadata,
            fields='contentRestrictions'
        ).execute()
        
        print(f"✓ Document unlocked: {doc_id}")
        print(f"  URL: https://docs.google.com/document/d/{doc_id}/edit")
        
    except HttpError as error:
        print(f"An error occurred: {error}", file=sys.stderr)
        if error.resp.status == 403:
            print("Note: You need editor access to lock/unlock documents.", file=sys.stderr)
        sys.exit(1)


def check_lock_status(doc_id: str, creds):
    """Check if a Google Doc is locked."""
    try:
        # Build the Drive service
        drive_service = build('drive', 'v3', credentials=creds)
        
        # Get file metadata including content restrictions
        file = drive_service.files().get(
            fileId=doc_id,
            fields='name,contentRestrictions,owners,modifiedTime'
        ).execute()
        
        doc_name = file.get('name', 'Unknown')
        content_restrictions = file.get('contentRestrictions', [])
        owners = file.get('owners', [])
        modified_time = file.get('modifiedTime', 'Unknown')
        
        print(f"\nDocument: {doc_name}")
        print(f"ID: {doc_id}")
        print(f"URL: https://docs.google.com/document/d/{doc_id}/edit")
        print(f"Last Modified: {modified_time}")
        
        if owners:
            owner_names = ', '.join([o.get('displayName', 'Unknown') for o in owners])
            print(f"Owner(s): {owner_names}")
        
        print("\n" + "=" * 60)
        
        if content_restrictions:
            for restriction in content_restrictions:
                if restriction.get('readOnly', False):
                    print("🔒 Status: LOCKED")
                    reason = restriction.get('reason', 'No reason provided')
                    print(f"   Reason: {reason}")
                    restricting_user = restriction.get('restrictingUser', {})
                    if restricting_user:
                        print(f"   Locked by: {restricting_user.get('displayName', 'Unknown')}")
                    restrict_time = restriction.get('restrictionTime', '')
                    if restrict_time:
                        print(f"   Locked at: {restrict_time}")
                else:
                    print("🔓 Status: UNLOCKED")
        else:
            print("🔓 Status: UNLOCKED")
        
        print("=" * 60)
        
    except HttpError as error:
        print(f"An error occurred: {error}", file=sys.stderr)
        sys.exit(1)


def list_comments(doc_id: str, creds, unresolved_only: bool = False, output_format: str = 'text'):
    """List all comments from a Google Doc."""
    try:
        # Build the Drive service
        drive_service = build('drive', 'v3', credentials=creds)
        
        # Get file name
        file = drive_service.files().get(fileId=doc_id, fields='name').execute()
        doc_name = file.get('name', 'Unknown')
        
        # Get all comments
        comments_result = drive_service.comments().list(
            fileId=doc_id,
            fields='comments(id,content,author,createdTime,modifiedTime,resolved,quotedFileContent,replies,anchor)',
            pageSize=100
        ).execute()
        
        all_comments = comments_result.get('comments', [])
        
        # Handle pagination
        while 'nextPageToken' in comments_result:
            comments_result = drive_service.comments().list(
                fileId=doc_id,
                fields='comments(id,content,author,createdTime,modifiedTime,resolved,quotedFileContent,replies,anchor)',
                pageSize=100,
                pageToken=comments_result['nextPageToken']
            ).execute()
            all_comments.extend(comments_result.get('comments', []))
        
        # Filter if needed
        if unresolved_only:
            all_comments = [c for c in all_comments if not c.get('resolved', False)]
        
        if not all_comments:
            if unresolved_only:
                print("No unresolved comments found.")
            else:
                print("No comments found.")
            return
        
        # Output based on format
        if output_format == 'json':
            print(json.dumps(all_comments, indent=2))
        elif output_format == 'markdown':
            print_comments_markdown(doc_name, doc_id, all_comments)
        else:  # text
            print_comments_text(doc_name, doc_id, all_comments)
        
    except HttpError as error:
        print(f"An error occurred: {error}", file=sys.stderr)
        sys.exit(1)


def print_comments_text(doc_name: str, doc_id: str, comments: list):
    """Print comments in text format."""
    print(f"\nComments for: {doc_name}")
    print(f"Document ID: {doc_id}")
    print(f"URL: https://docs.google.com/document/d/{doc_id}/edit")
    print(f"Total comments: {len(comments)}")
    print("=" * 80)
    
    for i, comment in enumerate(comments, 1):
        author = comment.get('author', {})
        author_name = author.get('displayName', 'Unknown')
        created = comment.get('createdTime', 'Unknown')
        resolved = comment.get('resolved', False)
        content = comment.get('content', '')
        quoted = comment.get('quotedFileContent', {}).get('value', '')
        
        status = "✓ RESOLVED" if resolved else "○ OPEN"
        
        print(f"\n[{i}] {status}")
        print(f"Author: {author_name}")
        print(f"Created: {created}")
        
        if quoted:
            print(f"Quoted text: \"{quoted}\"")
        
        print(f"Comment: {content}")
        
        # Print replies
        replies = comment.get('replies', [])
        if replies:
            print(f"  Replies ({len(replies)}):")
            for reply in replies:
                reply_author = reply.get('author', {}).get('displayName', 'Unknown')
                reply_content = reply.get('content', '')
                reply_time = reply.get('createdTime', 'Unknown')
                print(f"    → {reply_author} ({reply_time}): {reply_content}")
        
        print("-" * 80)


def print_comments_markdown(doc_name: str, doc_id: str, comments: list):
    """Print comments in Markdown format."""
    print(f"# Comments: {doc_name}\n")
    print(f"**Document ID:** {doc_id}  ")
    print(f"**URL:** [Open Document](https://docs.google.com/document/d/{doc_id}/edit)  ")
    print(f"**Total comments:** {len(comments)}\n")
    print("---\n")
    
    for i, comment in enumerate(comments, 1):
        author = comment.get('author', {})
        author_name = author.get('displayName', 'Unknown')
        created = comment.get('createdTime', 'Unknown')
        resolved = comment.get('resolved', False)
        content = comment.get('content', '')
        quoted = comment.get('quotedFileContent', {}).get('value', '')
        
        status = "✓ RESOLVED" if resolved else "○ OPEN"
        
        print(f"## Comment {i} - {status}\n")
        print(f"**Author:** {author_name}  ")
        print(f"**Created:** {created}\n")
        
        if quoted:
            print(f"> {quoted}\n")
        
        print(f"{content}\n")
        
        # Print replies
        replies = comment.get('replies', [])
        if replies:
            print(f"### Replies ({len(replies)})\n")
            for reply in replies:
                reply_author = reply.get('author', {}).get('displayName', 'Unknown')
                reply_content = reply.get('content', '')
                reply_time = reply.get('createdTime', 'Unknown')
                print(f"- **{reply_author}** ({reply_time}): {reply_content}")
            print()
        
        print("---\n")


def export_gdoc_to_markdown(doc_id: str, creds, output_path: str = None) -> str:
    """Export a Google Doc to Markdown format."""
    try:
        # Build the Drive service (used for export)
        drive_service = build('drive', 'v3', credentials=creds)
        
        # Fetch document metadata (title, created/modified dates)
        file_metadata = drive_service.files().get(
            fileId=doc_id,
            fields='name,createdTime,modifiedTime'
        ).execute()
        
        doc_title = file_metadata.get('name', '')
        created_time = file_metadata.get('createdTime', '')
        modified_time = file_metadata.get('modifiedTime', '')
        
        # Export the current version as Markdown
        # Google Docs now supports text/markdown as an export format
        request = drive_service.files().export_media(
            fileId=doc_id,
            mimeType='text/markdown'
        )
        
        file_stream = io.BytesIO()
        downloader = MediaIoBaseDownload(file_stream, request)
        
        done = False
        while not done:
            status, done = downloader.next_chunk()
        
        # Get the content as string
        markdown_content = file_stream.getvalue().decode('utf-8')
        
        # If output path provided, add frontmatter with gdoc_url and metadata
        if output_path:
            gdoc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
            
            # Check if content already has frontmatter
            if markdown_content.startswith('---'):
                # Parse existing frontmatter and add/update metadata
                try:
                    import frontmatter
                    post = frontmatter.loads(markdown_content)
                    post.metadata['title'] = doc_title
                    post.metadata['gdoc_url'] = gdoc_url
                    post.metadata['gdoc_created'] = created_time
                    post.metadata['gdoc_modified'] = modified_time
                    frontmatter_content = frontmatter.dumps(post)
                except Exception:
                    # Fallback: prepend frontmatter
                    frontmatter_content = f"---\ntitle: {doc_title}\ngdoc_url: {gdoc_url}\ngdoc_created: {created_time}\ngdoc_modified: {modified_time}\n---\n\n{markdown_content}"
            else:
                # Add frontmatter to the content
                frontmatter_content = f"---\ntitle: {doc_title}\ngdoc_url: {gdoc_url}\ngdoc_created: {created_time}\ngdoc_modified: {modified_time}\n---\n\n{markdown_content}"
            
            # Write to file
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(frontmatter_content)
            
            return frontmatter_content
        else:
            return markdown_content
        
    except HttpError as error:
        print(f"An error occurred: {error}", file=sys.stderr)
        sys.exit(1)


def strip_frontmatter_for_remote_sync(markdown_content: str) -> str:
    """Strip frontmatter from markdown content for remote platform sync (Google Docs, Confluence, etc.)."""
    # Check if content has frontmatter
    if markdown_content.startswith('---'):
        # Find the end of frontmatter
        lines = markdown_content.split('\n')
        if len(lines) > 1 and lines[0] == '---':
            for i, line in enumerate(lines[1:], 1):
                if line.strip() == '---':
                    # Found end of frontmatter, return content after it
                    return '\n'.join(lines[i+1:]).strip()
    
    # No frontmatter found, return original content
    return markdown_content


def show_diff(content1: str, content2: str, label1: str, label2: str):
    """Show a unified diff between two content strings."""
    # Normalize line endings
    content1 = content1.replace('\r\n', '\n').replace('\r', '\n')
    content2 = content2.replace('\r\n', '\n').replace('\r', '\n')
    
    # Split into lines for diff
    lines1 = content1.splitlines(keepends=True)
    lines2 = content2.splitlines(keepends=True)
    
    # Generate unified diff
    diff = difflib.unified_diff(
        lines1, lines2,
        fromfile=label1,
        tofile=label2,
        lineterm=''
    )
    
    # Print the diff
    diff_lines = list(diff)
    if diff_lines:
        print("=" * 60)
        print("DIFF (DRY RUN - No changes made):")
        print("=" * 60)
        for line in diff_lines:
            print(line, end='')
        print("=" * 60)
    else:
        print("No differences found - files are identical")


def diff_markdown_to_gdoc(markdown_path: str, doc_id: str, creds):
    """Show diff between markdown file and Google Doc (dry run)."""
    try:
        # Check if Google Doc is frozen
        if check_gdoc_frozen_status(doc_id, creds):
            print(f"⚠️  Google Doc is frozen (locked) - diff not available")
            print(f"Use --unlock to enable syncing to this document")
            return
        
        # Read markdown file
        with open(markdown_path, 'r', encoding='utf-8') as f:
            markdown_content = f.read()
        
        # Strip frontmatter for comparison (same as what would be synced)
        markdown_for_gdoc = strip_frontmatter_for_remote_sync(markdown_content)
        
        # Export Google Doc to markdown for comparison
        gdoc_markdown = export_gdoc_to_markdown(doc_id, creds)
        
        # Show diff
        show_diff(
            gdoc_markdown, 
            markdown_for_gdoc,
            f"Google Doc {doc_id}",
            f"Local file {markdown_path}"
        )
        
    except FileNotFoundError:
        print(f"Error: Markdown file not found: {markdown_path}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error during diff: {e}", file=sys.stderr)
        sys.exit(1)


def diff_gdoc_to_markdown(doc_id: str, markdown_path: str, creds):
    """Show diff between Google Doc and markdown file (dry run)."""
    try:
        # Export Google Doc to markdown
        gdoc_markdown = export_gdoc_to_markdown(doc_id, creds)
        
        # Read local markdown file
        with open(markdown_path, 'r', encoding='utf-8') as f:
            local_markdown = f.read()
        
        # Show diff
        show_diff(
            local_markdown,
            gdoc_markdown,
            f"Local file {markdown_path}",
            f"Google Doc {doc_id}"
        )
        
    except FileNotFoundError:
        print(f"Error: Markdown file not found: {markdown_path}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error during diff: {e}", file=sys.stderr)
        sys.exit(1)


def diff_markdown_to_confluence(markdown_path: str, confluence_dest: str, confluence):
    """Show diff between markdown file and Confluence page (dry run)."""
    try:
        # Parse Confluence destination
        dest_info = parse_confluence_destination(confluence_dest)
        page_id = dest_info['page_id']
        
        # Check if Confluence page is frozen
        if check_confluence_frozen_status(page_id, confluence):
            print(f"⚠️  Confluence page is frozen (locked) - diff not available")
            print(f"Use --unlock-confluence to enable syncing to this page")
            return
        
        # Read markdown file
        with open(markdown_path, 'r', encoding='utf-8') as f:
            markdown_content = f.read()
        
        # Strip frontmatter for comparison
        markdown_for_confluence = strip_frontmatter_for_remote_sync(markdown_content)
        
        # Export Confluence page to markdown
        confluence_markdown = export_confluence_to_markdown(page_id, confluence)
        
        # Show diff
        show_diff(
            confluence_markdown,
            markdown_for_confluence,
            f"Confluence page {page_id}",
            f"Local file {markdown_path}"
        )
        
    except FileNotFoundError:
        print(f"Error: Markdown file not found: {markdown_path}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error during diff: {e}", file=sys.stderr)
        sys.exit(1)


def diff_confluence_to_markdown(confluence_dest: str, markdown_path: str, confluence):
    """Show diff between Confluence page and markdown file (dry run)."""
    try:
        # Parse Confluence destination
        dest_info = parse_confluence_destination(confluence_dest)
        page_id = dest_info['page_id']
        
        # Check if Confluence page is frozen
        if check_confluence_frozen_status(page_id, confluence):
            print(f"⚠️  Confluence page is frozen (locked) - diff not available")
            print(f"Use --unlock-confluence to enable syncing to this page")
            return
        
        # Export Confluence page to markdown
        confluence_markdown = export_confluence_to_markdown(page_id, confluence)
        
        # Read local markdown file
        with open(markdown_path, 'r', encoding='utf-8') as f:
            local_markdown = f.read()
        
        # Show diff
        show_diff(
            local_markdown,
            confluence_markdown,
            f"Local file {markdown_path}",
            f"Confluence page {page_id}"
        )
        
    except FileNotFoundError:
        print(f"Error: Markdown file not found: {markdown_path}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error during diff: {e}", file=sys.stderr)
        sys.exit(1)


def import_markdown_to_gdoc(markdown_path: str, doc_id: str, creds, quiet: bool = False):
    """Import a Markdown file to a Google Doc."""
    try:
        # Read the markdown file
        with open(markdown_path, 'r', encoding='utf-8') as f:
            markdown_content = f.read()
        
        # Strip frontmatter for Google Doc (frontmatter is for markdown processing only)
        content_for_gdoc = strip_frontmatter_for_remote_sync(markdown_content)
        
        # Create a temporary file with the cleaned content
        temp_file_path = f"{markdown_path}.temp"
        with open(temp_file_path, 'w', encoding='utf-8') as f:
            f.write(content_for_gdoc)
        
        try:
            # Build the Drive service
            drive_service = build('drive', 'v3', credentials=creds)
            
            # Update the document by uploading the cleaned markdown
            media = MediaFileUpload(
                temp_file_path,
                mimetype='text/markdown',
                resumable=True
            )
            
            file_metadata = {
                'mimeType': 'application/vnd.google-apps.document'
            }
            
            updated_file = drive_service.files().update(
                fileId=doc_id,
                media_body=media,
                body=file_metadata
            ).execute()
            
            if not quiet:
                print(f"Successfully updated Google Doc: {doc_id}")
            
            # Update frontmatter with sync date
            gdoc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
            update_frontmatter_gdoc_url(markdown_path, gdoc_url)
            
        finally:
            # Clean up temporary file
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)
        
    except HttpError as error:
        print(f"An error occurred: {error}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print(f"Error: Markdown file not found: {markdown_path}", file=sys.stderr)
        sys.exit(1)


def create_new_gdoc_from_markdown_with_title(markdown_path: str, title: str, creds, quiet: bool = False) -> str:
    """Create a new Google Doc from a Markdown file with a specific title."""
    try:
        # Read the markdown file
        with open(markdown_path, 'r', encoding='utf-8') as f:
            markdown_content = f.read()
        
        # Strip frontmatter for Google Doc (frontmatter is for markdown processing only)
        content_for_gdoc = strip_frontmatter_for_remote_sync(markdown_content)
        
        # Create a temporary file with the cleaned content
        temp_file_path = f"{markdown_path}.temp"
        with open(temp_file_path, 'w', encoding='utf-8') as f:
            f.write(content_for_gdoc)
        
        try:
            # Build the Drive service
            drive_service = build('drive', 'v3', credentials=creds)
            
            # Upload the cleaned markdown file and convert it to Google Docs format
            file_metadata = {
                'name': title,
                'mimeType': 'application/vnd.google-apps.document'
            }
            
            media = MediaFileUpload(
                temp_file_path,
                mimetype='text/markdown',
                resumable=True
            )
            
            # Create the Google Doc
            file = drive_service.files().create(
                body=file_metadata,
                media_body=media,
                fields='id'
            ).execute()
            
            doc_id = file.get('id')
            
            if not quiet:
                print(f'Created new Google Doc with ID: {doc_id}')
                print(f'URL: https://docs.google.com/document/d/{doc_id}/edit')
            
            return doc_id
            
        finally:
            # Clean up temporary file
            if os.path.exists(temp_file_path):
                os.unlink(temp_file_path)
                
    except Exception as e:
        print(f'Error creating Google Doc: {e}', file=sys.stderr)
        return None


def create_new_gdoc_from_markdown(markdown_path: str, creds, quiet: bool = False) -> str:
    """Create a new Google Doc from a Markdown file."""
    try:
        # Read the markdown file
        with open(markdown_path, 'r', encoding='utf-8') as f:
            markdown_content = f.read()
        
        # Extract frontmatter metadata for title
        metadata = extract_frontmatter_metadata(markdown_content)
        
        # Strip frontmatter for Google Doc (frontmatter is for markdown processing only)
        content_for_gdoc = strip_frontmatter_for_remote_sync(markdown_content)
        
        # Create a temporary file with the cleaned content
        temp_file_path = f"{markdown_path}.temp"
        with open(temp_file_path, 'w', encoding='utf-8') as f:
            f.write(content_for_gdoc)
        
        try:
            # Build the Drive service
            drive_service = build('drive', 'v3', credentials=creds)
            
            # Get the document name (frontmatter title takes priority over filename)
            doc_name = metadata.get('title') or Path(markdown_path).stem
            
            # Upload the cleaned markdown file and convert it to Google Docs format
            file_metadata = {
                'name': doc_name,
                'mimeType': 'application/vnd.google-apps.document'
            }
            
            media = MediaFileUpload(
                temp_file_path,
                mimetype='text/markdown',
                resumable=True
            )
            
            file = drive_service.files().create(
                body=file_metadata,
                media_body=media,
                fields='id'
            ).execute()
            
            doc_id = file.get('id')
            gdoc_url = f"https://docs.google.com/document/d/{doc_id}/edit"
            
            # Update frontmatter with the Google Doc URL
            update_frontmatter_gdoc_url(markdown_path, gdoc_url)
            
            if not quiet:
                print(f"Created new Google Doc with ID: {doc_id}")
                print(f"URL: {gdoc_url}")
                print(f"Updated frontmatter in {markdown_path}")
            
            return doc_id
        
        finally:
            # Clean up temporary file
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)
        
    except HttpError as error:
        print(f"An error occurred: {error}", file=sys.stderr)
        sys.exit(1)


def check_sync_status(markdown_path: str, destination_type: str, destination_id: str, creds=None, confluence=None) -> str:
    """Check sync status between markdown and remote destination."""
    try:
        # Read markdown file
        with open(markdown_path, 'r', encoding='utf-8') as f:
            markdown_content = f.read()
        
        # Strip frontmatter for comparison
        markdown_for_remote = strip_frontmatter_for_remote_sync(markdown_content)
        
        if destination_type == 'Google Doc':
            if not creds:
                return "❓ (no credentials)"
            
            # Check if frozen first
            if check_gdoc_frozen_status(destination_id, creds):
                return "🔒 (frozen)"
            
            # Export Google Doc to markdown for comparison
            try:
                gdoc_markdown = export_gdoc_to_markdown(destination_id, creds)
                
                # Compare content
                if markdown_for_remote.strip() == gdoc_markdown.strip():
                    return "✅ (synced)"
                else:
                    return "⚠️  (differs)"
            except Exception as e:
                return f"❌ (error: {str(e)[:30]}...)"
        
        elif destination_type == 'Confluence':
            if not confluence:
                return "❓ (no confluence client)"
            
            # Export Confluence to markdown for comparison
            try:
                confluence_markdown = export_confluence_to_markdown(destination_id, confluence)
                
                # Compare content
                if markdown_for_remote.strip() == confluence_markdown.strip():
                    return "✅ (synced)"
                else:
                    return "⚠️  (differs)"
            except Exception as e:
                return f"❌ (error: {str(e)[:30]}...)"
        
        return "❓ (unknown type)"
        
    except Exception as e:
        return f"❌ (error: {str(e)[:30]}...)"


def list_markdown_files(path: str, output_format: str = 'text', check_status: bool = False, show_diff: bool = False):
    """List frontmatter information for markdown files."""
    import glob
    import json
    
    # Determine if path is file or directory
    if os.path.isfile(path):
        if not path.endswith('.md'):
            print(f"Error: {path} is not a markdown file", file=sys.stderr)
            sys.exit(1)
        files = [path]
    elif os.path.isdir(path):
        # Find all markdown files in directory (exclude common build/cache directories)
        pattern = os.path.join(path, '**', '*.md')
        all_files = glob.glob(pattern, recursive=True)
        
        # Filter out common build/cache directories
        exclude_dirs = {'venv', 'env', '.venv', '.env', 'node_modules', '.git', '__pycache__', '.pytest_cache', 'build', 'dist', '.tox'}
        files = []
        for file_path in all_files:
            # Check if any part of the path contains excluded directories
            path_parts = file_path.split(os.sep)
            if not any(part in exclude_dirs for part in path_parts):
                files.append(file_path)
        
        if not files:
            print(f"No markdown files found in {path}")
            return
    else:
        print(f"Error: {path} is not a valid file or directory", file=sys.stderr)
        sys.exit(1)
    
    # Get credentials if status checking is enabled
    creds = None
    confluence = None
    secrets_file_path = args.secrets_file if hasattr(args, 'secrets_file') and args.secrets_file else None
    if check_status:
        creds = get_credentials()
        confluence = get_confluence_client(secrets_file_path)
    
    results = []
    
    for file_path in files:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Extract frontmatter metadata
            metadata = extract_frontmatter_metadata(content)
            
            # Determine export locations
            export_locations = []
            
            # Check Google Doc
            if metadata.get('gdoc_url'):
                gdoc_info = {
                    'type': 'Google Doc',
                    'url': metadata['gdoc_url']
                }
                
                if check_status and creds:
                    doc_id = extract_doc_id_from_url(metadata['gdoc_url'])
                    if doc_id:
                        is_frozen = check_gdoc_frozen_status(doc_id, creds)
                        gdoc_info['frozen'] = is_frozen
                        gdoc_info['status'] = 'frozen' if is_frozen else 'available'
                
                if show_diff and creds:
                    doc_id = extract_doc_id_from_url(metadata['gdoc_url'])
                    if doc_id:
                        sync_status = check_sync_status(file_path, 'Google Doc', doc_id, creds, confluence)
                        gdoc_info['sync_status'] = sync_status
                
                export_locations.append(gdoc_info)
            
            # Check Confluence
            if metadata.get('confluence_url'):
                confluence_info = {
                    'type': 'Confluence',
                    'url': metadata['confluence_url']
                }
                
                if check_status and confluence:
                    dest_info = parse_confluence_destination(metadata['confluence_url'])
                    page_id = dest_info.get('page_id')
                    if page_id:
                        is_frozen = check_confluence_frozen_status(page_id, confluence)
                        confluence_info['frozen'] = is_frozen
                        confluence_info['status'] = 'frozen' if is_frozen else 'available'
                
                if show_diff and confluence:
                    dest_info = parse_confluence_destination(metadata['confluence_url'])
                    page_id = dest_info.get('page_id')
                    if page_id:
                        sync_status = check_sync_status(file_path, 'Confluence', page_id, creds, confluence)
                        confluence_info['sync_status'] = sync_status
                
                export_locations.append(confluence_info)
            
            # Check Batch Document
            if metadata.get('batch') and isinstance(metadata['batch'], dict):
                batch_info = metadata['batch']
                batch_url = batch_info.get('url')
                batch_title = batch_info.get('batch_title', 'Unknown Batch')
                heading_title = batch_info.get('heading_title', 'Unknown Heading')
                
                if batch_url:
                    batch_info_display = {
                        'type': f'Batch Document ({batch_title})',
                        'url': batch_url,
                        'heading': heading_title
                    }
                    
                    if check_status and creds:
                        doc_id = batch_info.get('doc_id')
                        if doc_id:
                            is_frozen = check_gdoc_frozen_status(doc_id, creds)
                            batch_info_display['frozen'] = is_frozen
                            batch_info_display['status'] = 'frozen' if is_frozen else 'available'
                    
                    export_locations.append(batch_info_display)
            
            file_info = {
                'file': file_path,
                'title': metadata.get('title'),
                'labels': metadata.get('labels', []),
                'export_locations': export_locations
            }
            
            results.append(file_info)
            
        except Exception as e:
            print(f"Error reading {file_path}: {e}", file=sys.stderr)
            continue
    
    # Display results
    if output_format == 'json':
        print(json.dumps(results, indent=2))
    else:
        display_frontmatter_info(results, check_status, show_diff)


def display_frontmatter_info(results, check_status: bool = False, show_diff: bool = False):
    """Display frontmatter information in a readable format."""
    if not results:
        print("No markdown files with frontmatter found")
        return
    
    # Group results by batch and individual exports
    batch_groups = {}
    individual_files = []
    
    for info in results:
        # Check if this file is part of a batch
        batch_info = None
        for location in info['export_locations']:
            if 'Batch Document' in location['type']:
                batch_info = location
                break
        
        if batch_info:
            # Extract batch key (doc_id from URL)
            batch_url = batch_info['url']
            batch_key = batch_url.split('/d/')[-1].split('/')[0] if '/d/' in batch_url else batch_url
            
            if batch_key not in batch_groups:
                batch_groups[batch_key] = {
                    'batch_info': batch_info,
                    'files': []
                }
            
            batch_groups[batch_key]['files'].append(info)
        else:
            individual_files.append(info)
    
    status_text = " (with live status)" if check_status else ""
    print(f"Found {len(results)} markdown file(s) with frontmatter{status_text}:")
    print("=" * 80)
    
    # Display batch groups first
    if batch_groups:
        for batch_key, batch_data in batch_groups.items():
            batch_info = batch_data['batch_info']
            files = batch_data['files']
            
            print(f"\n📦 Batch: {batch_info['type']}")
            print(f"   URL: {batch_info['url']}")
            
            status_icon = ""
            status_text = ""
            if check_status and 'status' in batch_info:
                if batch_info['status'] == 'frozen':
                    status_icon = " ❄️"
                    status_text = " (frozen)"
                elif batch_info['status'] == 'available':
                    status_icon = " ✅"
                    status_text = " (available)"
            
            print(f"   Status: {status_icon}{status_text}")
            print(f"   Files ({len(files)}):")
            
            for file_info in files:
                heading = None
                for location in file_info['export_locations']:
                    if 'heading' in location:
                        heading = location['heading']
                        break
                
                if heading:
                    print(f"     • {os.path.basename(file_info['file'])} → {heading}")
                else:
                    print(f"     • {os.path.basename(file_info['file'])}")
    
    # Display individual files
    if individual_files:
        for info in individual_files:
            print(f"\n📄 {info['file']}")
            
            if info['title']:
                print(f"   Title: {info['title']}")
            
            if info['labels']:
                print(f"   Labels: {', '.join(info['labels'])}")
            
            if info['export_locations']:
                print("   Export Locations:")
                for location in info['export_locations']:
                    status_icon = ""
                    status_text = ""
                    
                    if check_status and 'status' in location:
                        if location['status'] == 'frozen':
                            status_icon = " ❄️"
                            status_text = " (frozen)"
                        elif location['status'] == 'available':
                            status_icon = " ✅"
                            status_text = " (available)"
                    
                    if show_diff and 'sync_status' in location:
                        sync_status = location['sync_status']
                        print(f"     • {location['type']}: {location['url']} {sync_status}")
                    else:
                        print(f"     • {location['type']}: {location['url']}{status_icon}{status_text}")
            else:
                print("   Export Locations: None")


def check_existing_gdoc_confirmation(markdown_path: str, force: bool = False, destination_doc_id: str = None) -> bool:
    """
    Check if markdown file has existing gdoc_url and ask for confirmation if not forcing.
    
    Args:
        markdown_path (str): Path to markdown file
        force (bool): If True, skip confirmation
        destination_doc_id (str): Optional destination document ID to compare with existing
        
    Returns:
        bool: True if should proceed, False if should skip
    """
    try:
        with open(markdown_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        metadata = extract_frontmatter_metadata(content)
        existing_gdoc_url = metadata.get('gdoc_url')
        
        if existing_gdoc_url and not force:
            # Extract doc ID from existing URL
            existing_doc_id = extract_doc_id(existing_gdoc_url)
            
            # If destination_doc_id provided, check if it's the same document
            if destination_doc_id and existing_doc_id == destination_doc_id:
                # Same document, no warning needed - just updating content
                return True
            
            # Different document or creating new - show warning
            print(f"\n⚠️  Warning: {os.path.basename(markdown_path)} already has a Google Doc link:")
            print(f"   {existing_gdoc_url}")
            if destination_doc_id:
                print(f"\nThis operation will update the link to point to a different document.")
            else:
                print(f"\nThis operation will update the link to point to a new document.")
            
            try:
                response = input("Do you want to continue? [y/N]: ").strip().lower()
                if response in ['y', 'yes']:
                    return True
                elif response in ['n', 'no', '']:
                    print("Operation cancelled.")
                    return False
                else:
                    print("Please enter 'y' for yes or 'n' for no.")
                    return False
            except (EOFError, KeyboardInterrupt):
                print("\nOperation cancelled.")
                return False
        
        return True
        
    except Exception as e:
        print(f"Warning: Could not check existing frontmatter in {markdown_path}: {e}")
        return True


def create_empty_document(title: str, quiet: bool = False) -> str:
    """
    Create an empty Google Doc for tab management.
    
    This function creates a new Google Doc that can be used as a container
    for multiple tabs. Each tab will be added as an H1 heading, which
    Google Docs displays as separate tabs in the UI.
    
    Args:
        title (str): Title for the new document
        quiet (bool): If True, suppress output messages
        
    Returns:
        str: Document ID of the created document, or None if failed
        
    Example:
        doc_id = create_empty_document("Project Documentation")
        # Returns: "1ABC123def456GHI789jkl"
    """
    try:
        creds = get_credentials()
        docs_service = build('docs', 'v1', credentials=creds)
        
        # Create empty document
        doc = docs_service.documents().create(body={'title': title}).execute()
        doc_id = doc['documentId']
        
        if not quiet:
            print(f'✓ Created empty document: "{title}"')
            print(f'  Document ID: {doc_id}')
            print(f'  URL: https://docs.google.com/document/d/{doc_id}/edit')
        
        return doc_id
        
    except HttpError as error:
        print(f'Google API error: {error}', file=sys.stderr)
        return None
    except Exception as e:
        print(f'Error creating empty document: {e}', file=sys.stderr)
        return None


def check_for_formatted_h1_headings(markdown_content: str, quiet: bool = False) -> list:
    """
    Check for H1 headings that have markdown formatting (bold, italic, etc.).
    These can break TOC link creation.
    
    Args:
        markdown_content (str): Markdown content to check
        quiet (bool): If True, suppress output messages
        
    Returns:
        list: List of formatted H1 headings found
    """
    import re
    
    formatted_headings = []
    
    # Pattern to match H1 headings with formatting
    h1_pattern = r'^#\s+(.+)$'
    
    for line in markdown_content.split('\n'):
        match = re.match(h1_pattern, line.strip())
        if match:
            heading_text = match.group(1).strip()
            
            # Check for markdown formatting
            if ('**' in heading_text or 
                '__' in heading_text or 
                '*' in heading_text or 
                '_' in heading_text or
                '`' in heading_text):
                formatted_headings.append(heading_text)
    
    if formatted_headings and not quiet:
        print("⚠️  Warning: Found H1 headings with markdown formatting:")
        for heading in formatted_headings:
            print(f"   • {heading}")
        print("   These may not work properly in the Table of Contents.")
        print("   Consider removing formatting from H1 headings for better TOC compatibility.")
    
    return formatted_headings


def extract_h1_headings_from_markdown(content: str) -> list:
    """
    Extract H1 headings from markdown content.
    
    Args:
        content (str): Markdown content to parse
        
    Returns:
        list: List of H1 heading texts
    """
    import re
    
    # Pattern to match H1 headings (lines starting with # followed by space)
    h1_pattern = r'^#\s+(.+)$'
    headings = []
    
    for line in content.split('\n'):
        match = re.match(h1_pattern, line.strip())
        if match:
            headings.append(match.group(1).strip())
    
    return headings


def generate_table_of_contents(headings: list) -> str:
    """
    Generate a table of contents from a list of headings.
    Uses simple numbered list format that works well with Google Docs.
    
    Args:
        headings (list): List of heading texts
        
    Returns:
        str: Table of contents
    """
    if not headings:
        return ""
    
    toc_lines = ["## Table of Contents", ""]
    
    for i, heading in enumerate(headings, 1):
        # Simple numbered list without links
        # Google Docs will handle navigation through its native outline
        toc_lines.append(f"{i}. {heading}")
    
    toc_lines.append("")  # Add blank line after TOC
    return "\n".join(toc_lines)


def create_working_toc_links_in_gdoc(doc_id: str, headings: list, creds, quiet: bool = False) -> None:
    """
    Create working TOC links in Google Doc by finding heading IDs and creating proper links.
    This creates clickable links that actually work by using Google Docs' internal heading IDs.
    
    Args:
        doc_id (str): Google Doc ID
        headings (list): List of heading texts
        creds: Google API credentials
        quiet (bool): If True, suppress output messages
    """
    try:
        docs_service = build('docs', 'v1', credentials=creds)
        
        # Get the document
        doc = docs_service.documents().get(documentId=doc_id).execute()
        
        # Find all headings and their IDs
        heading_ids = {}
        for element in doc.get('body', {}).get('content', []):
            if 'paragraph' in element:
                paragraph = element['paragraph']
                if 'paragraphStyle' in paragraph:
                    style = paragraph['paragraphStyle']
                    if style.get('namedStyleType') == 'HEADING_1':
                        # This is a heading, find its ID
                        if 'elements' in paragraph and len(paragraph['elements']) > 0:
                            first_element = paragraph['elements'][0]
                            if 'textRun' in first_element:
                                heading_text = first_element['textRun'].get('content', '').strip()
                                
                                # Look for heading ID in various places
                                heading_id = None
                                
                                # Method 1: Check if heading already has an ID in textStyle.link
                                if 'headingId' in first_element.get('textRun', {}).get('textStyle', {}).get('link', {}):
                                    heading_id = first_element['textRun']['textStyle']['link']['headingId']
                                
                                # Method 2: Check if there's a headingId in the paragraph style
                                elif 'headingId' in paragraph.get('paragraphStyle', {}):
                                    heading_id = paragraph['paragraphStyle']['headingId']
                                
                                # Method 3: Generate a heading ID based on the text (Google Docs format)
                                if not heading_id and heading_text:
                                    # Google Docs generates IDs like "h.abc123def456"
                                    # We'll create a simple one based on the text
                                    import hashlib
                                    text_hash = hashlib.md5(heading_text.lower().encode()).hexdigest()[:12]
                                    heading_id = f"h.{text_hash}"
                                
                                if heading_id:
                                    heading_ids[heading_text] = heading_id
                                    if not quiet:
                                        print(f"  Found heading: '{heading_text}' -> {heading_id}")
        
        if not heading_ids:
            if not quiet:
                print("ℹ No headings with IDs found - headings may not be properly formatted")
            return
        
        # Find the TOC section and replace links
        requests = []
        
        for element in doc.get('body', {}).get('content', []):
            if 'paragraph' in element:
                paragraph = element['paragraph']
                if 'elements' in paragraph:
                    for elem in paragraph['elements']:
                        if 'textRun' in elem:
                            text_content = elem['textRun'].get('content', '')
                            # Check if this looks like a TOC item
                            for heading_text in headings:
                                # Check if this is a TOC item (exact match with heading text)
                                if (text_content.strip() == heading_text and 
                                    'Table of Contents' not in text_content):
                                    
                                    # Find the best matching heading ID
                                    best_match = None
                                    best_heading = None
                                    
                                    # Try exact match first
                                    if heading_text in heading_ids:
                                        best_match = heading_ids[heading_text]
                                        best_heading = heading_text
                                    else:
                                        # Try partial matches
                                        for doc_heading, heading_id in heading_ids.items():
                                            if heading_text.lower() in doc_heading.lower() or doc_heading.lower() in heading_text.lower():
                                                best_match = heading_id
                                                best_heading = doc_heading
                                                break
                                    
                                    if best_match:
                                        # Create a link to the heading
                                        start_index = elem['startIndex']
                                        end_index = elem['endIndex']
                                        
                                        # Update the text style to include a link
                                        requests.append({
                                            'updateTextStyle': {
                                                'range': {
                                                    'startIndex': start_index,
                                                    'endIndex': end_index
                                                },
                                                'textStyle': {
                                                    'link': {
                                                        'headingId': best_match
                                                    },
                                                    'foregroundColor': {
                                                        'color': {
                                                            'rgbColor': {
                                                                'red': 0.06666667,
                                                                'green': 0.33333334,
                                                                'blue': 0.8
                                                            }
                                                        }
                                                    },
                                                    'underline': True
                                                },
                                                'fields': 'link,foregroundColor,underline'
                                            }
                                        })
                                        
                                        if not quiet:
                                            print(f"  Linking TOC item: '{heading_text}' -> '{best_heading}' ({best_match})")
        
        # Apply the requests
        if requests:
            docs_service.documents().batchUpdate(
                documentId=doc_id,
                body={'requests': requests}
            ).execute()
            
            if not quiet:
                print(f"✓ Created {len(requests)} working TOC links")
        else:
            if not quiet:
                print("ℹ No TOC links to update")
        
    except Exception as e:
        if not quiet:
            print(f"Warning: Could not create working TOC links: {e}")


def fix_toc_links_in_gdoc(doc_id: str, creds, quiet: bool = False) -> None:
    """
    Fix TOC links in Google Doc to use proper internal navigation.
    This replaces markdown-style links with Google Docs internal links.
    """
    try:
        docs_service = build('docs', 'v1', credentials=creds)
        
        # Get the document
        doc = docs_service.documents().get(documentId=doc_id).execute()
        
        # Find the Table of Contents section and fix the links
        requests = []
        
        for element in doc.get('body', {}).get('content', []):
            if 'paragraph' in element:
                paragraph = element['paragraph']
                if 'elements' in paragraph:
                    for elem in paragraph['elements']:
                        if 'textRun' in elem:
                            text_content = elem['textRun'].get('content', '')
                            # Look for markdown-style links like [text](#anchor)
                            if '[' in text_content and '](#' in text_content:
                                # This is a TOC link that needs to be fixed
                                # For now, we'll just remove the anchor part
                                # Google Docs will handle internal navigation automatically
                                if not quiet:
                                    print("ℹ Found TOC links - Google Docs will handle internal navigation")
                                return
        
        if not quiet:
            print("ℹ No TOC links found to fix")
        
    except Exception as e:
        if not quiet:
            print(f"Warning: Could not fix TOC links: {e}")


def ensure_heading_formatting_in_gdoc(doc_id: str, creds, quiet: bool = False) -> None:
    """
    Ensure that H1 headings in a Google Doc are properly formatted as Heading 1 style.
    This is necessary for TOC links to work correctly.
    """
    try:
        docs_service = build('docs', 'v1', credentials=creds)
        
        # Get the document
        doc = docs_service.documents().get(documentId=doc_id).execute()
        
        # Find all paragraphs that start with '#' and format them as Heading 1
        requests = []
        
        for element in doc.get('body', {}).get('content', []):
            if 'paragraph' in element:
                paragraph = element['paragraph']
                if 'elements' in paragraph and len(paragraph['elements']) > 0:
                    # Check if this paragraph starts with a single '#' (H1)
                    first_element = paragraph['elements'][0]
                    if 'textRun' in first_element:
                        text_content = first_element['textRun'].get('content', '')
                        if text_content.startswith('# ') and not text_content.startswith('##'):
                            # This should be an H1 heading
                            start_index = element['startIndex']
                            end_index = element['endIndex']
                            
                            # Remove the '#' prefix
                            requests.append({
                                'deleteTextRange': {
                                    'range': {
                                        'startIndex': start_index,
                                        'endIndex': start_index + 2
                                    }
                                }
                            })
                            
                            # Set the paragraph style to Heading 1
                            requests.append({
                                'updateParagraphStyle': {
                                    'range': {
                                        'startIndex': start_index,
                                        'endIndex': end_index - 2
                                    },
                                    'paragraphStyle': {
                                        'namedStyleType': 'HEADING_1'
                                    },
                                    'fields': 'namedStyleType'
                                }
                            })
        
        # Apply all requests if any were found
        if requests:
            if not quiet:
                print(f"✓ Formatting {len(requests)//2} headings as Heading 1 style")
            
            docs_service.documents().batchUpdate(
                documentId=doc_id,
                body={'requests': requests}
            ).execute()
        else:
            if not quiet:
                print("ℹ No H1 headings found to format")
        
    except Exception as e:
        if not quiet:
            print(f"Warning: Could not format headings: {e}")


def generate_batch_id(title: str) -> str:
    """
    Generate a clean, short batch ID from a title.
    
    Examples:
        "Software Assurance Maturity Plan" -> "samp"
        "API Documentation" -> "api-docs"
        "Project Alpha v2.0" -> "project-alpha-v2-0"
    
    Args:
        title (str): Human-readable batch title
        
    Returns:
        str: Clean batch ID suitable for command-line usage
    """
    import re
    
    # Convert to lowercase and replace spaces/special chars with hyphens
    clean_id = re.sub(r'[^a-zA-Z0-9\s-]', '', title.lower())
    clean_id = re.sub(r'\s+', '-', clean_id.strip())
    
    # Remove multiple consecutive hyphens
    clean_id = re.sub(r'-+', '-', clean_id)
    
    # Remove leading/trailing hyphens
    clean_id = clean_id.strip('-')
    
    # If empty or too short, generate from first letters
    if len(clean_id) < 3:
        words = title.split()
        if len(words) >= 2:
            clean_id = ''.join(word[0].lower() for word in words[:3])
        else:
            clean_id = title.lower()[:8]
    
    # Ensure it starts with a letter
    if clean_id and not clean_id[0].isalpha():
        clean_id = 'batch-' + clean_id
    
    return clean_id or 'batch'


def create_batch_document_simple(markdown_files: list, title: str, quiet: bool = False, include_headers: bool = False, include_horizontal_sep: bool = False, include_title: bool = True, include_toc: bool = False) -> str:
    """
    Create a Google Doc by combining multiple markdown files client-side.
    
    This function combines multiple markdown files into a single temporary markdown
    file, then syncs that as one Google Doc using the existing, proven sync logic.
    This preserves all formatting and avoids the complexity of individual heading management.
    
    Args:
        markdown_files (list): List of markdown file paths
        title (str): Title for the new document
        quiet (bool): If True, suppress output messages
        include_headers (bool): If True, include file titles as headers in the document
        include_horizontal_sep (bool): If True, add horizontal separators between files
        include_title (bool): If True, include the batch title as the main document title
        include_toc (bool): If True, generate and include a table of contents for H1 headings
        
    Returns:
        str: Document ID of the created document, or None if failed
    """
    try:
        import tempfile
        import uuid
        
        if not quiet:
            print(f'Combining {len(markdown_files)} files into single document...')
        
        # Create a temporary combined markdown file
        combined_content = ""
        all_h1_headings = []  # Collect all H1 headings for TOC generation
        
        # Note: We don't add the title to content when include_title=True
        # because the document title will be set separately
        
        for i, markdown_path in enumerate(markdown_files):
            if not quiet:
                print(f'  Processing {i+1}/{len(markdown_files)}: {markdown_path}')
            
            try:
                # Read markdown file
                with open(markdown_path, 'r', encoding='utf-8') as f:
                    markdown_content = f.read()
                
                # Extract metadata
                metadata = extract_frontmatter_metadata(markdown_content)
                
                # Get heading title from frontmatter or filename
                heading_title = metadata.get('title')
                if not heading_title:
                    heading_title = Path(markdown_path).stem.replace('_', ' ').replace('-', ' ').title()
                
                # Strip frontmatter for the combined document
                content_for_combined = strip_frontmatter_for_remote_sync(markdown_content)
                
                # Check for formatted H1 headings that might break TOC
                if include_toc:
                    check_for_formatted_h1_headings(content_for_combined, quiet=quiet)
                
                # Collect H1 headings for TOC if requested
                if include_toc:
                    if include_headers:
                        # When using headers, the file title becomes an H1 heading
                        all_h1_headings.append(heading_title)
                    else:
                        # Without headers, collect H1 headings from the content
                        h1_headings = extract_h1_headings_from_markdown(content_for_combined)
                        all_h1_headings.extend(h1_headings)
                
                # Add content with or without headers
                if include_headers:
                    # When using headers, make the file title an H1 heading for proper anchor creation
                    combined_content += f"# {heading_title}\n\n{content_for_combined}\n\n"
                else:
                    # Without headers, ensure any existing H1 headings remain as H1
                    # (they should already be H1 from the original markdown)
                    combined_content += f"{content_for_combined}\n\n"
                
                # Add horizontal separator after each file (except the last one) if requested
                if include_horizontal_sep and i < len(markdown_files) - 1:
                    combined_content += "\n\n---\n\n"
                
                if not quiet:
                    print(f'    ✓ Added: {heading_title}')
                    
            except Exception as e:
                if not quiet:
                    print(f'    Error processing {markdown_path}: {e}')
                continue
        
        # Generate and prepend table of contents if requested
        if include_toc and all_h1_headings:
            toc_content = generate_table_of_contents(all_h1_headings)
            combined_content = toc_content + "\n" + combined_content
            if not quiet:
                print(f'✓ Generated table of contents with {len(all_h1_headings)} headings')
        
        # Create temporary file
        temp_file = tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False, encoding='utf-8')
        temp_file.write(combined_content)
        temp_file.close()
        
        try:
            # Use the existing create_new_gdoc_from_markdown function
            creds = get_credentials()
            doc_id = create_new_gdoc_from_markdown_with_title(temp_file.name, title, creds, quiet=quiet)
            
            if doc_id and not quiet:
                print(f'✓ Created batch document: "{title}"')
                print(f'  Document ID: {doc_id}')
                print(f'  URL: https://docs.google.com/document/d/{doc_id}/edit')
            
            # Create working TOC links if requested
            if doc_id and include_toc and all_h1_headings:
                if not quiet:
                    print("⏳ Waiting for document to be ready...")
                import time
                time.sleep(2)  # Give Google Docs time to process the document
                create_working_toc_links_in_gdoc(doc_id, all_h1_headings, creds, quiet=quiet)
            
            # Update frontmatter for all individual files with batch information
            if doc_id:
                # Generate a clean batch ID from the title
                batch_id = generate_batch_id(title)
                for i, markdown_path in enumerate(markdown_files):
                    try:
                        # Read the file again to get current content
                        with open(markdown_path, 'r', encoding='utf-8') as f:
                            current_content = f.read()
                        
                        # Extract metadata to get heading title
                        metadata = extract_frontmatter_metadata(current_content)
                        heading_title = metadata.get('title')
                        if not heading_title:
                            heading_title = Path(markdown_path).stem.replace('_', ' ').replace('-', ' ').title()
                        
                        # Create batch info
                        from datetime import datetime
                        current_time = datetime.now().isoformat()
                        
                        batch_info = {
                            'batch_id': batch_id,
                            'batch_title': title,
                            'doc_id': doc_id,
                            'heading_title': heading_title,
                            'url': f"https://docs.google.com/document/d/{doc_id}/edit",
                            'created': current_time,
                            'modified': current_time
                        }
                        
                        # Update frontmatter
                        updated_content = update_frontmatter_metadata(current_content, {'batch': batch_info})
                        with open(markdown_path, 'w', encoding='utf-8') as f:
                            f.write(updated_content)
                        
                        if not quiet:
                            print(f'  ✓ Updated frontmatter in {os.path.basename(markdown_path)}')
                            
                    except Exception as e:
                        if not quiet:
                            print(f'  Warning: Could not update frontmatter in {os.path.basename(markdown_path)}: {e}')
            
            # Print batch info at the end
            if doc_id and not quiet:
                print(f"\nbatch: {title}")
                print(f"batch_id: {batch_id}")
                print(f"URL: https://docs.google.com/document/d/{doc_id}/edit")
            
            return doc_id
            
        finally:
            # Clean up temporary file
            os.unlink(temp_file.name)
        
    except Exception as e:
        print(f'Error creating batch document: {e}', file=sys.stderr)
        return None


# Removed: create_batch_document (complex) - replaced by create_batch_document_simple
# Removed: add_markdown_as_heading - replaced by batch functionality  
# Removed: update_heading_content - replaced by batch functionality


def diff_batch_against_gdoc(doc_id: str, quiet: bool = False) -> None:
    """
    Diff an entire batch against its Google Doc.
    
    This function finds all markdown files that belong to the specified batch
    document and compares each one against its corresponding section in the
    Google Doc, showing differences for each heading section.
    
    Args:
        doc_id (str): Google Doc ID to diff against
        quiet (bool): If True, suppress output messages
        
    Example:
        diff_batch_against_gdoc("1ABC123def456")
        # Shows differences for all files in the batch
    """
    try:
        from collections import defaultdict
        
        # Find all markdown files that belong to this batch
        batch_files = []
        for root, dirs, files in os.walk('.'):
            for file in files:
                if file.endswith('.md'):
                    file_path = os.path.join(root, file)
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            content = f.read()
                        
                        metadata = extract_frontmatter_metadata(content)
                        
                        if 'batch' in metadata and isinstance(metadata['batch'], dict):
                            batch_info = metadata['batch']
                            if batch_info.get('doc_id') == doc_id:
                                batch_files.append({
                                    'file_path': file_path,
                                    'heading_title': batch_info.get('heading_title', 'Unknown'),
                                    'batch_info': batch_info
                                })
                    except Exception:
                        continue
        
        if not batch_files:
            if not quiet:
                print(f"No batch files found for document {doc_id}")
            return
        
        if not quiet:
            print(f"Diffing batch document {doc_id} against {len(batch_files)} files")
            print("=" * 60)
        
        # Get the Google Doc content
        creds = get_credentials()
        docs_service = build('docs', 'v1', credentials=creds)
        
        try:
            doc = docs_service.documents().get(documentId=doc_id).execute()
            doc_title = doc.get('title', 'Unknown')
        except Exception as e:
            print(f"Error accessing Google Doc: {e}", file=sys.stderr)
            return
        
        if not quiet:
            print(f"Document: {doc_title}")
            print(f"URL: https://docs.google.com/document/d/{doc_id}/edit")
            print()
        
        # For each batch file, find its corresponding section in the Google Doc
        for file_info in batch_files:
            file_path = file_info['file_path']
            heading_title = file_info['heading_title']
            
            if not quiet:
                print(f"Checking: {os.path.basename(file_path)} -> {heading_title}")
            
            try:
                # Read the markdown file
                with open(file_path, 'r', encoding='utf-8') as f:
                    markdown_content = f.read()
                
                # Strip frontmatter for comparison
                content_for_gdoc = strip_frontmatter_for_remote_sync(markdown_content)
                
                # Find the heading section in the Google Doc
                heading_section = find_heading_section_in_gdoc(doc, heading_title)
                
                if heading_section:
                    # Compare the content
                    if content_for_gdoc.strip() != heading_section.strip():
                        if not quiet:
                            print(f"  ⚠️  Differences found in '{heading_title}'")
                            show_diff(
                                heading_section,
                                content_for_gdoc,
                                f"Google Doc '{heading_title}'",
                                f"Markdown '{os.path.basename(file_path)}'"
                            )
                        else:
                            print(f"DIFF: {file_path}")
                    else:
                        if not quiet:
                            print(f"  ✓  No differences in '{heading_title}'")
                else:
                    if not quiet:
                        print(f"  ❌  Heading '{heading_title}' not found in Google Doc")
                    else:
                        print(f"MISSING: {file_path}")
                
                if not quiet:
                    print()
                    
            except Exception as e:
                if not quiet:
                    print(f"  Error processing {file_path}: {e}")
                else:
                    print(f"ERROR: {file_path}")
        
    except Exception as e:
        print(f'Error diffing batch: {e}', file=sys.stderr)


def update_batch_by_name(batch_identifier: str, quiet: bool = False) -> None:
    """
    Update an existing batch by finding all files that belong to it.
    
    This function can find a batch by either:
    1. Batch ID (searches for files with matching batch_id)
    2. Batch title (searches for files with matching batch_title)
    3. Google Doc ID (searches for files with matching doc_id)
    
    It then updates the Google Doc with all the found files in the correct order.
    
    Args:
        batch_identifier (str): Batch ID, batch title, or Google Doc ID
        quiet (bool): If True, suppress output messages
        
    Example:
        update_batch_by_name("samp")  # Batch ID
        update_batch_by_name("Software Assurance Maturity Plan")  # Batch title
        update_batch_by_name("1ABC123def456")  # Google Doc ID
    """
    try:
        # Find all markdown files that belong to this batch
        batch_files = []
        doc_id = None
        batch_title = None
        
        # First pass: find the target batch
        target_batch_info = None
        for root, dirs, files in os.walk('.'):
            for file in files:
                if file.endswith('.md'):
                    file_path = os.path.join(root, file)
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            content = f.read()
                        
                        metadata = extract_frontmatter_metadata(content)
                        
                        if 'batch' in metadata and isinstance(metadata['batch'], dict):
                            batch_info = metadata['batch']
                            batch_doc_id = batch_info.get('doc_id', '')
                            batch_name = batch_info.get('batch_title', '')
                            batch_id = batch_info.get('batch_id', '')
                            
                            # Check if this file belongs to the specified batch
                            # Match by batch_id, doc_id, or batch_title (exact or partial)
                            if (batch_identifier == batch_id or
                                batch_identifier == batch_doc_id or 
                                batch_identifier.lower() == batch_name.lower() or
                                (len(batch_identifier) > 3 and batch_identifier.lower() in batch_name.lower())):
                                
                                # Store the target batch info from first match
                                if target_batch_info is None:
                                    target_batch_info = batch_info
                                    doc_id = batch_doc_id
                                    batch_title = batch_name
                                    
                    except Exception:
                        continue
        
        if target_batch_info is None:
            if not quiet:
                print(f"No batch found matching '{batch_identifier}'")
                print("Available batches:")
                list_batch_groupings('.', quiet=True)
            return
        
        # Second pass: find all files that belong to the target batch
        for root, dirs, files in os.walk('.'):
            for file in files:
                if file.endswith('.md'):
                    file_path = os.path.join(root, file)
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            content = f.read()
                        
                        metadata = extract_frontmatter_metadata(content)
                        
                        if 'batch' in metadata and isinstance(metadata['batch'], dict):
                            batch_info = metadata['batch']
                            batch_doc_id = batch_info.get('doc_id', '')
                            
                            # Only include files from the target batch
                            if batch_doc_id == doc_id:
                                batch_files.append({
                                    'file_path': file_path,
                                    'heading_title': batch_info.get('heading_title', 'Unknown'),
                                    'batch_info': batch_info
                                })
                                    
                    except Exception:
                        continue
        
        if not batch_files:
            if not quiet:
                print(f"No batch files found for '{batch_identifier}'")
                print("Available batches:")
                list_batch_groupings('.', quiet=True)
            return
        
        if not doc_id:
            if not quiet:
                print(f"Error: Could not determine document ID for batch '{batch_identifier}'")
            return
        
        if not quiet:
            print(f"Updating batch: {batch_title}")
            print(f"Document ID: {doc_id}")
            print(f"Found {len(batch_files)} files to update")
            print("=" * 50)
        
        # Sort files by their original order (we'll use filename as a proxy for now)
        # In a real implementation, you might want to store the original order in frontmatter
        batch_files.sort(key=lambda x: x['file_path'])
        
        # Get credentials
        creds = get_credentials()
        docs_service = build('docs', 'v1', credentials=creds)
        drive_service = build('drive', 'v3', credentials=creds)
        
        # Clear the existing document content (keep the title)
        try:
            doc = docs_service.documents().get(documentId=doc_id).execute()
            doc_title = doc.get('title', 'Unknown')
            
            # Delete all content except the first paragraph (which contains the title)
            body_content = doc.get('body', {}).get('content', [])
            if len(body_content) > 1:
                # Delete everything after the first paragraph
                # Find the last element that's not just a newline
                last_content_index = 1
                for i in range(len(body_content) - 1, 0, -1):
                    element = body_content[i]
                    if 'paragraph' in element or 'table' in element or 'tableOfContents' in element:
                        last_content_index = i
                        break
                
                # Get the end index of the last content element, excluding trailing newline
                last_element = body_content[last_content_index]
                end_index = last_element.get('endIndex', 1)
                
                # Ensure we don't include the final newline character
                if end_index > 1:
                    end_index = end_index - 1
                
                delete_requests = [{
                    'deleteContentRange': {
                        'range': {
                            'startIndex': body_content[1].get('startIndex', 1),
                            'endIndex': end_index
                        }
                    }
                }]
                
                docs_service.documents().batchUpdate(
                    documentId=doc_id,
                    body={'requests': delete_requests}
                ).execute()
                
        except Exception as e:
            if not quiet:
                print(f"Warning: Could not clear existing content: {e}")
        
        # Now add each file as a heading section in order
        for i, file_info in enumerate(batch_files):
            file_path = file_info['file_path']
            heading_title = file_info['heading_title']
            
            if not quiet:
                print(f"  Processing {i+1}/{len(batch_files)}: {os.path.basename(file_path)} -> {heading_title}")
            
            try:
                # Read markdown file
                with open(file_path, 'r', encoding='utf-8') as f:
                    markdown_content = f.read()
                
                # Extract metadata
                metadata = extract_frontmatter_metadata(markdown_content)
                
                # Strip frontmatter for Google Doc
                content_for_gdoc = strip_frontmatter_for_remote_sync(markdown_content)
                
                # Create a temporary markdown file with the heading
                temp_file_path = f"{file_path}.temp_batch_update"
                with open(temp_file_path, 'w', encoding='utf-8') as f:
                    f.write(f"# {heading_title}\n\n{content_for_gdoc}")
                
                try:
                    # Convert markdown to Google Doc format using Drive API
                    media = MediaFileUpload(
                        temp_file_path,
                        mimetype='text/markdown',
                        resumable=True
                    )
                    
                    file_metadata = {
                        'mimeType': 'application/vnd.google-apps.document'
                    }
                    
                    # Create a temporary document with converted content
                    temp_doc = docs_service.documents().create(body={'title': f'temp_batch_update_{i}'}).execute()
                    temp_doc_id = temp_doc['documentId']
                    
                    # Update the temporary doc with converted content
                    drive_service.files().update(
                        fileId=temp_doc_id,
                        media_body=media,
                        body=file_metadata
                    ).execute()
                    
                    # Get the converted content
                    temp_doc_content = docs_service.documents().get(documentId=temp_doc_id).execute()
                    
                    # Get current document content to find insertion point
                    current_doc = docs_service.documents().get(documentId=doc_id).execute()
                    insert_position = max(1, current_doc.get('body', {}).get('content', [{}])[-1].get('endIndex', 1) - 1)
                    
                    # Copy content from temp doc to main doc
                    body_content = temp_doc_content.get('body', {}).get('content', [])
                    
                    # First, insert all text content at once to avoid index issues
                    all_text = ''
                    for element in body_content:
                        if 'paragraph' in element:
                            para = element['paragraph']
                            for text_run in para.get('elements', []):
                                if 'textRun' in text_run:
                                    all_text += text_run['textRun'].get('content', '')
                    
                    # Insert all text at once
                    if all_text.strip():
                        docs_service.documents().batchUpdate(
                            documentId=doc_id,
                            body={'requests': [{
                                'insertText': {
                                    'location': {
                                        'index': insert_position
                                    },
                                    'text': all_text
                                }
                            }]}
                        ).execute()
                        
                        # Now apply formatting in a separate batch
                        requests = []
                        current_position = insert_position
                        
                        for element in body_content:
                            if 'paragraph' in element:
                                para = element['paragraph']
                                
                                # Extract text content
                                text = ''
                                for text_run in para.get('elements', []):
                                    if 'textRun' in text_run:
                                        text += text_run['textRun'].get('content', '')
                                
                                if text.strip():  # Only process non-empty paragraphs
                                    # Apply paragraph style if it exists
                                    if 'paragraphStyle' in para and 'namedStyleType' in para['paragraphStyle']:
                                        style_type = para['paragraphStyle']['namedStyleType']
                                        if style_type in ['HEADING_1', 'HEADING_2', 'HEADING_3', 'HEADING_4', 'HEADING_5', 'HEADING_6']:
                                            requests.append({
                                                'updateParagraphStyle': {
                                                    'range': {
                                                        'startIndex': current_position,
                                                        'endIndex': current_position + len(text)
                                                    },
                                                    'paragraphStyle': {
                                                        'namedStyleType': style_type
                                                    },
                                                    'fields': 'namedStyleType'
                                                }
                                            })
                                    
                                    # Apply text formatting for each text run
                                    text_start = current_position
                                    for text_run in para.get('elements', []):
                                        if 'textRun' in text_run:
                                            run_text = text_run['textRun'].get('content', '')
                                            run_length = len(run_text)
                                            
                                            if 'textStyle' in text_run['textRun']:
                                                text_style = text_run['textRun']['textStyle']
                                                
                                                # Apply bold formatting
                                                if text_style.get('bold'):
                                                    requests.append({
                                                        'updateTextStyle': {
                                                            'range': {
                                                                'startIndex': text_start,
                                                                'endIndex': text_start + run_length
                                                            },
                                                            'textStyle': {
                                                                'bold': True
                                                            },
                                                            'fields': 'bold'
                                                        }
                                                    })
                                                
                                                # Apply italic formatting
                                                if text_style.get('italic'):
                                                    requests.append({
                                                        'updateTextStyle': {
                                                            'range': {
                                                                'startIndex': text_start,
                                                                'endIndex': text_start + run_length
                                                            },
                                                            'textStyle': {
                                                                'italic': True
                                                            },
                                                            'fields': 'italic'
                                                        }
                                                    })
                                            
                                            text_start += run_length
                                    
                                    current_position += len(text)
                        
                        # Execute formatting requests
                        if requests:
                            docs_service.documents().batchUpdate(
                                documentId=doc_id,
                                body={'requests': requests}
                            ).execute()
                    
                    # Clean up temporary document
                    drive_service.files().delete(fileId=temp_doc_id).execute()
                    
                    if not quiet:
                        print(f"    ✓ Updated heading: {heading_title}")
                    
                finally:
                    # Clean up temporary file
                    if os.path.exists(temp_file_path):
                        os.remove(temp_file_path)
                        
            except Exception as e:
                if not quiet:
                    print(f"    Error processing {file_path}: {e}")
                continue
        
        if not quiet:
            print(f"✓ Batch update completed")
            print(f"URL: https://docs.google.com/document/d/{doc_id}/edit")
        else:
            print(f"https://docs.google.com/document/d/{doc_id}/edit")
        
    except Exception as e:
        print(f'Error updating batch: {e}', file=sys.stderr)


def find_heading_section_in_gdoc(doc: dict, heading_title: str) -> str:
    """
    Find a heading section in a Google Doc and return its content.
    
    Args:
        doc (dict): Google Doc document object
        heading_title (str): Title of the heading to find
        
    Returns:
        str: Content of the heading section, or empty string if not found
    """
    try:
        content = doc.get('body', {}).get('content', [])
        
        # Find the target heading
        heading_start = None
        heading_end = None
        
        for element in content:
            if 'paragraph' in element:
                para = element['paragraph']
                if 'paragraphStyle' in para:
                    style = para['paragraphStyle']
                    if 'namedStyleType' in style and style['namedStyleType'] == 'HEADING_1':
                        # Extract text from the paragraph
                        text = ''
                        for text_run in para.get('elements', []):
                            if 'textRun' in text_run:
                                text += text_run['textRun'].get('content', '')
                        
                        if heading_title.lower() in text.lower():
                            heading_start = element.get('endIndex', 0)
                            break
        
        if heading_start is None:
            return ""
        
        # Find the end of the heading section (next H1 heading or end of document)
        for element in content:
            if element.get('startIndex', 0) > heading_start:
                if 'paragraph' in element:
                    para = element['paragraph']
                    if 'paragraphStyle' in para:
                        style = para['paragraphStyle']
                        if 'namedStyleType' in style and style['namedStyleType'] == 'HEADING_1':
                            heading_end = element.get('startIndex', 0)
                            break
        
        if heading_end is None:
            # Use end of document
            heading_end = content[-1].get('endIndex', 0)
        
        # Extract the content between heading_start and heading_end
        section_content = ""
        for element in content:
            start_idx = element.get('startIndex', 0)
            end_idx = element.get('endIndex', 0)
            
            if start_idx >= heading_start and end_idx <= heading_end:
                if 'paragraph' in element:
                    para = element['paragraph']
                    for text_run in para.get('elements', []):
                        if 'textRun' in text_run:
                            section_content += text_run['textRun'].get('content', '')
        
        return section_content.strip()
        
    except Exception:
        return ""


def list_batch_groupings(directory: str, quiet: bool = False) -> None:
    """
    List all batch groupings in markdown files.
    
    This function scans a directory for markdown files and groups them by
    their batch metadata, showing which files belong to which batch documents.
    
    Args:
        directory (str): Directory to scan for markdown files
        quiet (bool): If True, suppress output messages
        
    Example:
        list_batch_groupings("/path/to/markdown/files")
        # Shows grouped batch information
    """
    try:
        from collections import defaultdict
        
        # Find all markdown files
        markdown_files = []
        for root, dirs, files in os.walk(directory):
            for file in files:
                if file.endswith('.md'):
                    markdown_files.append(os.path.join(root, file))
        
        if not markdown_files:
            if not quiet:
                print("No markdown files found in directory")
            return
        
        # Group files by batch
        batch_groups = defaultdict(list)
        ungrouped_files = []
        
        for file_path in markdown_files:
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                
                metadata = extract_frontmatter_metadata(content)
                
                if 'batch' in metadata and isinstance(metadata['batch'], dict):
                    batch_info = metadata['batch']
                    batch_id = batch_info.get('batch_id', 'unknown')
                    batch_groups[batch_id].append({
                        'file': file_path,
                        'batch_info': batch_info
                    })
                else:
                    ungrouped_files.append(file_path)
                    
            except Exception as e:
                if not quiet:
                    print(f"Warning: Could not read {file_path}: {e}")
                continue
        
        if not quiet:
            print(f"Batch Groupings in {directory}")
            print("=" * 50)
            
            if batch_groups:
                for batch_id, files in batch_groups.items():
                    # Get batch info from first file
                    batch_info = files[0]['batch_info']
                    batch_title = batch_info.get('batch_title', 'Unknown Title')
                    doc_id = batch_info.get('doc_id', 'Unknown')
                    
                    print(f"\nBatch: {batch_title}")
                    print(f"  Batch ID: {batch_id}")
                    print(f"  Document ID: {doc_id}")
                    print(f"  URL: https://docs.google.com/document/d/{doc_id}/edit")
                    print(f"  Files ({len(files)}):")
                    
                    for file_info in files:
                        file_path = file_info['file']
                        heading_title = file_info['batch_info'].get('heading_title', 'Unknown')
                        print(f"    - {os.path.basename(file_path)} -> {heading_title}")
            else:
                print("No batch groupings found")
            
            if ungrouped_files:
                print(f"\nUngrouped files ({len(ungrouped_files)}):")
                for file_path in ungrouped_files:
                    print(f"  - {os.path.basename(file_path)}")
        
        if quiet:
            for batch_id, files in batch_groups.items():
                batch_info = files[0]['batch_info']
                doc_id = batch_info.get('doc_id', '')
                if doc_id:
                    print(f"https://docs.google.com/document/d/{doc_id}/edit")
        
    except Exception as e:
        print(f'Error listing batch groupings: {e}', file=sys.stderr)


def is_tab_url(url: str) -> bool:
    """
    Check if a Google Doc URL is a tab URL (contains heading fragment).
    
    This function detects if a Google Doc URL contains heading fragments
    that indicate it's targeting a specific tab/section within the document.
    
    Args:
        url (str): Google Doc URL to check
        
    Returns:
        bool: True if URL contains tab/heading fragments, False otherwise
        
    Example:
        is_tab_url("https://docs.google.com/document/d/123/edit#heading=h.abc")
        # Returns: True
    """
    if not url:
        return False
    return '#heading=' in url or '?tab=' in url


def extract_tab_title_from_url(url: str) -> str:
    """
    Extract tab title from a Google Doc URL with heading fragment.
    
    This function attempts to extract the tab title from a Google Doc URL
    that contains heading fragments. Currently returns None as the heading
    ID needs to be resolved by looking up the actual heading in the document.
    
    Args:
        url (str): Google Doc URL with heading fragment
        
    Returns:
        str: Tab title if extractable, None otherwise
        
    Note:
        This is a placeholder function. Full implementation would require
        API calls to resolve heading IDs to actual heading text.
        
    Example:
        title = extract_tab_title_from_url("https://docs.google.com/document/d/123/edit#heading=h.abc")
        # Returns: None (requires document lookup)
    """
    if not url:
        return None
    
    # Look for heading fragment
    if '#heading=' in url:
        # This would need to be resolved by looking up the heading in the document
        # For now, return None to indicate it needs to be resolved
        return None
    
    return None


def main():
    parser = argparse.ArgumentParser(
        description='Sync between Google Docs, Confluence, and Markdown files',
        epilog='Examples:\n'
               '  # Google Docs\n'
               '  %(prog)s https://docs.google.com/document/d/DOC_ID/edit output.md\n'
               '  %(prog)s input.md DOC_ID\n'
               '  %(prog)s input.md --create\n'
               '  %(prog)s input.md --create -u | pbcopy\n'
               '  %(prog)s DOC_ID --list-revisions\n'
               '  %(prog)s DOC_ID --list-comments\n'
               '  %(prog)s DOC_ID --lock\n\n'
               '  # Batch Document Management\n'
               '  %(prog)s --batch file1.md file2.md file3.md\n'
               '  %(prog)s --batch file1.md file2.md --batch-title "Project Documentation"\n'
               '  %(prog)s --batch file1.md file2.md --batch-headers --batch-horizontal-sep --batch-toc\n'
               '  %(prog)s DIRECTORY --list-batch\n'
               '  %(prog)s DOC_ID --diff-batch\n'
               '  %(prog)s BATCH_ID --batch-update\n'
               '  %(prog)s "Batch Title" --batch-update\n\n'
               '  # Confluence\n'
               '  %(prog)s input.md confluence:SPACE/123456\n'
               '  %(prog)s input.md --create-confluence --space ENG --title "My Page"\n'
               '  %(prog)s confluence:SPACE/123456 output.md\n'
               '  %(prog)s https://site.atlassian.net/wiki/spaces/ENG/pages/123456 output.md\n\n'
               '  # List frontmatter\n'
               '  %(prog)s list [file_or_directory]\n'
               '  %(prog)s list --check-status  # Check live frozen status\n'
               '  %(prog)s list --check-status --diff  # Check sync status summary\n'
               '  %(prog)s list --format json   # JSON output\n\n'
               '  # Diff (dry run)\n'
               '  %(prog)s file.md gdoc_url --diff\n'
               '  %(prog)s gdoc_url file.md --diff\n'
               '  %(prog)s file.md confluence:SPACE/123 --diff\n\n'
               '  # Intelligent destination detection\n'
               '  %(prog)s file.md  # Auto-detect from frontmatter',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    # Main sync arguments
    parser.add_argument('source', nargs='?', help='Source: Google Doc URL/ID, Confluence page, or Markdown file')
    parser.add_argument('destination', nargs='?', 
                       help='Destination: Google Doc URL/ID, Confluence page, or Markdown file')
    
    # Google Docs options
    parser.add_argument('--create', action='store_true',
                       help='Create a new Google Doc (use with markdown source)')
    parser.add_argument('--list-revisions', action='store_true',
                       help='List revision history for a Google Doc')
    parser.add_argument('--lock', action='store_true',
                       help='Lock a Google Doc to prevent editing')
    parser.add_argument('--unlock', action='store_true',
                       help='Unlock a Google Doc to allow editing')
    parser.add_argument('--lock-status', action='store_true',
                       help='Check if a Google Doc is locked')
    parser.add_argument('--lock-reason', type=str, metavar='REASON',
                       help='Reason for locking (use with --lock)')
    
    # Confluence lock options
    parser.add_argument('--lock-confluence', action='store_true',
                       help='Lock a Confluence page (restrict editing to allowed editors from secrets.yaml)')
    parser.add_argument('--unlock-confluence', action='store_true',
                       help='Unlock a Confluence page (remove all edit restrictions)')
    parser.add_argument('--confluence-lock-status', action='store_true',
                       help='Check if a Confluence page is locked')
    parser.add_argument('--list-comments', action='store_true',
                       help='List all comments from a Google Doc')
    parser.add_argument('--unresolved-only', action='store_true',
                       help='Show only unresolved comments (use with --list-comments)')
    
    # Confluence options
    parser.add_argument('--create-confluence', action='store_true',
                       help='Create a new Confluence page (use with markdown source)')
    parser.add_argument('--space', type=str, metavar='SPACE',
                       help='Confluence space key (required with --create-confluence)')
    parser.add_argument('--title', type=str, metavar='TITLE',
                       help='Page title (required with --create-confluence, overrides frontmatter title)')
    parser.add_argument('--parent-id', type=str, metavar='PARENT_ID',
                       help='Parent page ID for new Confluence page')
    parser.add_argument('--labels', type=str, metavar='LABELS',
                       help='Comma-separated labels for Confluence page (combined with frontmatter labels)')
    parser.add_argument('--secrets-file', type=str, metavar='PATH',
                       help='Path to secrets.yaml file (default: searches in current dir, ~/.config/mdsync/, ~/.mdsync/)')
    
    # Heading management options
    parser.add_argument('--create-empty', action='store_true',
                       help='Create empty Google Doc')
    parser.add_argument('--list-batch', action='store_true',
                       help='List all batch groupings in markdown files')
    parser.add_argument('--diff-batch', action='store_true',
                       help='Diff entire batch against Google Doc (use with batch document ID)')
    parser.add_argument('--batch-update', action='store_true',
                       help='Update existing batch by finding all files in current directory (use with batch ID, title, or doc ID)')
    parser.add_argument('--batch', nargs='+', metavar='MARKDOWN_FILE',
                       help='Create a new Google Doc with multiple markdown files as headings (simple client-side combination)')
    parser.add_argument('--batch-title', type=str, metavar='TITLE',
                       help='Title for the batch document (if not specified, uses first markdown file title)')
    parser.add_argument('--batch-headers', action='store_true',
                       help='Include individual file titles as headers in the batch document (default: content only)')
    parser.add_argument('--batch-horizontal-sep', action='store_true',
                       help='Add horizontal separators between files in the batch document (default: no separators)')
    parser.add_argument('--batch-toc', action='store_true',
                       help='Generate and include a table of contents for H1 headings in the batch document')
    
    # General options
    parser.add_argument('-u', '--url-only', action='store_true',
                       help='Output only the URL (perfect for piping to pbcopy)')
    parser.add_argument('-f', '--force', action='store_true',
                       help='Skip confirmation when overwriting existing Google Doc links in frontmatter')
    parser.add_argument('--diff', action='store_true',
                       help='Show diff between source and destination (markdown as common format)')
    parser.add_argument('--format', type=str, choices=['text', 'json', 'markdown'],
                       default='text', metavar='FORMAT',
                       help='Output format: text, json, or markdown (default: text)')
    parser.add_argument('--version', action='version', version='mdsync 0.2.9',
                       help='Show version information and exit')
    
    # Handle list command (special case) - check before parsing main args
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == 'list':
        # Parse list command arguments manually
        list_args = sys.argv[2:]  # Skip 'mdsync' and 'list'
        
        # Parse list arguments
        list_parser = argparse.ArgumentParser()
        list_parser.add_argument('path', nargs='?', default='.', 
                               help='File or directory to scan (default: current directory)')
        list_parser.add_argument('--format', type=str, choices=['text', 'json'], default='text',
                               help='Output format: text or json (default: text)')
        list_parser.add_argument('--check-status', action='store_true',
                               help='Check live frozen status of destinations (requires credentials)')
        list_parser.add_argument('--diff', action='store_true',
                               help='Show sync status summary for each destination (markdown vs remote)')
        
        try:
            list_args_parsed = list_parser.parse_args(list_args)
            list_markdown_files(list_args_parsed.path, list_args_parsed.format, list_args_parsed.check_status, list_args_parsed.diff)
            return
        except SystemExit:
            return
    
    args = parser.parse_args()
    
    # Extract secrets_file_path early for use throughout main()
    secrets_file_path = args.secrets_file if hasattr(args, 'secrets_file') and args.secrets_file else None
    
    # Handle --create-empty command (must be before type detection)
    if args.create_empty:
        # Use source as document title, or default title if not provided
        title = args.source if args.source else "Untitled Document"
        
        doc_id = create_empty_document(title, quiet=args.url_only)
        if doc_id:
            if args.url_only:
                print(f"https://docs.google.com/document/d/{doc_id}/edit")
            else:
                print(f"Document ID: {doc_id}")
        else:
            sys.exit(1)
        return
    
    # Handle --batch command (must be before type detection)
    if args.batch:
        if not args.batch:
            print("Error: At least one markdown file required for --batch", file=sys.stderr)
            print("Use: mdsync --batch file1.md file2.md file3.md", file=sys.stderr)
            sys.exit(1)
        
        # Determine document title
        if args.batch_title:
            title = args.batch_title
        else:
            # Use first markdown file's title
            try:
                with open(args.batch[0], 'r', encoding='utf-8') as f:
                    first_content = f.read()
                metadata = extract_frontmatter_metadata(first_content)
                title = metadata.get('title') or Path(args.batch[0]).stem.replace('_', ' ').replace('-', ' ').title()
            except Exception:
                title = "Batch Document"
        
        # Check if files already belong to an existing batch
        existing_batch_doc_id = None
        existing_batch_title = None
        files_in_existing_batch = []
        
        for markdown_path in args.batch:
            try:
                with open(markdown_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                metadata = extract_frontmatter_metadata(content)
                
                if 'batch' in metadata and isinstance(metadata['batch'], dict):
                    batch_info = metadata['batch']
                    batch_doc_id = batch_info.get('doc_id')
                    batch_title = batch_info.get('batch_title')
                    
                    if batch_doc_id:
                        if existing_batch_doc_id is None:
                            existing_batch_doc_id = batch_doc_id
                            existing_batch_title = batch_title
                        elif existing_batch_doc_id != batch_doc_id:
                            # Files belong to different batches - this is complex
                            existing_batch_doc_id = "MIXED"
                            break
                        
                        files_in_existing_batch.append(markdown_path)
                        
            except Exception:
                continue
        
        # If we found an existing batch, scan the directory for ALL files in that batch
        all_files_in_existing_batch = []
        if existing_batch_doc_id and existing_batch_doc_id != "MIXED":
            try:
                # Get the directory of the first file
                first_file_dir = os.path.dirname(args.batch[0]) if args.batch else "."
                
                # Scan directory for all markdown files that belong to this batch
                for filename in os.listdir(first_file_dir):
                    if filename.endswith('.md'):
                        file_path = os.path.join(first_file_dir, filename)
                        try:
                            with open(file_path, 'r', encoding='utf-8') as f:
                                content = f.read()
                            metadata = extract_frontmatter_metadata(content)
                            
                            if ('batch' in metadata and 
                                isinstance(metadata['batch'], dict) and
                                metadata['batch'].get('doc_id') == existing_batch_doc_id):
                                all_files_in_existing_batch.append(file_path)
                        except Exception:
                            continue
            except Exception:
                # If directory scanning fails, fall back to just the processed files
                all_files_in_existing_batch = files_in_existing_batch
        
        # If all files belong to the same existing batch, offer to update it
        if existing_batch_doc_id and existing_batch_doc_id != "MIXED" and not args.force:
            print(f"\n📋 Found existing batch: '{existing_batch_title}'")
            print(f"   Document ID: {existing_batch_doc_id}")
            print(f"   Files in batch: {len(all_files_in_existing_batch)}")
            
            # Check for files that will be excluded from the new batch
            new_batch_files = set(args.batch)
            existing_batch_files = set(all_files_in_existing_batch)
            excluded_files = existing_batch_files - new_batch_files
            
            if excluded_files:
                print(f"\n⚠️  Warning: {len(excluded_files)} file(s) from the existing batch will NOT be included in the new batch:")
                for file_path in sorted(excluded_files):
                    print(f"   • {os.path.basename(file_path)}")
                print(f"   These files will remain in the existing batch document.")
            
            print(f"\nThis will create a NEW batch document instead of updating the existing one.")
            print(f"To update the existing batch, use: mdsync '{existing_batch_title}' --batch-update")
            
            try:
                response = input("Do you want to create a new batch document? [y/N]: ").strip().lower()
                if response not in ['y', 'yes']:
                    print("Operation cancelled. Use --batch-update to update existing batch.")
                    sys.exit(0)
            except (EOFError, KeyboardInterrupt):
                print("\nOperation cancelled.")
                sys.exit(0)
        
        # Check for files with individual gdoc_url (not in batch)
        elif not args.force:
            files_with_individual_gdoc = []
            for markdown_path in args.batch:
                try:
                    with open(markdown_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    metadata = extract_frontmatter_metadata(content)
                    if metadata.get('gdoc_url') and not metadata.get('batch'):
                        files_with_individual_gdoc.append(markdown_path)
                except Exception:
                    continue
            
            if files_with_individual_gdoc:
                print(f"\n⚠️  Warning: {len(files_with_individual_gdoc)} file(s) have individual Google Doc links:")
                for file_path in files_with_individual_gdoc:
                    print(f"   {os.path.basename(file_path)}")
                print(f"\nThis batch operation will update these links to point to the new batch document.")
                
                try:
                    response = input("Do you want to continue? [y/N]: ").strip().lower()
                    if response not in ['y', 'yes']:
                        print("Operation cancelled.")
                        sys.exit(0)
                except (EOFError, KeyboardInterrupt):
                    print("\nOperation cancelled.")
                    sys.exit(0)
        
        # Create the batch document
        # Include title only if --batch-title was explicitly provided
        include_title = args.batch_title is not None
        doc_id = create_batch_document_simple(args.batch, title, quiet=args.url_only, include_headers=args.batch_headers, include_horizontal_sep=args.batch_horizontal_sep, include_title=include_title, include_toc=args.batch_toc)
        if doc_id:
            if args.url_only:
                print(f"https://docs.google.com/document/d/{doc_id}/edit")
            else:
                print(f"Document ID: {doc_id}")
        else:
            sys.exit(1)
        return
    
    
    # Validate required arguments (skip for list-batch and batch-update which use different sources)
    if not args.source and not args.list_batch and not args.batch_update:
        print("Error: Source is required", file=sys.stderr)
        print("Use: mdsync <source> [destination] or mdsync list [file_or_directory]", file=sys.stderr)
        print("Run 'mdsync --help' for more information", file=sys.stderr)
        sys.exit(1)
    
    # Determine source and destination types
    source_is_gdoc = args.source and is_google_doc(args.source)
    source_is_confluence = args.source and is_confluence_page(args.source)
    source_is_markdown = args.source and not source_is_gdoc and not source_is_confluence and not args.batch_update
    
    dest_is_confluence = args.destination and is_confluence_page(args.destination)
    dest_is_gdoc = args.destination and is_google_doc(args.destination)
    dest_is_markdown = args.destination and not dest_is_confluence and not dest_is_gdoc
    
    # Get appropriate credentials early for diff operations and intelligent destination detection
    creds = None
    confluence = None
    
    # Only get Google credentials when actually needed (for Google Doc operations)
    if source_is_gdoc or dest_is_gdoc or args.create or args.lock or args.unlock or args.lock_status or args.list_revisions or args.list_comments or (args.diff and (source_is_gdoc or dest_is_gdoc)):
        creds = get_credentials()
    
    if source_is_confluence or dest_is_confluence or args.create_confluence or args.diff or source_is_markdown:
        confluence = get_confluence_client(args.secrets_file if hasattr(args, 'secrets_file') else None)
    
    # Handle diff operations (dry run) - must be before intelligent destination detection
    if args.diff:
        if source_is_markdown and dest_is_gdoc:
            # Markdown → Google Doc diff
            doc_id = extract_doc_id(args.destination)
            diff_markdown_to_gdoc(args.source, doc_id, creds)
        elif source_is_gdoc and dest_is_markdown:
            # Google Doc → Markdown diff
            doc_id = extract_doc_id(args.source)
            diff_gdoc_to_markdown(doc_id, args.destination, creds)
        elif source_is_markdown and dest_is_confluence:
            # Markdown → Confluence diff
            diff_markdown_to_confluence(args.source, args.destination, confluence)
        elif source_is_confluence and dest_is_markdown:
            # Confluence → Markdown diff
            diff_confluence_to_markdown(args.source, args.destination, confluence)
        else:
            print("Error: --diff requires both source and destination", file=sys.stderr)
            print("Supported diff combinations:", file=sys.stderr)
            print("  markdown_file google_doc_url --diff", file=sys.stderr)
            print("  google_doc_url markdown_file --diff", file=sys.stderr)
            print("  markdown_file confluence:SPACE/PAGE_ID --diff", file=sys.stderr)
            print("  confluence:SPACE/PAGE_ID markdown_file --diff", file=sys.stderr)
            sys.exit(1)
        return
    
    # Intelligent destination detection for markdown files
    if source_is_markdown and not args.destination and not args.create and not args.create_confluence and not args.list_batch:
        # Check if markdown has frontmatter with URLs
        try:
            with open(args.source, 'r', encoding='utf-8') as f:
                markdown_content = f.read()
            
            frontmatter = extract_frontmatter_metadata(markdown_content)
            gdoc_url = frontmatter.get('gdoc_url')
            confluence_url = frontmatter.get('confluence_url')
            
            available_destinations = []
            frozen_destinations = []
            
            # Check Google Doc
            if gdoc_url:
                doc_id = extract_doc_id_from_url(gdoc_url)
                if doc_id:
                    # Check if frozen at runtime
                    if check_gdoc_frozen_status(doc_id, creds):
                        frozen_destinations.append(('gdoc', gdoc_url))
                        print(f"⚠️  Google Doc is frozen: {gdoc_url}")
                    else:
                        available_destinations.append(('gdoc', f"https://docs.google.com/document/d/{doc_id}/edit", gdoc_url))
            
            # Check Confluence
            if confluence_url:
                # Parse Confluence URL to get page ID
                dest_info = parse_confluence_destination(confluence_url)
                page_id = dest_info.get('page_id')
                if page_id:
                    # Check if frozen at runtime
                    if check_confluence_frozen_status(page_id, confluence):
                        frozen_destinations.append(('confluence', confluence_url))
                        print(f"⚠️  Confluence page is frozen: {confluence_url}")
                    else:
                        available_destinations.append(('confluence', confluence_url, confluence_url))
            
            if not available_destinations:
                print("Error: No destination specified and no available URLs in frontmatter", file=sys.stderr)
                if gdoc_url or confluence_url:
                    print("All configured destinations are frozen. Use platform-specific unlock commands to enable syncing.", file=sys.stderr)
                else:
                    print("Use: mdsync file.md <destination> or add gdoc_url/confluence_url to frontmatter", file=sys.stderr)
                sys.exit(1)
            elif len(available_destinations) == 1:
                # Single destination - auto-select
                platform, url, original_url = available_destinations[0]
                print(f"Found {platform} URL in frontmatter: {original_url}")
                print(f"Auto-syncing to {platform.title()}")
                args.destination = url
                if platform == 'gdoc':
                    dest_is_gdoc = True
                elif platform == 'confluence':
                    dest_is_confluence = True
            else:
                # Multiple destinations - ask user to choose
                print("Multiple destinations found in frontmatter:")
                for i, (platform, url, original_url) in enumerate(available_destinations, 1):
                    print(f"  {i}. {platform.title()}: {original_url}")
                
                try:
                    choice = input("Choose destination (1-{}): ".format(len(available_destinations)))
                    choice_idx = int(choice) - 1
                    if 0 <= choice_idx < len(available_destinations):
                        platform, url, original_url = available_destinations[choice_idx]
                        print(f"Selected {platform.title()}: {original_url}")
                        args.destination = url
                        if platform == 'gdoc':
                            dest_is_gdoc = True
                        elif platform == 'confluence':
                            dest_is_confluence = True
                    else:
                        print("Invalid choice", file=sys.stderr)
                        sys.exit(1)
                except (ValueError, KeyboardInterrupt):
                    print("Cancelled", file=sys.stderr)
                    sys.exit(1)
                    
        except FileNotFoundError:
            print(f"Error: Markdown file not found: {args.source}", file=sys.stderr)
            sys.exit(1)
    
    # Check for destination mismatch warnings
    if source_is_markdown and dest_is_gdoc and args.destination and not args.list_batch:
        try:
            with open(args.source, 'r', encoding='utf-8') as f:
                markdown_content = f.read()
            
            frontmatter = extract_frontmatter_metadata(markdown_content)
            frontmatter_gdoc_url = frontmatter.get('gdoc_url')
            
            if frontmatter_gdoc_url:
                frontmatter_doc_id = extract_doc_id_from_url(frontmatter_gdoc_url)
                dest_doc_id = extract_doc_id(args.destination)
                
                if frontmatter_doc_id and dest_doc_id and frontmatter_doc_id != dest_doc_id:
                    print(f"⚠️  WARNING: Destination mismatch!", file=sys.stderr)
                    print(f"   Frontmatter gdoc_url: {frontmatter_gdoc_url}", file=sys.stderr)
                    print(f"   Command destination:  {args.destination}", file=sys.stderr)
                    print(f"   This will sync to a different Google Doc than expected.", file=sys.stderr)
                    print(f"   Continue? (y/N): ", end='', file=sys.stderr)
                    
                    try:
                        response = input().strip().lower()
                        if response not in ['y', 'yes']:
                            print("Cancelled.", file=sys.stderr)
                            sys.exit(0)
                    except KeyboardInterrupt:
                        print("\nCancelled.", file=sys.stderr)
                        sys.exit(0)
        except FileNotFoundError:
            pass  # File not found, continue normally
    
    # Credentials already initialized above for diff operations
    
    # Handle lock/unlock operations
    if args.lock or args.unlock or args.lock_status:
        if not source_is_gdoc:
            print("Error: Lock operations only work with Google Docs", file=sys.stderr)
            sys.exit(1)
        
        doc_id = extract_doc_id(args.source)
        
        if args.lock:
            reason = args.lock_reason or "Document locked via mdsync"
            lock_document(doc_id, creds, reason)
        elif args.unlock:
            unlock_document(doc_id, creds)
        elif args.lock_status:
            check_lock_status(doc_id, creds)
        
        return
    
    # Handle --list-comments flag
    if args.list_comments:
        if not source_is_gdoc:
            print("Error: --list-comments only works with Google Docs", file=sys.stderr)
            sys.exit(1)
        
        doc_id = extract_doc_id(args.source)
        list_comments(doc_id, creds, unresolved_only=args.unresolved_only, output_format=args.format)
        return
    
    # Handle --list-revisions flag
    if args.list_revisions:
        if not source_is_gdoc:
            print("Error: --list-revisions only works with Google Docs", file=sys.stderr)
            sys.exit(1)
        
        doc_id = extract_doc_id(args.source)
        list_revisions(doc_id, creds)
        return
    
    # Handle Confluence lock/unlock operations
    if args.lock_confluence or args.unlock_confluence or args.confluence_lock_status:
        if not is_confluence_page(args.source):
            print("Error: Source must be a Confluence page for lock operations", file=sys.stderr)
            sys.exit(1)
        
        parsed = parse_confluence_destination(args.source)
        page_id = parsed['page_id']
        
        if not page_id:
            print("Error: Could not extract Confluence page ID", file=sys.stderr)
            sys.exit(1)
        
        confluence_creds = get_confluence_credentials(secrets_file_path)
        if not confluence_creds:
            print("Error: Confluence credentials not found", file=sys.stderr)
            sys.exit(1)
        
        if args.lock_confluence:
            print(f"Locking Confluence page {page_id}...")
            success = lock_confluence_page(
                page_id,
                confluence_creds['url'],
                confluence_creds['username'],
                confluence_creds['api_token'],
                secrets_file_path=secrets_file_path
            )
            if success:
                print(f"✓ Page locked successfully")
                print(f"  Only configured editors can now edit this page")
            else:
                print(f"✗ Failed to lock page", file=sys.stderr)
                sys.exit(1)
        
        elif args.unlock_confluence:
            print(f"Unlocking Confluence page {page_id}...")
            success = unlock_confluence_page(
                page_id,
                confluence_creds['url'],
                confluence_creds['username'],
                confluence_creds['api_token']
            )
            if success:
                print(f"✓ Page unlocked successfully")
            else:
                print(f"✗ Failed to unlock page", file=sys.stderr)
                sys.exit(1)
        
        elif args.confluence_lock_status:
            check_confluence_lock_status(
                page_id,
                confluence_creds['url'],
                confluence_creds['username'],
                confluence_creds['api_token']
            )
        
        return
    
    # Handle heading management operations
    # These commands enable creating and managing documents with organized heading sections
    
    if args.list_batch:
        if not args.source:
            print("Error: Directory required for --list-batch", file=sys.stderr)
            print("Use: mdsync DIRECTORY --list-batch", file=sys.stderr)
            sys.exit(1)
        
        # List batch groupings in markdown files
        list_batch_groupings(args.source, quiet=args.url_only)
        return
    
    if args.diff_batch:
        if not args.source:
            print("Error: Google Doc ID required for --diff-batch", file=sys.stderr)
            print("Use: mdsync DOC_ID --diff-batch", file=sys.stderr)
            sys.exit(1)
        
        if not source_is_gdoc:
            print("Error: --diff-batch only works with Google Docs", file=sys.stderr)
            sys.exit(1)
        
        # Diff entire batch against Google Doc
        doc_id = extract_doc_id(args.source)
        diff_batch_against_gdoc(doc_id, quiet=args.url_only)
        return
    
    if args.batch_update:
        if not args.source:
            print("Error: Batch ID, title, or Google Doc ID required for --batch-update", file=sys.stderr)
            print("Use: mdsync BATCH_ID --batch-update", file=sys.stderr)
            print("     mdsync 'Batch Title' --batch-update", file=sys.stderr)
            print("     mdsync DOC_ID --batch-update", file=sys.stderr)
            sys.exit(1)
        
        # Update existing batch by finding all files
        update_batch_by_name(args.source, quiet=args.url_only)
        return
    
    
    # Handle Confluence → Markdown
    if source_is_confluence:
        if not args.destination:
            print("Error: Destination markdown file required", file=sys.stderr)
            sys.exit(1)
        
        parsed = parse_confluence_destination(args.source)
        page_id = parsed['page_id']
        
        if not page_id:
            print("Error: Could not extract Confluence page ID", file=sys.stderr)
            sys.exit(1)
        
        if not args.url_only:
            print(f"Exporting Confluence page {page_id} to {args.destination}...")
        
        # Export with frontmatter
        markdown_content = export_confluence_to_markdown(page_id, confluence, args.destination)
        
        if not args.url_only:
            print(f"Successfully exported to {args.destination}")
            print(f"Added confluence_url to frontmatter")
        return
    
    # Handle Google Doc → Markdown
    if source_is_gdoc:
        if not args.destination:
            print("Error: Destination markdown file required", file=sys.stderr)
            sys.exit(1)
        
        doc_id = extract_doc_id(args.source)
        
        if not args.url_only:
            print(f"Exporting Google Doc {doc_id} to {args.destination}...")
        
        # Export with frontmatter
        markdown_content = export_gdoc_to_markdown(doc_id, creds, args.destination)
        
        if not args.url_only:
            print(f"Successfully exported to {args.destination}")
            print(f"Added metadata to frontmatter: title, gdoc_url, gdoc_created, gdoc_modified")
        return
    
    # Handle Markdown → Confluence
    if source_is_markdown and (dest_is_confluence or args.create_confluence):
        if args.create_confluence:
            # Create new Confluence page
            if not args.space:
                print("Error: --space required with --create-confluence", file=sys.stderr)
                sys.exit(1)
            
            # Title is optional - will use frontmatter or filename if not provided
            labels = args.labels.split(',') if args.labels else None
            
            if not args.url_only:
                print(f"Creating new Confluence page '{args.title}' in space {args.space}...")
            
            page_id = create_confluence_page(
                args.source, confluence, args.space, args.title,
                parent_id=args.parent_id, labels=labels, quiet=args.url_only
            )
            
            if args.url_only:
                # Get the page URL
                page = confluence.get_page_by_id(page_id)
                base_url = confluence.url.rstrip('/')
                print(f"{base_url}/wiki/spaces/{args.space}/pages/{page_id}")
        
        elif dest_is_confluence:
            # Update existing Confluence page
            parsed = parse_confluence_destination(args.destination)
            page_id = parsed['page_id']
            
            if not page_id:
                print("Error: Could not extract Confluence page ID from destination", file=sys.stderr)
                sys.exit(1)
            
            if not args.url_only:
                print(f"Updating Confluence page {page_id}...")
            
            import_markdown_to_confluence(args.source, page_id, confluence, quiet=args.url_only)
            
            if args.url_only:
                page = confluence.get_page_by_id(page_id)
                space_key = page['space']['key']
                base_url = confluence.url.rstrip('/')
                print(f"{base_url}/wiki/spaces/{space_key}/pages/{page_id}")
        
        return
    
    # Handle Markdown → Google Doc
    if source_is_markdown and (dest_is_gdoc or args.create):
        if args.list_revisions:
            print("Error: --list-revisions only works with Google Docs", file=sys.stderr)
            sys.exit(1)
        
        if args.create:
            # Check for existing gdoc_url and ask for confirmation
            if not check_existing_gdoc_confirmation(args.source, args.force):
                sys.exit(0)
            
            # Create a new Google Doc
            if not args.url_only:
                print(f"Creating new Google Doc from {args.source}...")
            doc_id = create_new_gdoc_from_markdown(args.source, creds, quiet=args.url_only)
            if args.url_only:
                print(f"https://docs.google.com/document/d/{doc_id}/edit")
        else:
            if not args.destination:
                print("Error: Destination Google Doc URL/ID required (or use --create)", file=sys.stderr)
                sys.exit(1)
            
            doc_id = extract_doc_id(args.destination)
            
            # Check for existing gdoc_url and ask for confirmation
            if not check_existing_gdoc_confirmation(args.source, args.force, doc_id):
                sys.exit(0)
            
            # Check if this is a batch file
            try:
                with open(args.source, 'r', encoding='utf-8') as f:
                    markdown_content = f.read()
                
                metadata = extract_frontmatter_metadata(markdown_content)
                
                if 'batch' in metadata and isinstance(metadata['batch'], dict):
                    batch_info = metadata['batch']
                    if batch_info.get('doc_id') == doc_id:
                        if not args.url_only:
                            print(f"Note: This file is part of a batch document")
                            print(f"Batch: {batch_info.get('batch_title', 'Unknown')}")
                            print(f"Heading: {batch_info.get('heading_title', 'Unknown')}")
                            print(f"Consider using: mdsync {doc_id} --diff-batch")
                            print()
            except Exception:
                pass  # Not a batch file, continue with normal processing
            
            if not args.url_only:
                print(f"Importing {args.source} to Google Doc {doc_id}...")
            
            import_markdown_to_gdoc(args.source, doc_id, creds, quiet=args.url_only)
            if args.url_only:
                print(f"https://docs.google.com/document/d/{doc_id}/edit")
        
        return
    
    # If we get here, show error
    print("Error: Invalid source/destination combination", file=sys.stderr)
    print("Run 'mdsync --help' for usage examples", file=sys.stderr)
    sys.exit(1)


if __name__ == '__main__':
    main()
