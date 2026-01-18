#!/bin/bash

# Create dist directory
mkdir -p dist

# Package Chrome Extension
echo "Packaging Chrome extension..."
cd extension_chrome
zip -r ../dist/epub-downloader-chrome-v2.3.2.zip . -x "web-ext-artifacts/*" -x ".*"
cd ..
echo "Created dist/epub-downloader-chrome-v2.3.2.zip"

# Copy Firefox Extension (Signed)
echo "Copying Firefox extension..."
cp firefox_extension/web-ext-artifacts/*.xpi dist/
echo "Copied Firefox XPIs to dist/"
