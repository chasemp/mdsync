#!/bin/bash
# Setup script for mdsync

echo "Setting up mdsync..."

# Check if Python 3 is installed
if ! command -v python3 &> /dev/null; then
    echo "Error: Python 3 is required but not installed."
    exit 1
fi

echo "Python 3 found: $(python3 --version)"

# Create virtual environment (optional but recommended)
read -p "Create a virtual environment? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    python3 -m venv venv
    echo "Virtual environment created. Activate it with: source venv/bin/activate"
    source venv/bin/activate
fi

# Install dependencies
echo "Installing dependencies..."
pip install -r requirements.txt

# Make the script executable
chmod +x mdsync.py

echo ""
echo "Setup complete!"
echo ""
echo "Next steps:"
echo "1. Go to https://console.cloud.google.com/"
echo "2. Create a new project or select an existing one"
echo "3. Enable the Google Docs API and Google Drive API"
echo "4. Create OAuth 2.0 credentials (Desktop app)"
echo "5. Download the credentials and save as 'credentials.json' in this directory"
echo ""
echo "Then you can run: ./mdsync.py <source> <destination>"
