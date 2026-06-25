import zipfile
from io import BytesIO

import pytest
from bs4 import BeautifulSoup
from ebooklib import epub
from PIL import Image as PillowImage

from dala.core.extractor import ArticleExtractor
from dala.core.image_processor import ImageProcessor
from dala.core.writer import (
    EpubWriter,
    OutputWriteError,
    PdfWriter,
    default_output_filename,
    ensure_output_extension,
    output_format_info,
    prepare_book_for_output,
)
from dala.models import BookData, Chapter, ConversionOptions, ImageAsset
from tests.helpers import make_book, make_chapter, make_forum_book


def test_output_format_helpers():
    assert output_format_info("pdf").media_type == "application/pdf"
    assert default_output_filename("My Book", "pdf") == "My_Book.pdf"
    assert ensure_output_extension("out", "pdf") == "out.pdf"
    assert ensure_output_extension("out.pdf", "pdf") == "out.pdf"


def test_article_html_wrapper_escapes_title_and_language():
    html = ArticleExtractor.build_article_html(
        'A&B <Test> "Title"',
        "<p>Body</p>",
        meta_html='<div class="post-meta"><p>Meta</p></div>',
        lang='en"bad',
        include_hr=True,
    )

    soup = BeautifulSoup(html, "html.parser")

    assert soup.html["lang"] == 'en"bad'
    assert soup.title.get_text() == 'A&B <Test> "Title"'
    assert soup.body.find("h1").get_text() == 'A&B <Test> "Title"'
    assert soup.body.find("hr") is not None
    assert "Body" in soup.get_text(" ", strip=True)


def test_pdf_html_uses_temp_file_image_refs(tmp_path):
    book = BookData(
        title="PDF Test",
        author="Author",
        uid="urn:test",
        language="en",
        description="",
        source_url="https://example.com",
        chapters=[
            Chapter(
                title="Chapter",
                filename="chapter.xhtml",
                content_html='<p>Hello</p><img src="images/photo.webp" alt="Photo">',
                uid="chapter",
            )
        ],
        images=[
            ImageAsset(
                uid="img",
                filename="images/photo.webp",
                media_type="image/webp",
                content=b"image-bytes",
                original_url="https://example.com/photo.webp",
            )
        ],
    )

    asset_refs = PdfWriter._asset_file_uris(book, tmp_path)
    html = PdfWriter.build_html(book, ConversionOptions(output_format="pdf", pdf_preset="ereader"), asset_refs=asset_refs)

    assert "PDF Test" in html
    assert "file://" in html
    assert (tmp_path / "photo.webp").read_bytes() == b"image-bytes"
    assert 'src="images/photo.webp"' not in html
    assert "data:image/webp;base64," not in html


def test_pdf_asset_refs_can_write_pdf_friendly_jpeg(tmp_path):
    raw = BytesIO()
    PillowImage.new("RGB", (32, 24), (200, 20, 20)).save(raw, format="PNG")
    book = BookData(
        title="PDF Test",
        author="Author",
        uid="urn:test",
        language="en",
        description="",
        source_url="https://example.com",
        chapters=[
            Chapter(
                title="Chapter",
                filename="chapter.xhtml",
                content_html='<p>Hello</p><img src="images/photo.png" alt="Photo">',
                uid="chapter",
            )
        ],
        images=[
            ImageAsset(
                uid="img",
                filename="images/photo.png",
                media_type="image/png",
                content=raw.getvalue(),
                original_url="https://example.com/photo.png",
            )
        ],
    )

    asset_refs = PdfWriter._asset_file_uris(
        book,
        tmp_path,
        ConversionOptions(output_format="pdf", image_preset="optimized"),
        pdf_assets=True,
    )

    assert asset_refs["images/photo.png"].endswith("/photo.jpg")
    assert (tmp_path / "photo.jpg").read_bytes().startswith(b"\xff\xd8")


def test_pdf_html_adds_toc_for_multiple_chapters():
    book = make_book(
        title="Bundle",
        uid="urn:bundle",
        chapters=[
            make_chapter(title="First", filename="first.xhtml", content_html="<p>One</p>", uid="first"),
            make_chapter(title="Second", filename="second.xhtml", content_html="<p>Two</p>", uid="second"),
        ],
    )

    html = PdfWriter.build_html(book, ConversionOptions(output_format="pdf"))

    assert '<div class="pdf-toc">' in html
    assert 'href="#first"' in html
    assert 'href="#second"' in html


def test_pdf_html_adds_forum_page_links_to_single_chapter_toc():
    book = make_forum_book(page_count=2)

    html = PdfWriter.build_html(book, ConversionOptions(output_format="pdf"))

    assert '<div class="pdf-toc">' in html
    assert 'href="#forum-thread"' in html
    assert 'href="#page_1"' in html
    assert 'href="#page_2"' in html


def test_epub_nav_includes_forum_page_fragment_links(tmp_path):
    book = make_forum_book(
        page_count=2,
        toc_structure=[
            (
                epub.Link("thread.xhtml", "Thread", "forum_thread"),
                [
                    epub.Link("thread.xhtml#page_1", "Page 1", "forum_page_1"),
                    epub.Link("thread.xhtml#page_2", "Page 2", "forum_page_2"),
                ],
            )
        ],
    )
    output = tmp_path / "forum.epub"

    EpubWriter.write(book, str(output))

    with zipfile.ZipFile(output) as epub_file:
        nav = epub_file.read("EPUB/nav.xhtml").decode("utf-8")
        ncx = epub_file.read("EPUB/toc.ncx").decode("utf-8")

    assert "thread.xhtml#page_1" in nav
    assert "thread.xhtml#page_2" in nav
    assert "thread.xhtml#page_1" in ncx
    assert "thread.xhtml#page_2" in ncx


def test_pdf_single_chapter_dedupes_document_and_chapter_titles():
    title = "Article Title"
    book = BookData(
        title=title,
        author="Author",
        uid="urn:single-pdf",
        language="en",
        description="",
        source_url="",
        chapters=[
            Chapter(
                title=title,
                filename="chapter.xhtml",
                content_html=f"<html><body><h1>{title}</h1><hr/><p>Body</p></body></html>",
                uid="chapter",
            )
        ],
    )

    html = PdfWriter.build_html(book, ConversionOptions(output_format="pdf"))

    assert '<h1 class="title">Article Title</h1>' not in html
    assert html.count("<h1>Article Title</h1>") == 1
    assert "<hr/>" not in html
    assert "<p>Body</p>" in html


def test_pdf_bundle_keeps_document_header_and_removes_internal_chapter_h1():
    book = BookData(
        title="Bundle",
        author="Author",
        uid="urn:bundle-pdf",
        language="en",
        description="",
        source_url="",
        chapters=[
            Chapter(title="First", filename="first.xhtml", content_html="<h1>First</h1><p>One</p>", uid="first"),
            Chapter(title="Second", filename="second.xhtml", content_html="<h1>Second</h1><p>Two</p>", uid="second"),
        ],
    )

    html = PdfWriter.build_html(book, ConversionOptions(output_format="pdf"))

    assert '<h1 class="title">Bundle</h1>' in html
    assert html.count("<h1>First</h1>") == 1
    assert html.count("<h1>Second</h1>") == 1
    assert "<p>One</p>" in html
    assert "<p>Two</p>" in html


def test_pdf_render_options_footer_only_for_document():
    document_options = PdfWriter.pdf_render_options(
        "/tmp/out.pdf",
        ConversionOptions(output_format="pdf", pdf_preset="document", pdf_page_size="letter"),
    )
    ereader_options = PdfWriter.pdf_render_options(
        "/tmp/out.pdf",
        ConversionOptions(output_format="pdf", pdf_preset="ereader", pdf_page_size="kobo_clara"),
    )

    assert document_options["display_header_footer"] is True
    assert "footer_template" in document_options
    assert "outline" not in document_options
    assert "tagged" not in document_options
    assert "outline" not in ereader_options
    assert "tagged" not in ereader_options
    assert "display_header_footer" not in ereader_options
    assert ereader_options["width"] == "90mm"


def test_pdf_browser_executable_uses_option_before_environment(monkeypatch, tmp_path):
    option_browser = tmp_path / "chromium-option"
    option_browser.write_text("#!/bin/sh\n")
    env_browser = tmp_path / "chromium-env"
    env_browser.write_text("#!/bin/sh\n")
    monkeypatch.setenv("DALA_BROWSER_EXECUTABLE", str(env_browser))

    resolved = PdfWriter.pdf_browser_executable(
        ConversionOptions(output_format="pdf", browser_executable=str(option_browser))
    )

    assert resolved == str(option_browser)


def test_pdf_browser_executable_uses_environment(monkeypatch, tmp_path):
    env_browser = tmp_path / "chromium-env"
    env_browser.write_text("#!/bin/sh\n")
    monkeypatch.setenv("DALA_BROWSER_EXECUTABLE", str(env_browser))

    assert PdfWriter.pdf_browser_executable(ConversionOptions(output_format="pdf")) == str(env_browser)


def test_pdf_browser_executable_rejects_missing_configured_path(monkeypatch):
    monkeypatch.delenv("DALA_BROWSER_EXECUTABLE", raising=False)

    with pytest.raises(OutputWriteError, match="browser executable"):
        PdfWriter.pdf_browser_executable(
            ConversionOptions(output_format="pdf", browser_executable="/missing/chromium")
        )


def test_pdf_native_outline_postprocess_adds_clean_bookmarks(tmp_path):
    from pypdf import PdfReader
    from pypdf import PdfWriter as PypdfWriter

    output_path = tmp_path / "outlined.pdf"
    writer = PypdfWriter()
    for _ in range(4):
        writer.add_blank_page(width=612, height=792)
    with output_path.open("wb") as fh:
        writer.write(fh)

    book = BookData(
        title="Bundle",
        author="Author",
        uid="urn:bundle",
        language="en",
        description="",
        source_url="",
        chapters=[
            Chapter(title="First", filename="first.xhtml", content_html="<p>One</p>", uid="first"),
            Chapter(title="Second", filename="second.xhtml", content_html="<p>Two</p>", uid="second"),
        ],
    )

    PdfWriter.add_native_outline(str(output_path), book, include_contents=True)

    reader = PdfReader(str(output_path))
    outlines = reader.outline
    assert [item.title for item in outlines] == ["Bundle", "First", "Second"]
    assert [reader.get_page_number(item.page) for item in outlines] == [0, 2, 3]


def test_pdf_native_outline_omits_redundant_single_document_title():
    book = BookData(
        title="Article",
        author="Author",
        uid="urn:article",
        language="en",
        description="",
        source_url="",
        chapters=[
            Chapter(title="Article", filename="article.xhtml", content_html="<p>One</p>", uid="article"),
        ],
    )

    assert PdfWriter._chapter_outline_entries(book, include_contents=False) == [("Article", "article", 0)]


def test_pdf_native_outline_omits_redundant_document_title_with_comments():
    book = BookData(
        title="Article",
        author="Author",
        uid="urn:article",
        language="en",
        description="",
        source_url="",
        chapters=[
            Chapter(title="Article", filename="article.xhtml", content_html="<p>One</p>", uid="article"),
            Chapter(title="Comments", filename="comments.xhtml", content_html="<p>Two</p>", uid="comments"),
        ],
    )

    assert PdfWriter._chapter_outline_entries(book, include_contents=True) == [
        ("Article", "article", 2),
        ("Comments", "comments", 3),
    ]


def test_pdf_native_outline_entries_include_forum_pages():
    book = make_forum_book(page_count=2)

    assert PdfWriter._chapter_outline_entries(book, include_contents=False) == [
        ("Forum Thread", "forum-thread", 0),
        ("Page 1", "page_1", 0),
        ("Page 2", "page_2", 0),
    ]


def test_epub_css_styles_metadata_and_captions(tmp_path):
    book = BookData(
        title="Styled",
        author="Author",
        uid="urn:styled",
        language="en",
        description="",
        source_url="",
        chapters=[Chapter(title="Chapter", filename="chapter.xhtml", content_html="<p>Body</p>", uid="chapter")],
    )
    output = tmp_path / "styled.epub"

    EpubWriter.write(book, str(output))

    with zipfile.ZipFile(output) as epub_file:
        css = epub_file.read("EPUB/style/default.css").decode("utf-8")

    assert "font-family: Georgia, serif" in css
    assert "line-height: 1.46" in css
    assert ".image-caption { display: block;" in css
    assert ".image-alt" in css
    assert ".dala-caption-translation-pair .image-caption" in css
    assert ".post-meta { border-top:" in css
    assert "blockquote" in css
    assert "th, td { border: 1px solid" in css
    assert 'td[data-align="right"]' in css
    assert "figure table { text-align: left;" in css


def test_pdf_html_styles_metadata_and_captions():
    book = BookData(
        title="Styled PDF",
        author="Author",
        uid="urn:styled-pdf",
        language="en",
        description="",
        source_url="",
        chapters=[Chapter(title="Chapter", filename="chapter.xhtml", content_html="<p>Body</p>", uid="chapter")],
    )

    html = PdfWriter.build_html(book, ConversionOptions(output_format="pdf"))

    assert ".image-caption { display: block;" in html
    assert ".image-alt" in html
    assert ".dala-caption-translation-pair .image-caption" in html
    assert ".post-meta { border-top:" in html
    assert "th, td { border: 1px solid" in html
    assert 'td[data-align="right"]' in html
    assert "figure table { text-align: left;" in html
    assert "line-height: 1.5" in html
    assert "line-height: 1.46" not in html


def test_no_images_cleanup_keeps_caption_and_removes_image_shells():
    soup = BeautifulSoup(
        """
        <body>
            <figure>
                <img src="https://example.com/photo.jpg" alt="wp-17552007607367818172295140123330"/>
                <figcaption>Merci Sofie !</figcaption>
            </figure>
        </body>
        """,
        "html.parser",
    )

    ImageProcessor.remove_images_for_text_output(soup)

    assert not soup.find("img")
    assert not soup.find("figure")
    assert not soup.find("figcaption")
    assert soup.select_one(".image-caption").get_text(strip=True) == "Merci Sofie !"
    assert not soup.select_one(".image-alt")


def test_no_images_cleanup_keeps_useful_alt_without_caption():
    soup = BeautifulSoup(
        '<body><p>Before</p><img src="photo.jpg" alt="Two cyclists crossing a bridge outside Paris"/><p>After</p></body>',
        "html.parser",
    )

    ImageProcessor.remove_images_for_text_output(soup)

    assert not soup.find("img")
    assert soup.select_one(".image-alt").get_text(strip=True) == "[Image: Two cyclists crossing a bridge outside Paris]"


def test_no_images_cleanup_dedupes_caption_and_alt():
    soup = BeautifulSoup(
        """
        <body>
            <figure>
                <img src="photo.jpg" alt="Forest path outside Paris"/>
                <figcaption>Forest path outside Paris</figcaption>
            </figure>
        </body>
        """,
        "html.parser",
    )

    ImageProcessor.remove_images_for_text_output(soup)

    assert soup.select_one(".image-caption").get_text(strip=True) == "Forest path outside Paris"
    assert not soup.select_one(".image-alt")


def test_no_images_cleanup_suppresses_low_value_alt_and_empty_wrapper():
    soup = BeautifulSoup(
        '<body><div class="img-block"><img src="https://example.com/wp-123.jpg" alt="wp-123.jpg"/></div><p>Body</p></body>',
        "html.parser",
    )

    ImageProcessor.remove_images_for_text_output(soup)

    assert not soup.find("img")
    assert not soup.select_one(".img-block")
    assert not soup.select_one(".image-alt")
    assert "Body" in soup.get_text(" ", strip=True)


def test_prepare_book_for_no_images_removes_images_and_assets():
    book = BookData(
        title="No Images",
        author="Author",
        uid="urn:no-images",
        language="en",
        description="",
        source_url="",
        chapters=[
            Chapter(
                title="Chapter",
                filename="chapter.xhtml",
                content_html="""
                <html><body>
                    <figure>
                        <img src="https://example.com/photo.jpg" alt="Photo"/>
                        <figcaption>Useful caption</figcaption>
                    </figure>
                </body></html>
                """,
                uid="chapter",
            )
        ],
        images=[
            ImageAsset(
                uid="img",
                filename="images/photo.jpg",
                media_type="image/jpeg",
                content=b"image",
                original_url="https://example.com/photo.jpg",
            )
        ],
    )

    cleaned = prepare_book_for_output(book, ConversionOptions(no_images=True))

    assert cleaned is not book
    assert cleaned.images == []
    assert book.images
    assert "<img" not in cleaned.chapters[0].content_html
    assert "<figure" not in cleaned.chapters[0].content_html
    assert "Useful caption" in cleaned.chapters[0].content_html


def test_prepare_book_normalizes_caption_markup_and_removes_share_boilerplate():
    book = BookData(
        title="Cleanup",
        author="Author",
        uid="urn:cleanup",
        language="en",
        description="",
        source_url="",
        chapters=[
            Chapter(
                title="Chapter",
                filename="chapter.xhtml",
                content_html="""
                <html><body>
                    <p>We share a meal before the ride.</p>
                    <div class="sharedaddy">Share on Facebook Share Share on Twitter</div>
                    <figure>
                        <img src="images/photo.jpg" alt="Photo"/>
                        <figcaption style="font-size: 16px">A quiet forest path</figcaption>
                    </figure>
                    <p class="wp-caption-text">Duplicate standalone caption</p>
                    <p>Share on Facebook Share Share on Twitter</p>
                </body></html>
                """,
                uid="chapter",
            )
        ],
    )

    cleaned = prepare_book_for_output(book, ConversionOptions(no_images=False))
    soup = BeautifulSoup(cleaned.chapters[0].content_html, "html.parser")

    assert "We share a meal before the ride." in soup.get_text(" ", strip=True)
    assert "Share on Facebook" not in soup.get_text(" ", strip=True)
    assert not soup.select(".sharedaddy")
    assert not soup.find("figcaption")
    captions = [tag.get_text(strip=True) for tag in soup.select(".image-caption")]
    assert "A quiet forest path" in captions
    assert "Duplicate standalone caption" in captions
    assert all(not tag.has_attr("style") for tag in soup.select(".image-caption"))


def test_find_caption_from_wordpress_linked_image_wrapper():
    soup = BeautifulSoup(
        """
        <div class="wp-caption alignnone">
            <a href="photo.jpg"><img src="photo.jpg" alt="_MG_2678"/></a>
            <p class="wp-caption-text">Regimented choices...</p>
        </div>
        """,
        "html.parser",
    )

    caption = ImageProcessor.find_caption(soup.find("img"))

    assert caption == "Regimented choices..."


def test_prepare_book_removes_short_standalone_share_text_but_keeps_sentences():
    book = BookData(
        title="Share Cleanup",
        author="Author",
        uid="urn:share-cleanup",
        language="en",
        description="",
        source_url="",
        chapters=[
            Chapter(
                title="Chapter",
                filename="chapter.xhtml",
                content_html="""
                <html><body>
                    <p>Share on Twitter</p>
                    <p>The authors share their data and methods.</p>
                    <span>Tweet</span>
                </body></html>
                """,
                uid="chapter",
            )
        ],
    )

    cleaned = prepare_book_for_output(book, ConversionOptions())
    text = BeautifulSoup(cleaned.chapters[0].content_html, "html.parser").get_text(" ", strip=True)

    assert "Share on Twitter" not in text
    assert "Tweet" not in text
    assert "The authors share their data and methods." in text


def test_prepare_book_removes_jetpack_share_and_like_flair():
    book = BookData(
        title="Jetpack Share",
        author="Author",
        uid="urn:jetpack-share",
        language="en",
        description="",
        source_url="",
        chapters=[
            Chapter(
                title="Chapter",
                filename="chapter.xhtml",
                content_html="""
                <html><body>
                    <p>Lending can be socially valuable.</p>
                    <div id="jp-post-flair">
                        <div>
                            <div>
                                <h3>Share this:</h3>
                                <div>
                                    <ul>
                                        <li>
                                            <a data-shared="sharing-twitter-1140" href="https://example.com/post/?share=twitter">
                                                <span id="sharing-twitter-1140">Share on X (Opens in new window)</span>
                                                <span>X</span>
                                            </a>
                                        </li>
                                        <li>
                                            <a data-shared="sharing-facebook-1140" href="https://example.com/post/?share=facebook">
                                                <span id="sharing-facebook-1140">Share on Facebook (Opens in new window)</span>
                                                <span>Facebook</span>
                                            </a>
                                        </li>
                                    </ul>
                                </div>
                            </div>
                        </div>
                        <div id="like-post-wrapper-65735519-1140"><span>Like</span></div>
                        <div id="jp-relatedposts"><h3><em>Related</em></h3></div>
                    </div>
                </body></html>
                """,
                uid="chapter",
            )
        ],
    )

    cleaned = prepare_book_for_output(book, ConversionOptions())
    text = BeautifulSoup(cleaned.chapters[0].content_html, "html.parser").get_text(" ", strip=True)

    assert "Lending can be socially valuable." in text
    assert "Share this" not in text
    assert "Share on X" not in text
    assert "Share on Facebook" not in text
    assert "Like" not in text
    assert "Related" not in text


def test_prepare_book_removes_share_urls_navigation_related_and_subscribe_prompts():
    book = BookData(
        title="Cleanup",
        author="Author",
        uid="urn:cleanup",
        language="en",
        description="",
        source_url="",
        chapters=[
            Chapter(
                title="Chapter",
                filename="chapter.xhtml",
                content_html="""
                <html><body>
                    <p>Main article text stays here.</p>
                    <div class="post-navigation"><a rel="prev" href="/old">Previous Post</a></div>
                    <a href="https://twitter.com/intent/tweet?url=https://example.com">Share via X</a>
                    <a href="https://facebook.com/sharer/sharer.php?u=https://example.com">Facebook</a>
                    <a href="mailto:?subject=Article">Email</a>
                    <div class="related-posts"><h2>Related Posts</h2><p>Another story</p></div>
                    <div data-print-layout="hide">
                        <span>Explore more on these topics</span>
                        <ul>
                            <li><a href="/us-news/texas">Texas</a></li>
                            <li><a href="/tone/news">news</a></li>
                        </ul>
                        <a data-link-name="meta-syndication-article" href="https://syndication.theguardian.com/?url=https%3A%2F%2Fexample.com">Reuse this content</a>
                    </div>
                    <div class="newsletter-signup"><p>Subscribe to our newsletter</p><input value="email@example.com"/></div>
                    <p>This article discusses why newsletters are durable media.</p>
                </body></html>
                """,
                uid="chapter",
            )
        ],
    )

    cleaned = prepare_book_for_output(book, ConversionOptions())
    text = BeautifulSoup(cleaned.chapters[0].content_html, "html.parser").get_text(" ", strip=True)

    assert "Main article text stays here." in text
    assert "Previous Post" not in text
    assert "Share via X" not in text
    assert "Facebook" not in text
    assert "Related Posts" not in text
    assert "Explore more on these topics" not in text
    assert "Reuse this content" not in text
    assert "Subscribe to our newsletter" not in text
    assert "This article discusses why newsletters are durable media." in text


def test_prepare_book_removes_generic_related_link_clusters():
    book = BookData(
        title="Cleanup",
        author="Author",
        uid="urn:cleanup-links",
        language="en",
        description="",
        source_url="",
        chapters=[
            Chapter(
                title="Chapter",
                filename="chapter.xhtml",
                content_html="""
                <html><body>
                    <p>Main article text stays here.</p>
                    <section>
                        <h2>More on this story</h2>
                        <ul>
                            <li><a href="/a">Background timeline</a></li>
                            <li><a href="/b">What happens next</a></li>
                            <li><a href="/c">Analysis and reaction</a></li>
                        </ul>
                    </section>
                </body></html>
                """,
                uid="chapter",
            )
        ],
    )

    cleaned = prepare_book_for_output(book, ConversionOptions())
    text = BeautifulSoup(cleaned.chapters[0].content_html, "html.parser").get_text(" ", strip=True)

    assert "Main article text stays here." in text
    assert "More on this story" not in text
    assert "Background timeline" not in text


def test_prepare_book_removes_cbc_and_science_widget_boilerplate():
    book = BookData(
        title="Publisher Widgets",
        author="Author",
        uid="urn:publisher-widgets",
        language="en",
        description="",
        source_url="",
        chapters=[
            Chapter(
                title="Chapter",
                filename="chapter.xhtml",
                content_html="""
                <html><body>
                    <p>Main article text stays here.</p>
                    <section><h2>Popular Now in Montreal</h2><a href="/a">Unrelated CBC story</a><a href="/b">Another story</a></section>
                    <section><h2>Trending Videos</h2><p>Video teaser text</p></section>
                    <div><h2>Discover More from CBC</h2><p>Recommended card</p></div>
                    <div><h2>Related podcast</h2><p>Progress 0:00 Volume AI-generated audio</p></div>
                    <p>Download PDF</p>
                    <p>Advertisement</p>
                    <div data-cy="author-image-img"><img src="images/author.jpg"/></div>
                    <div><strong>WATCH | Tree-planting initiative sees pushback:</strong><div data-cy="player-placeholder-ui-container"><img src="images/video.jpg"/><p>Video teaser</p></div></div>
                    <p>The article continues after the embedded material.</p>
                </body></html>
                """,
                uid="chapter",
            )
        ],
    )

    cleaned = prepare_book_for_output(book, ConversionOptions())
    text = BeautifulSoup(cleaned.chapters[0].content_html, "html.parser").get_text(" ", strip=True)

    assert "Main article text stays here." in text
    assert "The article continues after the embedded material." in text
    assert "Popular Now" not in text
    assert "Trending Videos" not in text
    assert "Discover More from CBC" not in text
    assert "Related podcast" not in text
    assert "Download PDF" not in text
    assert "Advertisement" not in text
    assert "WATCH" not in text
    assert "Video teaser" not in text
    assert not BeautifulSoup(cleaned.chapters[0].content_html, "html.parser").find("img", src="images/author.jpg")


def test_prepare_book_removes_recirculation_byline_images_and_empty_inline_tags():
    book = BookData(
        title="Recirculation",
        author="Author",
        uid="urn:recirc",
        language="en",
        description="",
        source_url="",
        chapters=[
            Chapter(
                title="Chapter",
                filename="chapter.xhtml",
                content_html="""
                <html><body>
                    <p>Despite the <b></b> new guidance, the memo remains newsworthy.</p>
                    <div data-testid="byline-container"><img src="images/author-headshot.jpg"/><span>By Reporter</span></div>
                    <p>Main article text stays here.</p>
                    <hr/>
                    <div data-fy-request-id="abc">
                      <span>Most read</span>
                      <ul>
                        <li><a href="/one">Unrelated recommendation</a></li>
                        <li><a href="/two">Another recommendation</a></li>
                      </ul>
                      <img alt="headline" src="images/teaser.jpg"/>
                    </div>
                </body></html>
                """,
                uid="chapter",
            )
        ],
    )

    cleaned = prepare_book_for_output(book, ConversionOptions())
    soup = BeautifulSoup(cleaned.chapters[0].content_html, "html.parser")
    text = soup.get_text(" ", strip=True)

    assert "Main article text stays here." in text
    assert "Most read" not in text
    assert "Unrelated recommendation" not in text
    assert not soup.find("img", src="images/author-headshot.jpg")
    assert not soup.find("img", src="images/teaser.jpg")
    assert not soup.find("b")


def test_prepare_book_keeps_article_lists_with_contextual_links():
    book = BookData(
        title="Linked List",
        author="Author",
        uid="urn:linked-list",
        language="en",
        description="",
        source_url="",
        chapters=[
            Chapter(
                title="Chapter",
                filename="chapter.xhtml",
                content_html="""
                <html><body>
                    <p>Main article text stays here.</p>
                    <ul>
                        <li><a href="/a">Texas</a> officials described the prosecution as a deterrent, while defense lawyers said the case criminalized protest.</li>
                        <li><a href="/b">Chicago</a> prosecutors dropped a similar case after misconduct questions.</li>
                        <li><a href="/c">Spokane</a> jurors reached a different result after a separate trial.</li>
                    </ul>
                </body></html>
                """,
                uid="chapter",
            )
        ],
    )

    cleaned = prepare_book_for_output(book, ConversionOptions())
    text = BeautifulSoup(cleaned.chapters[0].content_html, "html.parser").get_text(" ", strip=True)

    assert "Texas officials described the prosecution" in text
    assert "Chicago prosecutors dropped" in text
    assert "Spokane jurors reached" in text


def test_prepare_book_keeps_article_wrappers_that_mention_newsletters():
    long_body = " ".join(["newsletter analysis remains part of the article"] * 30)
    book = BookData(
        title="Newsletter Article",
        author="Author",
        uid="urn:newsletter-article",
        language="en",
        description="",
        source_url="",
        chapters=[
            Chapter(
                title="Chapter",
                filename="chapter.xhtml",
                content_html=f'<html><body><div class="newsletter-article"><p>{long_body}</p></div></body></html>',
                uid="chapter",
            )
        ],
    )

    cleaned = prepare_book_for_output(book, ConversionOptions())
    text = BeautifulSoup(cleaned.chapters[0].content_html, "html.parser").get_text(" ", strip=True)

    assert "newsletter analysis remains part of the article" in text


def test_prepare_book_removes_localized_short_bylines():
    book = BookData(
        title="Localized",
        author="Author",
        uid="urn:localized",
        language="en",
        description="",
        source_url="",
        chapters=[
            Chapter(
                title="Chapter",
                filename="chapter.xhtml",
                content_html="""
                <html><body>
                    <p>Publié le 15 août 2025 par velovefamily</p>
                    <p>Publicado el 15 de agosto de 2025 por Autor</p>
                    <p>Veröffentlicht am 15. August 2025 von Autor</p>
                    <p>The article body remains.</p>
                </body></html>
                """,
                uid="chapter",
            )
        ],
    )

    cleaned = prepare_book_for_output(book, ConversionOptions())
    text = BeautifulSoup(cleaned.chapters[0].content_html, "html.parser").get_text(" ", strip=True)

    assert "Publié le" not in text
    assert "Publicado el" not in text
    assert "Veröffentlicht am" not in text
    assert "The article body remains." in text


def test_prepare_book_removes_duplicate_inner_article_header():
    title = "“Lending is Meritorious and Should be Praised”: How The Fifth Lateran Council Unlocked Financial Theory"
    book = BookData(
        title=title,
        author="Sebastiangarren",
        uid="urn:duplicate-title",
        language="en",
        description="",
        source_url="",
        chapters=[
            Chapter(
                title=title,
                filename="chapter.xhtml",
                content_html=f"""
                <html><body>
                    <h1>{title}</h1>
                    <div class="post-meta"><p>Source: Sebastian Garren</p></div>
                    <hr/>
                    <article>
                        <header>
                            <h1>“Lending is Meritorious and Should be Praised”: How The Fifth Lateran Council Unlocked Financial&nbsp;Theory</h1>
                            <div><span>Posted on</span> <time>June 17, 2026</time> <span>by <a>sebastiangarren</a></span></div>
                        </header>
                        <div><p>Advances in medieval economic theory had consequences.</p></div>
                    </article>
                </body></html>
                """,
                uid="chapter",
            )
        ],
    )

    cleaned = prepare_book_for_output(book, ConversionOptions())
    soup = BeautifulSoup(cleaned.chapters[0].content_html, "html.parser")
    text = soup.get_text(" ", strip=True)

    assert len(soup.find_all("h1")) == 1
    assert not soup.find("article").find("header")
    assert "Source: Sebastian Garren" in text
    assert "Posted on" not in text
    assert "June 17, 2026" not in text
    assert "Advances in medieval economic theory" in text


def test_prepare_book_removes_duplicate_inner_title_with_site_suffix():
    book = BookData(
        title="Quebec town recognizes trees as living beings with rights | CBC News",
        author="CBC",
        uid="urn:cbc-title",
        language="en",
        description="",
        source_url="",
        chapters=[
            Chapter(
                title="Quebec town recognizes trees as living beings with rights | CBC News",
                filename="chapter.xhtml",
                content_html="""
                <html><body>
                    <h1>Quebec town recognizes trees as living beings with rights | CBC News</h1>
                    <article>
                        <h1>Quebec town recognizes trees as living beings with rights</h1>
                        <p>The article body remains.</p>
                    </article>
                </body></html>
                """,
                uid="chapter",
            )
        ],
    )

    cleaned = prepare_book_for_output(book, ConversionOptions())
    soup = BeautifulSoup(cleaned.chapters[0].content_html, "html.parser")

    assert len(soup.find_all("h1")) == 1
    assert soup.find("h1").get_text(strip=True) == "Quebec town recognizes trees as living beings with rights | CBC News"
    assert "The article body remains." in soup.get_text(" ", strip=True)
