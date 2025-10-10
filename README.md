---
batch:
  batch_id: batch_1-T_8kHyPdwGzeOiztJ60UIWNoiWkLSfxMnxNpIMQuac
  batch_title: Test Batch Detection
  doc_id: 1-T_8kHyPdwGzeOiztJ60UIWNoiWkLSfxMnxNpIMQuac
  heading_title: Readme
  url: https://docs.google.com/document/d/1-T_8kHyPdwGzeOiztJ60UIWNoiWkLSfxMnxNpIMQuac/edit
gdoc_url: https://docs.google.com/document/d/1kokfxD3TqNm1b39Yv-2w3xuRt36XF8cYzI9vySH8idY/edit
---

# mdsync - Google Docs ↔ Markdown Sync Utility

A command-line utility to synchronize content between Google Docs and Markdown files.

## Features

- Sync from Google Docs to Markdown files
- Sync from Markdown files to Google Docs
- Sync from Markdown files to Confluence pages
- Sync from Confluence pages to Markdown files
- **Heading Management**: Create and manage documents with organized heading sections
- **Heading Operations**: Add, update, and list heading sections in Google Docs
- Leverages Google Docs native Markdown support
- Confluence note/info/warning/tip macro support
- Intelligent destination detection from frontmatter
- Live status checking for frozen documents
- **Sync Date Tracking**: Automatic timestamps for created/modified dates in frontmatter
- Simple command-line interface

## Installation

### Homebrew (Recommended)

The easiest way to install mdsync on macOS:

```bash
brew install chasemp/tap/mdsync
```

This automatically installs mdsync and all required dependencies.

### Manual Installation

#### Quick Install

```bash
# Clone the repository
git clone https://github.com/chasemp/mdsync.git
cd mdsync

# Install as a command-line tool
pip install -e .
```

Now you can use `mdsync` from anywhere!

#### Alternative: Install from GitHub

```bash
pip install git+https://github.com/chasemp/mdsync.git
```

#### Alternative: Run Without Installing

```bash
# Clone and install dependencies
git clone https://github.com/chasemp/mdsync.git
cd mdsync
pip install -r requirements.txt

# Run directly
./mdsync.py --help
```

See [INSTALL.md](INSTALL.md) for more installation options.

### Homebrew Tap

mdsync is available through the [chasemp/homebrew-tap](https://github.com/chasemp/homebrew-tap) repository. This tap provides easy installation and updates for mdsync and other tools.

```bash
# Install mdsync from the tap
brew install chasemp/tap/mdsync

# Update mdsync
brew upgrade mdsync

# Uninstall mdsync
brew uninstall mdsync
```

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

:::success
This creates a success box with a checkmark icon.
:::

:::success Task Completed
This creates a success box with a custom title.
:::
```

### Supported Macro Types

- `:::info` - Information boxes (blue)
- `:::warning` - Warning boxes (yellow/orange)  
- `:::tip` - Tip boxes (green)
- `:::note` - Note boxes (gray)
- `:::success` - Success boxes (gray with ✅ icon)

All macros support optional titles and can contain any markdown content including code blocks, links, and formatting.

## Batch Document Management

mdsync supports creating batch documents that combine multiple markdown files into a single Google Doc with automatic organization and navigation.

### Creating Batch Documents

Combine multiple markdown files into a single document:
```bash
mdsync --batch file1.md file2.md file3.md --batch-title "Project Documentation"
```

### Batch Document Options

**Include file titles as headers:**
```bash
mdsync --batch file1.md file2.md --batch-headers
```

**Add horizontal separators between files:**
```bash
mdsync --batch file1.md file2.md --batch-horizontal-sep
```

**Generate table of contents for H1 headings:**
```bash
mdsync --batch file1.md file2.md --batch-toc
```

**Full featured batch document:**
```bash
mdsync --batch file1.md file2.md --batch-title "My Project" --batch-headers --batch-horizontal-sep --batch-toc
```

### Table of Contents (TOC)

The `--batch-toc` option automatically generates a clickable table of contents based on H1 headings:

**Without `--batch-headers`:**
- Uses existing H1 headings (`# heading`) from your markdown files
- Creates clickable links to each H1 section in the Google Doc

**With `--batch-headers`:**
- File titles become H1 headings (`# File Title`)
- Creates clickable links to each file section in the Google Doc

**Example TOC output:**
```markdown
## Table of Contents

1. [Getting Started](#getting-started)
2. [Installation](#installation)
3. [Configuration](#configuration)
4. [Advanced Features](#advanced-features)
```

### Batch Management

**List all batch groupings:**
```bash
mdsync DIRECTORY --list-batch
```

**Update existing batch:**
```bash
mdsync BATCH_NAME --batch-update
```

**Diff batch against Google Doc:**
```bash
mdsync DOC_ID --diff-batch
```

## Heading Management

mdsync supports creating and managing documents with organized heading sections that appear in the Google Docs outline navigation.

### Creating Documents with Heading Sections

Create an empty document for heading management:
```bash
mdsync --create-empty "Project Documentation"
```

Add markdown files as heading sections to existing documents:
```bash
mdsync overview.md DOC_ID --heading
mdsync development.md DOC_ID --heading
mdsync testing.md DOC_ID --heading
```

### Heading Operations

List all headings in a document:
```bash
mdsync DOC_ID --list-headings
```

Update an existing heading section:
```bash
mdsync updated-content.md DOC_ID --update-heading --heading-title "Overview"
```

### Heading Structure

- **H1 headings** become top-level sections in the Google Docs outline
- **H2 headings** become sub-sections in the outline
- **H3+ headings** become subsections within sections

### Frontmatter Integration

Use frontmatter to configure heading behavior:

```yaml
---
title: "Project Overview"
gdoc_url: "https://docs.google.com/document/d/DOC_ID/edit"
---
# Project Overview Content
```

The `title` field is used as the tab name, and `gdoc_url` can include heading fragments for tab targeting.

### Tab Workflow Examples

**Complete project documentation setup:**
```bash
# 1. Create empty document
mdsync --create-empty "Software Project"

# 2. Add project sections as tabs
mdsync overview.md DOC_ID --tab
mdsync requirements.md DOC_ID --tab
mdsync architecture.md DOC_ID --tab
mdsync testing.md DOC_ID --tab

# 3. List all tabs
mdsync DOC_ID --list-tabs

# 4. Update a specific tab
mdsync updated-requirements.md DOC_ID --update-tab --tab-title "Requirements"
```

**Team collaboration workflow:**
```bash
# Each team member maintains their own markdown file
mdsync frontend-guide.md DOC_ID --tab --tab-title "Frontend"
mdsync backend-guide.md DOC_ID --tab --tab-title "Backend"
mdsync deployment-guide.md DOC_ID --tab --tab-title "Deployment"
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