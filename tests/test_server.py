import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock
import server # Import to make sure app is loaded
from server import app
from web_to_epub import BookData, Chapter

client = TestClient(app)

def test_ping():
    response = client.get("/ping")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

@patch("server.core.process_urls", new_callable=AsyncMock)
@patch("server.core.EpubWriter.write")
def test_convert_endpoint(mock_write, mock_process):
    # Mock the core processing to return a dummy book
    dummy_book = BookData(
        title="Test Book",
        author="Test Author",
        uid="urn:test",
        language="en",
        description="desc",
        source_url="http://example.com",
        chapters=[Chapter(title="C1", filename="c1.xhtml", content_html="<p>Hi</p>", uid="c1")]
    )
    mock_process.return_value = [dummy_book]

    payload = {
        "sources": [
            {
                "url": "http://example.com/article",
                "html": "<html>...</html>",
                "is_forum": False
            }
        ],
        "no_images": True,
        "bundle_title": "My Bundle"
    }
    
    response = client.post("/convert", json=payload)
    
    # Debug info if failed
    if response.status_code != 200:
        print(response.json())

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/epub+zip"
    
    # Verify core.process_urls was called correctly
    assert mock_process.called
    args, kwargs = mock_process.call_args
    
    # args[0] is sources list
    sources = args[0]
    assert len(sources) == 1
    assert sources[0].url == "http://example.com/article"
    assert sources[0].html == "<html>...</html>"
    
    # args[1] is options
    options = args[1]
    assert options.no_images is True
    
    # Verify bundle creation logic (if applicable) or just single book return
    # The server logic handles single vs bundle.
    assert "filename" in response.headers["content-disposition"]
