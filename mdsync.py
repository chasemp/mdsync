#!/usr/bin/env python3
"""
mdsync - Sync between Google Docs and Markdown files
"""

import os
import sys
import re
import argparse
from pathlib import Path
from typing import Optional, Tuple

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
import io

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
        description='Sync between Google Docs and Markdown files',
        epilog='Examples:\n'
               '  %(prog)s https://docs.google.com/document/d/DOC_ID/edit output.md\n'
               '  %(prog)s input.md https://docs.google.com/document/d/DOC_ID/edit\n'
               '  %(prog)s DOC_ID output.md\n'
               '  %(prog)s input.md --create\n'
               '  %(prog)s input.md --create -u | pbcopy\n'
               '  %(prog)s DOC_ID --list-revisions\n'
               '  %(prog)s DOC_ID --lock\n'
               '  %(prog)s DOC_ID --unlock\n'
               '  %(prog)s DOC_ID --lock-status',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument('source', help='Source: Google Doc URL/ID or Markdown file')
    parser.add_argument('destination', nargs='?', 
                       help='Destination: Google Doc URL/ID or Markdown file')
    parser.add_argument('--create', action='store_true',
                       help='Create a new Google Doc (use with markdown source)')
    parser.add_argument('-u', '--url-only', action='store_true',
                       help='Output only the URL (perfect for piping to pbcopy)')
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
    
    args = parser.parse_args()
    
    # Get credentials
    creds = get_credentials()
    
    # Determine the sync direction
    source_is_gdoc = is_google_doc(args.source)
    
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
    
    # Handle --list-revisions flag
    if args.list_revisions:
        if not source_is_gdoc:
            print("Error: --list-revisions only works with Google Docs", file=sys.stderr)
            sys.exit(1)
        
        doc_id = extract_doc_id(args.source)
        list_revisions(doc_id, creds)
        return
    
    if source_is_gdoc:
        # Google Doc → Markdown
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
    
    else:
        # Markdown → Google Doc
        
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


if __name__ == '__main__':
    main()
