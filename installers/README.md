# Dala Installers

These files install or update the Dala Python server from PyPI. They do not install the browser extension.

## Windows

Double-click:

```text
Install or Update Dala.bat
```

The batch file runs the PowerShell installer. The installer creates a Desktop launcher named `Start Dala Server.bat`.

## macOS

Double-click:

```text
Install or Update Dala.command
```

macOS may require right-clicking the file and choosing **Open** the first time. The installer creates a Desktop launcher named `Start Dala Server.command`.

## Linux

Run:

```bash
sh "Install or Update Dala.sh"
```

## Headless Browser Support

The installers ask whether to add optional headless browser support. This lets the Dala server control Chrome/Chromium in the background. It is needed for PDF output and some JavaScript-heavy pages, and it is separate from the normal Dala browser extension.
