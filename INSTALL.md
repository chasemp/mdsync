---
batch:
  batch_id: batch_1QimKT4Fd-ibsyS7eXOeE0myXs1v3PaS5jdER6dZfdGw
  batch_title: Order Test Fixed
  doc_id: 1QimKT4Fd-ibsyS7eXOeE0myXs1v3PaS5jdER6dZfdGw
  heading_title: Install
  url: https://docs.google.com/document/d/1QimKT4Fd-ibsyS7eXOeE0myXs1v3PaS5jdER6dZfdGw/edit#heading=install
---

# Installation Guide

## Method 1: Install Locally (Recommended)

Install `mdsync` as a command-line tool that you can run from anywhere:

```bash
# From the project directory
pip install -e .
```

The `-e` flag installs in "editable" mode, so any changes you make to the code will be reflected immediately.

After installation, you can use the `mdsync` command from anywhere:

```bash
mdsync --help
mdsync file.md --create -u | pbcopy
```

### Uninstall

```bash
pip uninstall mdsync
```

## Method 2: Install from GitHub

You can also install directly from GitHub:

```bash
pip install git+https://github.com/chasemp/mdsync.git
```

## Method 3: Run Directly (No Installation)

If you prefer not to install, you can run the script directly:

```bash
./mdsync.py --help
```

## Verify Installation

After installation, verify it works:

```bash
mdsync --help
```

You should see the help message with all available options.

## Setting Up Credentials

Regardless of installation method, you still need to set up Google API credentials:

1. Follow the instructions in [SETUP_GUIDE.md](SETUP_GUIDE.md)
2. Place your `credentials.json` file in:
   - **If installed**: `~/.config/mdsync/credentials.json` (or current directory)
   - **If running directly**: Same directory as `mdsync.py`

The script will look for credentials in the following order:
1. Current directory
2. `~/.config/mdsync/`
3. `~/.mdsync/`

## Virtual Environment (Optional but Recommended)

If you want to keep dependencies isolated:

```bash
# Create virtual environment
python3 -m venv venv

# Activate it
source venv/bin/activate  # On macOS/Linux
# or
venv\Scripts\activate  # On Windows

# Install
pip install -e .

# Use mdsync
mdsync --help
```

## Troubleshooting

### Command not found

If `mdsync` command is not found after installation:

1. Make sure pip's bin directory is in your PATH
2. Try reinstalling: `pip uninstall mdsync && pip install -e .`
3. Check where it was installed: `pip show mdsync`

### Permission denied

If you get permission errors:

```bash
# Use --user flag
pip install --user -e .
```