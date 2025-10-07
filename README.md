# mdsync - Google Docs ↔ Markdown Sync Utility

A command-line utility to synchronize content between Google Docs and Markdown files.

## Features

- Sync from Google Docs to Markdown files
- Sync from Markdown files to Google Docs
- Sync from Markdown files to Confluence pages
- Sync from Confluence pages to Markdown files
- Leverages Google Docs native Markdown support
- Confluence note/info/warning/tip macro support
- Intelligent destination detection from frontmatter
- Live status checking for frozen documents
- Simple command-line interface

## Installation

### Quick Install

```bash
# Clone the repository
git clone https://github.com/chasemp/mdsync.git
cd mdsync

# Install as a command-line tool
pip install -e .
```

Now you can use `mdsync` from anywhere!

### Alternative: Install from GitHub

```bash
pip install git+https://github.com/chasemp/mdsync.git
```

### Alternative: Run Without Installing

```bash
# Clone and install dependencies
git clone https://github.com/chasemp/mdsync.git
cd mdsync
pip install -r requirements.txt

# Run directly
./mdsync.py --help
```

See [INSTALL.md](INSTALL.md) for more installation options.

### Set up Google API credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select an existing one
3. Enable the Google Docs API and Google Drive API
4. Create OAuth 2.0 credentials (Desktop app)
5. Download the credentials and save as `credentials.json` in one of:
   - Current directory
   - `~/.config/mdsync/credentials.json`
   - `~/.mdsync/credentials.json`

See [SETUP_GUIDE.md](SETUP_GUIDE.md) for detailed setup instructions.

## Usage

```bash
mdsync <source> <destination>
```

### Examples

Sync from Google Doc to Markdown file:
```bash
mdsync "https://docs.google.com/document/d/YOUR_DOC_ID/edit" output.md
```

Sync from Markdown file to Google Doc:
```bash
mdsync input.md "https://docs.google.com/document/d/YOUR_DOC_ID/edit"
```

Create a new Google Doc from Markdown:
```bash
mdsync input.md --create
```

Create a new doc and copy URL to clipboard:
```bash
mdsync input.md --create -u | pbcopy
```

You can also use just the document ID:
```bash
mdsync YOUR_DOC_ID output.md
```

> **Note:** If you didn't install with pip, use `./mdsync.py` instead of `mdsync`

## Confluence Notes and Macros

mdsync supports creating Confluence note, info, warning, and tip macros from markdown. This allows you to create visually distinct callout boxes in your Confluence pages.

### Block Quote Notes

Regular markdown block quotes are automatically converted to Confluence note macros:

```markdown
> This is an important note that will appear in a Confluence note box.
```

### Special Syntax Macros

You can use special syntax to create different types of Confluence macros:

```markdown
:::info Important Information
This creates an info box with a title.
:::

:::warning Security Warning
This creates a warning box with a title.
:::

:::tip Pro Tip
This creates a tip box with a title.
:::

:::note Implementation Note
This creates a note box with a title.
:::

:::note
This creates a note box without a title.
:::
```

### Supported Macro Types

- `:::info` - Information boxes (blue)
- `:::warning` - Warning boxes (yellow/orange)  
- `:::tip` - Tip boxes (green)
- `:::note` - Note boxes (gray)

All macros support optional titles and can contain any markdown content including code blocks, links, and formatting.

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
