---
batch:
  batch_id: batch_1-T_8kHyPdwGzeOiztJ60UIWNoiWkLSfxMnxNpIMQuac
  batch_title: Test Batch Detection
  doc_id: 1-T_8kHyPdwGzeOiztJ60UIWNoiWkLSfxMnxNpIMQuac
  heading_title: Setup Guide
  url: https://docs.google.com/document/d/1-T_8kHyPdwGzeOiztJ60UIWNoiWkLSfxMnxNpIMQuac/edit
gdoc_url: https://docs.google.com/document/d/14kt6q7FAKfWdSV3nNz8X5qMvZ49TTiSH8c2bqyS_Wk4/edit
---

# Setup Guide for mdsync

This guide will walk you through setting up Google API credentials for mdsync.

## Step 1: Install mdsync

### Option A: Homebrew (Recommended for macOS)
```bash
brew install chasemp/tap/mdsync
```

### Option B: Manual Installation
Run the setup script:
```bash
./setup.sh
```

Or manually:
```bash
pip install -r requirements.txt
```

## Step 2: Create Google Cloud Project

1. Go to the [Google Cloud Console](https://console.cloud.google.com/)
2. Click on the project dropdown at the top
3. Click "New Project"
4. Give it a name (e.g., "mdsync") and click "Create"

## Step 3: Enable Required APIs

1. In your project, go to "APIs & Services" > "Library"
2. Search for "Google Docs API" and click "Enable"
3. Search for "Google Drive API" and click "Enable"

## Step 4: Create OAuth 2.0 Credentials

1. Go to "APIs & Services" > "Credentials"
2. Click "Create Credentials" > "OAuth client ID"
3. If prompted, configure the OAuth consent screen:
   - Choose "External" (unless you have a Google Workspace)
   - Fill in the required fields (App name, User support email, Developer contact)
   - Click "Save and Continue"
   - Skip adding scopes (click "Save and Continue")
   - Add your email as a test user
   - Click "Save and Continue"
4. Back on the credentials page, click "Create Credentials" > "OAuth client ID"
5. Choose "Desktop app" as the application type
6. Give it a name (e.g., "mdsync client")
7. Click "Create"

## Step 5: Download Credentials

1. After creating the OAuth client, you'll see a dialog with your client ID and secret
2. Click "Download JSON"
3. Save the downloaded file as `credentials.json` in the mdsync directory

## Step 6: Test the Setup

Run a test command:
```bash
./mdsync.py --help
```

On first run with a Google Doc, you'll be prompted to authorize the application in your browser.

## Troubleshooting

### "credentials.json not found"
Make sure you've downloaded the OAuth credentials and saved them as `credentials.json` in the project directory.

### "Access blocked: This app's request is invalid"
Make sure you've enabled both the Google Docs API and Google Drive API in your project.

### "The caller does not have permission"
Make sure you've added your email as a test user in the OAuth consent screen configuration.

### Rate Limits
If you hit rate limits, you may need to wait or request a quota increase in the Google Cloud Console.

## Batch Documents and Table of Contents

Once you have mdsync set up, you can create batch documents that combine multiple markdown files with automatic table of contents generation.

### Creating Batch Documents with TOC

**Basic batch with table of contents:**
```bash
mdsync --batch file1.md file2.md file3.md --batch-toc
```

**Batch with file titles as headers and TOC:**
```bash
mdsync --batch file1.md file2.md --batch-headers --batch-toc
```

**Full featured batch document:**
```bash
mdsync --batch file1.md file2.md file3.md --batch-title "Project Documentation" --batch-headers --batch-horizontal-sep --batch-toc
```

### How TOC Works

- **Without `--batch-headers`**: Uses existing H1 headings (`# heading`) from your markdown files
- **With `--batch-headers`**: File titles become H1 headings (`# File Title`)
- **TOC links**: Automatically generated and clickable in Google Docs
- **Navigation**: Appears at the top of the document for easy navigation

### Example Workflow

1. **Create individual markdown files** with H1 headings:
   ```markdown
   # Getting Started
   This section covers basic setup...
   
   # Installation
   Follow these steps to install...
   ```

2. **Combine into batch document**:
   ```bash
   mdsync --batch getting-started.md installation.md --batch-toc
   ```

3. **Result**: A Google Doc with a clickable table of contents linking to each H1 section.