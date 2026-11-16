"""Explicit, local-only browser captures for extraction debugging."""
import json
import os
import tempfile
from pathlib import Path
from uuid import uuid4


MAX_CAPTURE_BYTES = 128 * 1024 * 1024
MAX_COMPRESSED_CAPTURE_BYTES = 32 * 1024 * 1024


def save_debug_capture(payload: dict) -> dict:
    if not isinstance(payload.get("html"), str) or not payload["html"]:
        raise ValueError("Page HTML is required.")
    user_id = getattr(os, "getuid", lambda: "local")()
    root = Path(os.getenv("DALA_DEBUG_DIR") or Path(tempfile.gettempdir()) / f"dala-debug-{user_id}")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    capture_id = uuid4().hex
    directory = root / capture_id
    directory.mkdir(mode=0o700)
    files = {}
    for key, filename in (("html", "page.html"), ("rendered_html", "rendered.html")):
        if isinstance(payload.get(key), str):
            path = directory / filename
            path.write_text(payload[key], encoding="utf-8")
            path.chmod(0o600)
            files[key] = str(path)
    # Keep source and metadata separate so diagnostic HTML need never execute.
    metadata = {key: value for key, value in payload.items() if key not in {"html", "rendered_html"}}
    path = directory / "details.json"
    path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    path.chmod(0o600)
    files["details"] = str(path)
    return {"capture_id": capture_id, "directory": str(directory), "files": files}
