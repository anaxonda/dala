# Dala Installers

These files install or update the Dala Python server from PyPI. They install `uv` only if it is missing, ask whether to add optional headless browser/PDF support, and do not install the browser extension.

These wrappers are for Windows, macOS, and desktop Linux. On Android/Termux, use `scripts/install-dala.sh`; it uses Termux Python plus `pip --user` instead of `uv tool install`. The release bundle also includes `android/` Termux:Widget shortcuts for starting, stopping, and checking the installed server.

After installation, start Dala with the Desktop launcher if one was created, or run:

```bash
dala-server
```

`dala-server` opens the local status page by default. Use `dala-server --no-open` for background services or SSH sessions.

## Windows

Double-click:

```text
Install or Update Dala.bat
```

The batch file runs the PowerShell installer. The installer creates a Desktop launcher named `Start Dala Server.bat`.

Manual PowerShell fallback:

```powershell
.\Install or Update Dala.ps1
```

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

The Linux installer does not create a `.desktop` file automatically. Use `launchers/dala-server.desktop` from the release bundle as a template if you want a desktop launcher.

## Headless Browser Support

The Windows and macOS installers ask whether to add optional headless browser support. This lets the Dala server control Chrome/Chromium in the background. It is needed for PDF output and some JavaScript-heavy pages, and it is separate from the normal Dala browser extension.

Linux users can pass the option directly:

```bash
sh "Install or Update Dala.sh" --headless-browser
```
