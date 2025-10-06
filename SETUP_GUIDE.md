# Setup Guide for mdsync

This guide will walk you through setting up Google API credentials for mdsync.

## Step 1: Install Dependencies

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
