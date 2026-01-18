# keyboard shortcut implmentation

this extension can absolutely be triggered via keyboard shortcuts using the content script approach! Here's how to add keyboard triggering to your EPUB downloader:

## Implementation Strategy

Add a content script that listens for a keyboard shortcut and triggers the same download flow that the context menu uses.

### 1. Update manifest.json

```json
{
  "manifest_version": 2,
  "name": "EPUB Downloader",
  "content_scripts": [
    {
      "matches": ["<all_urls>"],
      "js": ["keyboard-trigger.js"],
      "run_at": "document_idle"
    },
    {
      "matches": ["<all_urls>"],
      "js": ["content.js"],  // Your existing content script
      "run_at": "document_idle"
    }
  ],
  // ... rest of your manifest
}
```

### 2. Create keyboard-trigger.js

```javascript
// Listen for keyboard shortcut to trigger download
document.addEventListener('keydown', function(event) {
  // Example: Ctrl+Shift+E to download current page to EPUB
  if (event.ctrlKey && event.shiftKey && event.key === 'E') {
    // Don't trigger if user is typing
    const tag = event.target.tagName.toLowerCase();
    if (tag === 'input' || tag === 'textarea' || event.target.isContentEditable) {
      return;
    }
    
    event.preventDefault();
    event.stopPropagation();
    
    // Trigger the download for current page
    triggerEpubDownload();
  }
}, true);

function triggerEpubDownload() {
  // Send message to background script to initiate download
  // This mimics what the context menu does
  browser.runtime.sendMessage({
    action: 'downloadCurrentPage',
    url: window.location.href,
    title: document.title
  });
  
  // Optional: Show visual feedback
  showDownloadNotification();
}

function showDownloadNotification() {
  // Create a temporary notification element
  const notification = document.createElement('div');
  notification.textContent = 'Starting EPUB download...';
  notification.style.cssText = `
    position: fixed;
    top: 20px;
    right: 20px;
    background: #4CAF50;
    color: white;
    padding: 12px 24px;
    border-radius: 4px;
    z-index: 999999;
    font-family: sans-serif;
    box-shadow: 0 2px 5px rgba(0,0,0,0.2);
  `;
  document.body.appendChild(notification);
  
  setTimeout(() => {
    notification.remove();
  }, 3000);
}
```

### 3. Update background.js

Add a message listener to handle the keyboard-triggered download:

```javascript
// Handle messages from keyboard trigger
browser.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.action === 'downloadCurrentPage') {
    // Use the same logic as your context menu handler
    // Trigger the content script to collect HTML + assets
    browser.tabs.sendMessage(sender.tab.id, {
      action: 'collectPageData'
    }).then(payload => {
      // Send to your FastAPI backend
      return fetch('http://127.0.0.1:8000/convert', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
      });
    }).then(response => response.blob())
    .then(epubBlob => {
      // Deliver via downloads API or new tab
      if (browser.downloads) {
        const url = URL.createObjectURL(epubBlob);
        browser.downloads.download({
          url: url,
          filename: `${sanitizeFilename(message.title)}.epub`,
          saveAs: true
        });
      } else {
        // Fallback: open in new tab
        const url = URL.createObjectURL(epubBlob);
        browser.tabs.create({ url: url });
      }
    }).catch(error => {
      console.error('Download failed:', error);
    });
  }
});

function sanitizeFilename(title) {
  return title.replace(/[^a-z0-9]/gi, '_').substring(0, 100);
}
```

### 4. Update content.js

Make sure your existing content script can respond to the collection request:

```javascript
// Listen for messages from background script
browser.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.action === 'collectPageData') {
    // Your existing logic to collect HTML + assets
    const payload = {
      url: window.location.href,
      title: document.title,
      html: document.documentElement.outerHTML,
      assets: collectAssets(),
      // ... other data your backend needs
    };
    
    sendResponse(payload);
    return true; // Keep message channel open for async response
  }
});

function collectAssets() {
  // Your existing asset collection logic
  // e.g., grab inline images, etc.
  return [];
}
```

## Alternative: Multiple Shortcuts

You could add multiple keyboard shortcuts for different workflows:

```javascript
document.addEventListener('keydown', function(event) {
  const tag = event.target.tagName.toLowerCase();
  const isTyping = tag === 'input' || tag === 'textarea' || event.target.isContentEditable;
  
  if (isTyping) return;
  
  // Ctrl+Shift+E: Download current page immediately
  if (event.ctrlKey && event.shiftKey && event.key === 'E') {
    event.preventDefault();
    browser.runtime.sendMessage({
      action: 'downloadCurrentPage',
      url: window.location.href,
      title: document.title
    });
  }
  
  // Ctrl+Shift+Q: Add to queue (open popup)
  if (event.ctrlKey && event.shiftKey && event.key === 'Q') {
    event.preventDefault();
    browser.runtime.sendMessage({
      action: 'addToQueue',
      url: window.location.href
    });
  }
}, true);
```

## Benefits of This Approach

1. **Works on Android Firefox** - Uses content scripts instead of broken `commands` API
2. **Integrates with existing flow** - Reuses your context menu → content script → backend pipeline
3. **Power user friendly** - Keyboard shortcuts are faster than right-click menu
4. **Visual feedback** - Optional notification confirms the action
5. **Flexible** - Can add multiple shortcuts for different workflows (queue vs immediate download)

The key insight is that keyboard shortcuts just become another UI trigger alongside your popup and context menu, but they all funnel into the same collection → conversion → delivery pipeline you've already built.

On Android Firefox, **user remapping is very limited** because the `commands` API (which provides built-in remapping in desktop Firefox) doesn't work. You'll need to implement your own settings system.

## Implementation Options

### Option 1: Simple Settings in Extension Storage (Recommended)

Create a settings page where users can customize their shortcuts:

**manifest.json:**
```json
{
  "manifest_version": 2,
  "name": "EPUB Downloader",
  "permissions": ["storage"],
  "options_ui": {
    "page": "settings.html",
    "open_in_tab": true
  },
  "content_scripts": [
    {
      "matches": ["<all_urls>"],
      "js": ["keyboard-trigger.js"]
    }
  ]
}
```

**settings.html:**
```html
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>EPUB Downloader Settings</title>
  <style>
    body {
      font-family: sans-serif;
      max-width: 600px;
      margin: 40px auto;
      padding: 20px;
    }
    .shortcut-setting {
      margin: 20px 0;
      padding: 15px;
      border: 1px solid #ddd;
      border-radius: 4px;
    }
    label {
      display: block;
      margin-bottom: 8px;
      font-weight: bold;
    }
    input {
      width: 100%;
      padding: 8px;
      font-family: monospace;
      font-size: 14px;
    }
    button {
      margin-top: 20px;
      padding: 10px 20px;
      background: #4CAF50;
      color: white;
      border: none;
      border-radius: 4px;
      cursor: pointer;
    }
    button:hover {
      background: #45a049;
    }
    .hint {
      color: #666;
      font-size: 12px;
      margin-top: 4px;
    }
    .status {
      margin-top: 10px;
      padding: 10px;
      background: #e7f3e7;
      border-radius: 4px;
      display: none;
    }
  </style>
</head>
<body>
  <h1>Keyboard Shortcuts</h1>
  
  <div class="shortcut-setting">
    <label for="downloadShortcut">Download Current Page to EPUB</label>
    <input type="text" id="downloadShortcut" placeholder="Press your desired shortcut...">
    <div class="hint">Click and press your desired key combination (e.g., Ctrl+Shift+E)</div>
  </div>
  
  <div class="shortcut-setting">
    <label for="queueShortcut">Add to Download Queue</label>
    <input type="text" id="queueShortcut" placeholder="Press your desired shortcut...">
    <div class="hint">Click and press your desired key combination (e.g., Ctrl+Shift+Q)</div>
  </div>
  
  <button id="save">Save Settings</button>
  <button id="reset">Reset to Defaults</button>
  
  <div class="status" id="status">Settings saved!</div>
  
  <script src="settings.js"></script>
</body>
</html>
```

**settings.js:**
```javascript
const DEFAULT_SHORTCUTS = {
  download: { ctrl: true, shift: true, key: 'E' },
  queue: { ctrl: true, shift: true, key: 'Q' }
};

// Load current settings
async function loadSettings() {
  const result = await browser.storage.sync.get('shortcuts');
  return result.shortcuts || DEFAULT_SHORTCUTS;
}

// Save settings
async function saveSettings(shortcuts) {
  await browser.storage.sync.set({ shortcuts });
}

// Convert shortcut object to display string
function shortcutToString(shortcut) {
  const parts = [];
  if (shortcut.ctrl) parts.push('Ctrl');
  if (shortcut.alt) parts.push('Alt');
  if (shortcut.shift) parts.push('Shift');
  if (shortcut.meta) parts.push('Meta');
  parts.push(shortcut.key);
  return parts.join('+');
}

// Capture keyboard input
function setupShortcutCapture(inputId, action) {
  const input = document.getElementById(inputId);
  
  input.addEventListener('keydown', function(event) {
    event.preventDefault();
    
    // Ignore modifier-only presses
    if (['Control', 'Shift', 'Alt', 'Meta'].includes(event.key)) {
      return;
    }
    
    const shortcut = {
      ctrl: event.ctrlKey,
      shift: event.shiftKey,
      alt: event.altKey,
      meta: event.metaKey,
      key: event.key
    };
    
    input.value = shortcutToString(shortcut);
    input.dataset.shortcut = JSON.stringify(shortcut);
  });
  
  input.addEventListener('focus', function() {
    input.value = '';
    input.placeholder = 'Press your desired shortcut...';
  });
}

// Initialize
document.addEventListener('DOMContentLoaded', async function() {
  const shortcuts = await loadSettings();
  
  // Display current shortcuts
  document.getElementById('downloadShortcut').value = shortcutToString(shortcuts.download);
  document.getElementById('downloadShortcut').dataset.shortcut = JSON.stringify(shortcuts.download);
  
  document.getElementById('queueShortcut').value = shortcutToString(shortcuts.queue);
  document.getElementById('queueShortcut').dataset.shortcut = JSON.stringify(shortcuts.queue);
  
  // Setup capture
  setupShortcutCapture('downloadShortcut', 'download');
  setupShortcutCapture('queueShortcut', 'queue');
  
  // Save button
  document.getElementById('save').addEventListener('click', async function() {
    const downloadInput = document.getElementById('downloadShortcut');
    const queueInput = document.getElementById('queueShortcut');
    
    const newShortcuts = {
      download: JSON.parse(downloadInput.dataset.shortcut || JSON.stringify(DEFAULT_SHORTCUTS.download)),
      queue: JSON.parse(queueInput.dataset.shortcut || JSON.stringify(DEFAULT_SHORTCUTS.queue))
    };
    
    await saveSettings(newShortcuts);
    
    // Show confirmation
    const status = document.getElementById('status');
    status.style.display = 'block';
    setTimeout(() => { status.style.display = 'none'; }, 3000);
  });
  
  // Reset button
  document.getElementById('reset').addEventListener('click', async function() {
    await saveSettings(DEFAULT_SHORTCUTS);
    location.reload();
  });
});
```

**keyboard-trigger.js (updated to use settings):**
```javascript
let shortcuts = null;

// Load shortcuts from storage
async function loadShortcuts() {
  const result = await browser.storage.sync.get('shortcuts');
  shortcuts = result.shortcuts || {
    download: { ctrl: true, shift: true, key: 'E' },
    queue: { ctrl: true, shift: true, key: 'Q' }
  };
}

// Check if event matches shortcut
function matchesShortcut(event, shortcut) {
  return event.ctrlKey === !!shortcut.ctrl &&
         event.shiftKey === !!shortcut.shift &&
         event.altKey === !!shortcut.alt &&
         event.metaKey === !!shortcut.meta &&
         event.key === shortcut.key;
}

// Initialize
loadShortcuts();

// Listen for shortcut changes
browser.storage.onChanged.addListener((changes, area) => {
  if (area === 'sync' && changes.shortcuts) {
    shortcuts = changes.shortcuts.newValue;
  }
});

// Keyboard listener
document.addEventListener('keydown', function(event) {
  if (!shortcuts) return;
  
  // Don't trigger if user is typing
  const tag = event.target.tagName.toLowerCase();
  if (tag === 'input' || tag === 'textarea' || event.target.isContentEditable) {
    return;
  }
  
  // Check download shortcut
  if (matchesShortcut(event, shortcuts.download)) {
    event.preventDefault();
    event.stopPropagation();
    browser.runtime.sendMessage({
      action: 'downloadCurrentPage',
      url: window.location.href,
      title: document.title
    });
  }
  
  // Check queue shortcut
  if (matchesShortcut(event, shortcuts.queue)) {
    event.preventDefault();
    event.stopPropagation();
    browser.runtime.sendMessage({
      action: 'addToQueue',
      url: window.location.href
    });
  }
}, true);
```

### Option 2: Simple Text-Based Config

For a lighter approach, let users enter shortcuts as text strings:

**settings.html (simplified):**
```html
<div class="shortcut-setting">
  <label for="downloadShortcut">Download Current Page</label>
  <input type="text" id="downloadShortcut" value="Ctrl+Shift+E">
  <div class="hint">Format: Ctrl+Shift+E or Alt+D (case-sensitive)</div>
</div>
```

**Parse user input:**
```javascript
function parseShortcut(str) {
  const parts = str.split('+').map(s => s.trim());
  const key = parts.pop();
  
  return {
    ctrl: parts.includes('Ctrl'),
    shift: parts.includes('Shift'),
    alt: parts.includes('Alt'),
    meta: parts.includes('Meta'),
    key: key
  };
}
```

### Option 3: Popup Quick Settings

Add a shortcuts section to your existing popup:

```html
<!-- In your popup.html -->
<details>
  <summary>⌨️ Keyboard Shortcuts</summary>
  <div>
    <p>Download page: <strong id="currentDownloadShortcut">Ctrl+Shift+E</strong> 
       <button id="editDownloadShortcut">Edit</button>
    </p>
    <p>Add to queue: <strong id="currentQueueShortcut">Ctrl+Shift+Q</strong> 
       <button id="editQueueShortcut">Edit</button>
    </p>
  </div>
</details>
```

## Key Points

1. **Storage API** - Use `browser.storage.sync` to persist settings across devices (or `browser.storage.local` for device-only)

2. **Real-time Updates** - Listen to `storage.onChanged` in content scripts so changes take effect immediately without reload

3. **Validation** - Warn users about conflicts with browser shortcuts or invalid combinations

4. **Defaults** - Always provide sensible defaults that work out of the box

5. **User Experience** - The interactive key capture approach (Option 1) is most user-friendly since users don't need to know key names

The settings page is accessible via the extension's options in the Firefox Add-ons menu, making it easy for users to find and customize their shortcuts.

Yes! The approach can be **completely shared** between desktop and Android Firefox. In fact, this is one of the major advantages of implementing custom keyboard shortcuts this way.

## Why It Works Across Both Platforms

1. **Content scripts work identically** on desktop and Android Firefox
2. **Storage API is cross-platform** - `browser.storage.sync` even syncs settings between devices
3. **Same message passing** - `browser.runtime.sendMessage` works the same way
4. **Settings UI portable** - The options page works on both platforms

## Unified Implementation Strategy

You can use **one codebase** for both platforms with minimal platform-specific code:

### manifest.json (shared)
```json
{
  "manifest_version": 2,
  "name": "EPUB Downloader",
  "permissions": ["storage", "downloads", "contextMenus"],
  "browser_specific_settings": {
    "gecko": {
      "id": "epub-downloader@example.com"
    }
  },
  "background": {
    "scripts": ["background.js"]
  },
  "content_scripts": [
    {
      "matches": ["<all_urls>"],
      "js": ["keyboard-trigger.js", "content.js"]
    }
  ],
  "options_ui": {
    "page": "settings.html",
    "open_in_tab": true
  },
  "browser_action": {
    "default_popup": "popup.html"
  }
}
```

### Enhanced for Desktop: Add Optional `commands` API

You can **layer on** the native `commands` API for desktop while keeping the custom implementation as a fallback:

**manifest.json (with optional commands for desktop):**
```json
{
  "commands": {
    "download-page": {
      "suggested_key": {
        "default": "Ctrl+Shift+E"
      },
      "description": "Download current page to EPUB"
    },
    "add-to-queue": {
      "suggested_key": {
        "default": "Ctrl+Shift+Q"
      },
      "description": "Add to download queue"
    }
  }
}
```

**background.js (handles both approaches):**
```javascript
// Desktop: Native commands API (if available)
if (browser.commands) {
  browser.commands.onCommand.addListener((command) => {
    switch (command) {
      case 'download-page':
        handleDownloadCurrentPage();
        break;
      case 'add-to-queue':
        handleAddToQueue();
        break;
    }
  });
}

// Shared: Handle messages from content script (works on both platforms)
browser.runtime.onMessage.addListener((message, sender, sendResponse) => {
  switch (message.action) {
    case 'downloadCurrentPage':
      handleDownloadCurrentPage(sender.tab.id, message.url, message.title);
      break;
    case 'addToQueue':
      handleAddToQueue(message.url);
      break;
  }
});

async function handleDownloadCurrentPage(tabId, url, title) {
  // Your shared download logic
  // Works whether triggered from commands API or content script
}

async function handleAddToQueue(url) {
  // Your shared queue logic
}
```

## Platform Detection (if needed)

For the rare cases where you need platform-specific behavior:

```javascript
// Detect platform
const platformInfo = await browser.runtime.getPlatformInfo();
const isAndroid = platformInfo.os === 'android';

// Or check for API availability
const hasNativeCommands = typeof browser.commands !== 'undefined';

if (isAndroid) {
  // Android-specific behavior
} else {
  // Desktop-specific behavior
}
```

## Settings UI: Adaptive Approach

Your settings page can show different options based on platform:

**settings.html:**
```html
<div id="desktop-commands" style="display: none;">
  <h2>Native Keyboard Shortcuts (Desktop)</h2>
  <p>You can customize these shortcuts in Firefox's built-in shortcut manager:</p>
  <button id="openShortcutManager">Open Shortcut Manager</button>
  <hr>
</div>

<div id="custom-shortcuts">
  <h2>Custom Keyboard Shortcuts</h2>
  <p id="custom-shortcuts-note"></p>
  
  <div class="shortcut-setting">
    <label for="downloadShortcut">Download Current Page to EPUB</label>
    <input type="text" id="downloadShortcut" placeholder="Press your desired shortcut...">
  </div>
  
  <!-- More shortcuts... -->
</div>
```

**settings.js:**
```javascript
document.addEventListener('DOMContentLoaded', async function() {
  const platformInfo = await browser.runtime.getPlatformInfo();
  const isAndroid = platformInfo.os === 'android';
  
  if (isAndroid) {
    // Android: Only show custom shortcuts
    document.getElementById('custom-shortcuts-note').textContent = 
      'Configure your keyboard shortcuts below:';
  } else {
    // Desktop: Show both options
    document.getElementById('desktop-commands').style.display = 'block';
    document.getElementById('custom-shortcuts-note').textContent = 
      'These custom shortcuts work in addition to Firefox\'s native shortcuts:';
    
    // Button to open Firefox's shortcut manager
    document.getElementById('openShortcutManager').addEventListener('click', () => {
      browser.tabs.create({ url: 'about:addons' });
      // User can then navigate to Extensions > Gear icon > Manage Extension Shortcuts
    });
  }
  
  // Rest of settings logic (shared)...
});
```

## Best of Both Worlds Strategy

**Recommended approach:**

1. **Desktop**: Use native `commands` API as primary method (better OS integration, shows in Firefox's shortcut manager)
2. **Both platforms**: Keep custom content script shortcuts as backup/alternative
3. **Settings sync**: Use `browser.storage.sync` so custom shortcuts sync across devices

**Why this is optimal:**

- Desktop users get native OS-level shortcuts that work even when not focused on the page
- Android users get working shortcuts via content scripts
- Custom shortcuts work on both as a fallback or supplement
- Single codebase, no duplication
- Settings sync between devices automatically

## Storage Sync Example

This is particularly powerful with `browser.storage.sync`:

```javascript
// User sets custom shortcut on desktop
await browser.storage.sync.set({
  shortcuts: {
    download: { ctrl: true, shift: true, key: 'D' }
  }
});

// Automatically syncs to their Android Firefox
// Works immediately on both devices
```

## Summary

**Yes, the approach is 100% shareable!**

- ✅ Same manifest (with optional `commands` for desktop enhancement)
- ✅ Same content scripts (`keyboard-trigger.js`)
- ✅ Same background script logic
- ✅ Same settings UI (with minor adaptive display)
- ✅ Settings sync automatically via `browser.storage.sync`
- ✅ Works on both platforms without platform-specific forks

The only real difference is that desktop can *also* use the native `commands` API for enhanced functionality, but the custom content script approach works identically on both platforms as a unified fallback.


