#!/bin/bash

# Create dist directory
mkdir -p dist

# Package Chrome Extension
echo "Packaging Chrome extension..."
cd extension_chrome
zip -r ../dist/dala-chrome-v2.3.1.zip . -x "web-ext-artifacts/*" -x ".*"
cd ..
echo "Created dist/dala-chrome-v2.3.1.zip"