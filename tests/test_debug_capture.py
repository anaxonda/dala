import json
import gzip
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from dala.server import app


def test_debug_capture_saves_source_and_diagnostics_privately(tmp_path, monkeypatch):
    monkeypatch.setenv("DALA_DEBUG_DIR", str(tmp_path))
    payload = {"url": "https://app.ft.com/content/test", "html": "<article>Story</article>",
               "rendered_html": '<article><img src="selected.jpg"></article>',
               "images": [{"current_src": "selected.jpg", "ancestors": [{"tag": "BUTTON"}]}]}
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        response = client.post("/helper/debug-capture", json=payload)
    assert response.status_code == 200
    result = response.json()
    directory = Path(result["directory"])
    assert directory.parent == tmp_path
    assert (directory / "page.html").read_text() == payload["html"]
    assert (directory / "rendered.html").read_text() == payload["rendered_html"]
    details = json.loads((directory / "details.json").read_text())
    assert details["images"] == payload["images"]
    assert "html" not in details
    assert directory.stat().st_mode & 0o077 == 0
    assert all(path.stat().st_mode & 0o077 == 0 for path in directory.iterdir())


def test_debug_capture_rejects_remote_connections(tmp_path, monkeypatch):
    monkeypatch.setenv("DALA_DEBUG_DIR", str(tmp_path))
    with TestClient(app, client=("192.168.1.2", 5000)) as client:
        response = client.post("/helper/debug-capture", json={"html": "page"})
    assert response.status_code == 403
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("body", [b"[]", b"not json", b'{"html":123}'])
def test_debug_capture_rejects_invalid_source(body):
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        response = client.post("/helper/debug-capture", content=body)
    assert response.status_code == 400


def test_debug_capture_enforces_upload_limit(monkeypatch):
    monkeypatch.setattr("dala.server.MAX_CAPTURE_BYTES", 32)
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        response = client.post("/helper/debug-capture", json={"html": "x" * 100})
    assert response.status_code == 413


def test_debug_capture_accepts_source_larger_than_previous_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("DALA_DEBUG_DIR", str(tmp_path))
    html = '<article>' + 'x' * (26 * 1024 * 1024) + '</article>'
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        response = client.post("/helper/debug-capture", json={"html": html})
    assert response.status_code == 200
    assert Path(response.json()['files']['html']).read_text() == html


def test_debug_capture_accepts_gzip(tmp_path, monkeypatch):
    monkeypatch.setenv("DALA_DEBUG_DIR", str(tmp_path))
    payload = {"html": "<article>Compressed source</article>", "images": []}
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        response = client.post("/helper/debug-capture", content=gzip.compress(json.dumps(payload).encode()),
                               headers={"Content-Encoding": "gzip"})
    assert response.status_code == 200
    assert Path(response.json()['files']['html']).read_text() == payload['html']


def test_debug_capture_limits_decompressed_size(monkeypatch):
    monkeypatch.setattr("dala.server.MAX_CAPTURE_BYTES", 32)
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        response = client.post("/helper/debug-capture", content=gzip.compress(b'{"html":"' + b'x' * 1000 + b'"}'),
                               headers={"Content-Encoding": "gzip"})
    assert response.status_code == 413


@pytest.mark.parametrize("data", [b"not gzip", gzip.compress(b'{"html":"source"}')[:-5]])
def test_debug_capture_rejects_invalid_gzip(data):
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        response = client.post("/helper/debug-capture", content=data, headers={"Content-Encoding": "gzip"})
    assert response.status_code == 400
