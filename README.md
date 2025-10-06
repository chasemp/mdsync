# mdsync - Google Docs ↔ Markdown Sync Utility

A command-line utility to synchronize content between Google Docs and Markdown files.

## Features

- Sync from Google Docs to Markdown files
- Sync from Markdown files to Google Docs
- Leverages Google Docs native Markdown support
- Simple command-line interface

## Installation

1. Clone this repository
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Set up Google API credentials:
   - Go to [Google Cloud Console](https://console.cloud.google.com/)
   - Create a new project or select an existing one
   - Enable the Google Docs API
   - Create OAuth 2.0 credentials (Desktop app)
   - Download the credentials and save as `credentials.json` in the project directory

## Usage

```bash
python mdsync.py <source> <destination>
```

### Examples

Sync from Google Doc to Markdown file:
```bash
python mdsync.py "https://docs.google.com/document/d/YOUR_DOC_ID/edit" output.md
```

Sync from Markdown file to Google Doc:
```bash
python mdsync.py input.md "https://docs.google.com/document/d/YOUR_DOC_ID/edit"
```

You can also use just the document ID:
```bash
python mdsync.py YOUR_DOC_ID output.md
```

## First Run

On first run, the script will:
1. Open your browser for Google authentication
2. Ask you to grant permissions to access Google Docs
3. Save a token for future use

## Requirements

- Python 3.7+
- Google account
- Google Docs API credentials

## How It Works

- **Google Docs → Markdown**: Uses the Google Docs API to export documents in Markdown format
- **Markdown → Google Docs**: Imports Markdown content into Google Docs using the native import API

## Limitations

- Point-in-time sync only (no continuous sync or conflict resolution)
- Requires internet connection
- Subject to Google API rate limits
