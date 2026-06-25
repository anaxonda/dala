# Android / Termux

Use the main shell installer from Termux. It detects Android, avoids `uv tool install`, installs Termux native packages for heavy image/HTML dependencies, and creates `dala`/`dala-server` wrappers in `~/.local/bin`.

```bash
pkg update
pkg install python python-pip curl
curl -fsSLo install-dala.sh https://raw.githubusercontent.com/anaxonda/dala/main/scripts/install-dala.sh
sh install-dala.sh
dala-server --no-open
```

Open `http://127.0.0.1:8000/` from the Android browser, or point the Dala browser extension at that local server.

## Termux Widget Shortcuts

The scripts in this directory are for Termux:Widget. They manage the installed `dala-server` command, not a source checkout.

```bash
pkg install termux-api
mkdir -p ~/.shortcuts/dala
cp android/dala_start.sh ~/.shortcuts/dala/start.sh
cp android/dala_stop.sh ~/.shortcuts/dala/stop.sh
cp android/dala_status.sh ~/.shortcuts/dala/status.sh
chmod +x ~/.shortcuts/dala/*.sh
```

Then add the Termux:Widget shortcut to the Android home screen.

Files used by the scripts:

- PID: `~/.cache/dala/server.pid`
- Log: `~/.logs/dala-server.log`
- URL: `http://127.0.0.1:8000/`

Set `DALA_PORT` before launching if you need a different port.
