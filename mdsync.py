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
          'https://www.googleapis.com/auth/drive.file']


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
    return ('docs.google.com' in path or 
            ('/' not in path and '.' not in path and len(path) > 20))


def is_confluence_page(path: str) -> bool:
    """Check if the path is a Confluence page URL or ID."""
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


def get_confluence_credentials():
    """Get Confluence credentials as a dict (for direct API calls)."""
    # Look for Confluence credentials in multiple locations
    confluence_url = os.getenv('CONFLUENCE_URL')
    confluence_username = os.getenv('CONFLUENCE_USERNAME')
    confluence_token = os.getenv('CONFLUENCE_API_TOKEN') or os.getenv('CONFLUENCE_TOKEN')
    
    # Try secrets.yaml first (preferred method)
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


def get_confluence_client():
    """Get Confluence API client from secrets.yaml, environment variables, or config."""
    if not CONFLUENCE_AVAILABLE:
        print("Error: Confluence support not available. Install with: pip install atlassian-python-api", file=sys.stderr)
        sys.exit(1)
    
    # Look for Confluence credentials in multiple locations
    confluence_url = os.getenv('CONFLUENCE_URL')
    confluence_username = os.getenv('CONFLUENCE_USERNAME')
    confluence_token = os.getenv('CONFLUENCE_API_TOKEN') or os.getenv('CONFLUENCE_TOKEN')
    
    # Try secrets.yaml first (preferred method)
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
    
    # Remove problematic id attributes from headings
    confluence_content = re.sub(
        r'<h([1-6]) id="[^"]*">',
        r'<h\1>',
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
    
    # Convert links - distinguish between internal pages and external URLs
    def convert_link(match):
        href = match.group(1)
        text = match.group(2)
        
        # External URL (starts with http/https)
        if href.startswith(('http://', 'https://', 'mailto:')):
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


def export_confluence_to_markdown(page_id: str, confluence) -> str:
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
        
        # Convert markdown to Confluence storage format
        storage_content = markdown_to_confluence_storage(markdown_content)
        
        # Update the page
        confluence.update_page(
            page_id=page_id,
            title=title,
            body=storage_content,
            parent_id=page.get('ancestors', [{}])[-1].get('id') if page.get('ancestors') else None,
            type='page',
            representation='storage'
        )
        
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
            if frontmatter_labels:
                print(f"  Labels: {', '.join(frontmatter_labels)}")
        
    except FileNotFoundError:
        print(f"Error: Markdown file not found: {markdown_path}", file=sys.stderr)
        sys.exit(1)
    except Exception as error:
        print(f"An error occurred: {error}", file=sys.stderr)
        sys.exit(1)


def extract_frontmatter_metadata(markdown_content: str) -> dict:
    """Extract metadata from markdown frontmatter (title, labels, etc.)."""
    try:
        import frontmatter
        post = frontmatter.loads(markdown_content)
        return {
            'title': post.metadata.get('title'),
            'labels': post.metadata.get('labels', []),
            'parent': post.metadata.get('parent'),
        }
    except Exception:
        return {'title': None, 'labels': [], 'parent': None}


def get_confluence_permissions_config():
    """Get default permissions configuration from secrets.yaml."""
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
                        allowed_editors: dict = None) -> bool:
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
            perms_config = get_confluence_permissions_config()
            if perms_config and 'allowed_editors' in perms_config:
                allowed_editors = perms_config['allowed_editors']
            else:
                # Fallback: only current user
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
        
        # Convert markdown to Confluence storage format
        storage_content = markdown_to_confluence_storage(markdown_content)
        
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
            if parent_id:
                print(f"  Parent ID: {parent_id}")
            if all_labels:
                print(f"  Labels: {', '.join(all_labels)}")
        
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


def export_gdoc_to_markdown(doc_id: str, creds) -> str:
    """Export a Google Doc to Markdown format."""
    try:
        # Build the Drive service (used for export)
        drive_service = build('drive', 'v3', credentials=creds)
        
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
        return markdown_content
        
    except HttpError as error:
        print(f"An error occurred: {error}", file=sys.stderr)
        sys.exit(1)


def import_markdown_to_gdoc(markdown_path: str, doc_id: str, creds, quiet: bool = False):
    """Import a Markdown file to a Google Doc."""
    try:
        # Read the markdown file
        with open(markdown_path, 'r', encoding='utf-8') as f:
            markdown_content = f.read()
        
        # Build the Drive service
        drive_service = build('drive', 'v3', credentials=creds)
        
        # Create a temporary file with the markdown content
        temp_file = io.BytesIO(markdown_content.encode('utf-8'))
        
        # Update the document by uploading the markdown
        media = MediaFileUpload(
            markdown_path,
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
        
    except HttpError as error:
        print(f"An error occurred: {error}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print(f"Error: Markdown file not found: {markdown_path}", file=sys.stderr)
        sys.exit(1)


def create_new_gdoc_from_markdown(markdown_path: str, creds, quiet: bool = False) -> str:
    """Create a new Google Doc from a Markdown file."""
    try:
        # Build the Drive service
        drive_service = build('drive', 'v3', credentials=creds)
        
        # Get the base name for the document
        doc_name = Path(markdown_path).stem
        
        # Upload the markdown file and convert it to Google Docs format
        file_metadata = {
            'name': doc_name,
            'mimeType': 'application/vnd.google-apps.document'
        }
        
        media = MediaFileUpload(
            markdown_path,
            mimetype='text/markdown',
            resumable=True
        )
        
        file = drive_service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id'
        ).execute()
        
        doc_id = file.get('id')
        
        if not quiet:
            print(f"Created new Google Doc with ID: {doc_id}")
            print(f"URL: https://docs.google.com/document/d/{doc_id}/edit")
        
        return doc_id
        
    except HttpError as error:
        print(f"An error occurred: {error}", file=sys.stderr)
        sys.exit(1)


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
               '  # Confluence\n'
               '  %(prog)s input.md confluence:SPACE/123456\n'
               '  %(prog)s input.md --create-confluence --space ENG --title "My Page"\n'
               '  %(prog)s confluence:SPACE/123456 output.md\n'
               '  %(prog)s https://site.atlassian.net/wiki/spaces/ENG/pages/123456 output.md',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument('source', help='Source: Google Doc URL/ID, Confluence page, or Markdown file')
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
    
    # General options
    parser.add_argument('-u', '--url-only', action='store_true',
                       help='Output only the URL (perfect for piping to pbcopy)')
    parser.add_argument('--format', type=str, choices=['text', 'json', 'markdown'],
                       default='text', metavar='FORMAT',
                       help='Output format: text, json, or markdown (default: text)')
    
    args = parser.parse_args()
    
    # Determine source and destination types
    source_is_gdoc = is_google_doc(args.source)
    source_is_confluence = is_confluence_page(args.source)
    source_is_markdown = not source_is_gdoc and not source_is_confluence
    
    dest_is_confluence = args.destination and is_confluence_page(args.destination)
    dest_is_gdoc = args.destination and is_google_doc(args.destination)
    
    # Get appropriate credentials
    creds = None
    confluence = None
    
    if source_is_gdoc or dest_is_gdoc or args.create or args.lock or args.unlock or args.lock_status or args.list_revisions or args.list_comments:
        creds = get_credentials()
    
    if source_is_confluence or dest_is_confluence or args.create_confluence:
        confluence = get_confluence_client()
    
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
        
        confluence_creds = get_confluence_credentials()
        if not confluence_creds:
            print("Error: Confluence credentials not found", file=sys.stderr)
            sys.exit(1)
        
        if args.lock_confluence:
            print(f"Locking Confluence page {page_id}...")
            success = lock_confluence_page(
                page_id,
                confluence_creds['url'],
                confluence_creds['username'],
                confluence_creds['api_token']
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
        
        markdown_content = export_confluence_to_markdown(page_id, confluence)
        
        # Write to file
        with open(args.destination, 'w', encoding='utf-8') as f:
            f.write(markdown_content)
        
        if not args.url_only:
            print(f"Successfully exported to {args.destination}")
        return
    
    # Handle Google Doc → Markdown
    if source_is_gdoc:
        if not args.destination:
            print("Error: Destination markdown file required", file=sys.stderr)
            sys.exit(1)
        
        doc_id = extract_doc_id(args.source)
        
        if not args.url_only:
            print(f"Exporting Google Doc {doc_id} to {args.destination}...")
        
        markdown_content = export_gdoc_to_markdown(doc_id, creds)
        
        # Write to file
        with open(args.destination, 'w', encoding='utf-8') as f:
            f.write(markdown_content)
        
        if not args.url_only:
            print(f"Successfully exported to {args.destination}")
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
