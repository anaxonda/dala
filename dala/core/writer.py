import html
import io
import logging
import os
import re
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup
from ebooklib import epub
from pygments.formatters import HtmlFormatter
from ..models import log, BookData, ConversionOptions, sanitize_filename
from .browser import resolve_browser_executable
from .image_processor import ImageProcessor
from .translation import TranslationProcessor


class OutputWriteError(RuntimeError):
    """Raised when a requested output format cannot be written."""


@dataclass(frozen=True)
class OutputFormatInfo:
    extension: str
    media_type: str
    label: str


@dataclass(frozen=True)
class TocEntry:
    title: str
    anchor: Optional[str]
    fallback_page: int
    children: tuple["TocEntry", ...] = ()


OUTPUT_FORMATS = {
    "epub": OutputFormatInfo(extension=".epub", media_type="application/epub+zip", label="EPUB"),
    "pdf": OutputFormatInfo(extension=".pdf", media_type="application/pdf", label="PDF"),
}


def normalize_output_format(value: Optional[str]) -> str:
    output_format = (value or "epub").strip().lower()
    if output_format not in OUTPUT_FORMATS:
        raise OutputWriteError(f"Unsupported output format: {value}")
    return output_format


def output_format_info(value: Optional[str]) -> OutputFormatInfo:
    return OUTPUT_FORMATS[normalize_output_format(value)]


def ensure_output_extension(path: str, output_format: Optional[str]) -> str:
    info = output_format_info(output_format)
    if path.lower().endswith(info.extension):
        return path
    return f"{path}{info.extension}"


def default_output_filename(title: str, output_format: Optional[str]) -> str:
    return f"{sanitize_filename(title)}{output_format_info(output_format).extension}"


SOCIAL_CLEANUP_SELECTORS = [
    ".sharedaddy",
    ".sd-sharing-enabled",
    ".sd-social",
    ".sd-block",
    ".share",
    ".sharing",
    ".social-share",
    ".share-buttons",
    ".post-share",
    ".entry-share",
    ".addtoany_share_save_container",
    ".heateor_sss_sharing_container",
    "#jp-post-flair",
    "#jp-relatedposts",
    "[id^='jp-relatedposts']",
    "[id^='like-post-wrapper']",
    "[id^='sharing-']",
    "[data-shared]",
    "[class*='sharethis']",
    "[class*='sharing']",
    "[class*='social-share']",
    "[id*='sharethis']",
    "a[href*='?share=']",
    "a[href*='&share=']",
    "a[href*='twitter.com/intent']",
    "a[href*='x.com/intent']",
    "a[href*='facebook.com/sharer']",
    "a[href*='linkedin.com/shareArticle']",
    "a[href*='pinterest.com/pin/create']",
    "a[href*='reddit.com/submit']",
    "a[href*='tumblr.com/share']",
    "a[href*='getpocket.com/save']",
    "a[href^='mailto:?']",
]

NAV_CLEANUP_SELECTORS = [
    "#nav-above",
    "#nav-below",
    ".nav-previous",
    ".nav-next",
    ".nav-links",
    ".navigation",
    ".post-navigation",
    ".posts-navigation",
    ".comment-navigation",
    ".pagination",
    "a[rel='prev']",
    "a[rel='next']",
]

RELATED_CLEANUP_SELECTORS = [
    "#jp-relatedposts",
    "[id^='jp-relatedposts']",
    ".jp-relatedposts",
    ".related-posts",
    ".relatedposts",
    ".yarpp-related",
    ".crp_related",
    ".outbrain",
    "[data-fy-request-id]",
    "[data-fy-surface]",
    "[data-recommendations]",
]

SUBSCRIBE_CLEANUP_SELECTORS = [
    ".subscribe-promo",
    ".subscription-widget",
    ".newsletter-signup",
    ".newsletter-form",
    ".email-signup",
    ".signup-form",
    ".substack-subscribe",
    ".subscribe-widget",
    "[class*='subscribe-prompt']",
    "[class*='newsletter-signup']",
    "[class*='newsletter-form']",
]

UTILITY_FOOTER_HEADINGS = (
    "explore more",
    "more on this",
    "more from",
    "related",
    "related stories",
    "related articles",
    "read more",
    "recommended",
    "recommended stories",
    "topics",
    "tagged",
)
PUBLISHER_WIDGET_HEADING_RE = re.compile(
    r"^(?:"
    r"social sharing|popular now(?:\s+in .*)?|trending videos?|discover more from .+|"
    r"most read|most popular|latest stories?|top stories?|"
    r"recommended(?: for you)?|related stories?|related articles?|related podcast|"
    r"table of contents|download pdf|advertisement|listen to this article|watch\s*\|.*"
    r")$",
    re.IGNORECASE,
)
PUBLISHER_WIDGET_SHORT_TEXT_RE = re.compile(
    r"^(?:"
    r"progress|volume|mute|unmute|play|pause|0:00|"
    r"ai-generated audio|report an issue|give feedback"
    r")$",
    re.IGNORECASE,
)
CAPTION_CLASSES = {"caption", "wp-caption-text", "gallery-caption", "image-caption"}
CAPTION_SELECTOR = "figcaption, .caption, .wp-caption-text, .gallery-caption, .image-caption"
SHARE_TEXT_RE = re.compile(
    r"^(?:"
    r"share(?:\s+this)?"
    r"|share\s+this:?"
    r"|share\s+on\s+(?:facebook|twitter|x|linkedin|reddit|pinterest|tumblr|mastodon)"
    r"|tweet"
    r"|like\s+this:?"
    r"|reblog"
    r"|share\s+via\s+email"
    r"|pocket"
    r"|more"
    r"|loading(?:\.\.\.)?"
    r"|print"
    r"|email"
    r")(?:\s+(?:share|tweet|print|email|loading(?:\.\.\.)?|reblog|pocket|more|"
    r"share\s+on\s+(?:facebook|twitter|x|linkedin|reddit|pinterest|tumblr|mastodon)))*$",
    re.IGNORECASE,
)


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _normalized_title(value: str) -> str:
    text = _compact_text(value).casefold()
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[“”\"'‘’]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _titles_equivalent(candidate: str, chapter_title: str) -> bool:
    candidate_norm = _normalized_title(candidate)
    title_norm = _normalized_title(chapter_title)
    if not candidate_norm or not title_norm:
        return False
    if candidate_norm == title_norm:
        return True
    if len(candidate_norm) < 20 or len(title_norm) < 20:
        return False
    separators = (" | ", " - ", " – ", " — ", ": ")
    return any(
        title_norm.startswith(f"{candidate_norm}{sep}")
        or candidate_norm.startswith(f"{title_norm}{sep}")
        for sep in separators
    )


def _remove_social_boilerplate(soup: BeautifulSoup) -> None:
    for selector in SOCIAL_CLEANUP_SELECTORS + RELATED_CLEANUP_SELECTORS:
        for tag in soup.select(selector):
            tag.decompose()

    for tag in list(soup.find_all(["p", "div", "span", "li", "a", "button"])):
        if tag.find(["img", "figure", "picture", "table", "blockquote", "pre", "code"]):
            continue
        text = _compact_text(tag.get_text(" ", strip=True))
        if not text or len(text) > 140:
            continue
        if SHARE_TEXT_RE.match(text):
            tag.decompose()


def _remove_navigation_boilerplate(soup: BeautifulSoup) -> None:
    for selector in NAV_CLEANUP_SELECTORS:
        for tag in soup.select(selector):
            tag.decompose()


def _remove_subscription_boilerplate(soup: BeautifulSoup) -> None:
    for selector in SUBSCRIBE_CLEANUP_SELECTORS:
        for tag in soup.select(selector):
            text = _compact_text(tag.get_text(" ", strip=True)).casefold()
            if not text or len(text) <= 360:
                tag.decompose()
                continue
            if any(token in text for token in ["subscribe", "newsletter", "sign up", "email address"]):
                tag.decompose()


def _remove_topic_footer_boilerplate(soup: BeautifulSoup) -> None:
    for tag in list(soup.select('[data-print-layout="hide"]')):
        text = _compact_text(tag.get_text(" ", strip=True)).casefold()
        if (
            text.startswith("explore more on these topics")
            or "reuse this content" in text
            or "syndication.theguardian.com" in str(tag)
        ):
            tag.decompose()

    for link in list(soup.select('[data-link-name="meta-syndication-article"], a[href*="syndication.theguardian.com"]')):
        container = link.find_parent(["div", "section", "p"]) or link
        text = _compact_text(container.get_text(" ", strip=True)).casefold()
        if text in {"reuse this content", "reuse this content."}:
            container.decompose()
        else:
            link.decompose()

    for marker in soup.find_all(string=lambda value: isinstance(value, str) and "Explore more on these topics" in value):
        container = marker.find_parent(["div", "section", "aside"])
        if container:
            container.decompose()


def _remove_boilerplate_block(tag, max_chars: int = 2400) -> None:
    candidate = tag
    for ancestor in tag.parents:
        if not getattr(ancestor, "name", None) or ancestor.name in {"body", "html", "article", "main"}:
            break
        text = _compact_text(ancestor.get_text(" ", strip=True))
        if text and len(text) <= max_chars:
            candidate = ancestor
        else:
            break
    candidate.decompose()


def _remove_publisher_widget_boilerplate(soup: BeautifulSoup) -> None:
    for selector in [
        "[data-cy*='author-image' i]",
        "[data-testid*='author' i] img",
        "[data-testid*='byline' i] img",
        "[data-qa*='author' i] img",
        "[data-qa*='byline' i] img",
        "[class*='author-avatar' i]",
        "[class*='byline' i] img",
        "[data-cy*='player' i]",
        "[data-testid*='player' i]",
        "[data-testid*='video' i]",
        "[class*='video-player' i]",
    ]:
        try:
            for tag in list(soup.select(selector)):
                text = _compact_text(tag.get_text(" ", strip=True))
                if not text or len(text) <= 2400:
                    tag.decompose()
        except Exception:
            continue

    for tag in list(soup.find_all(["h1", "h2", "h3", "h4", "h5", "p", "div", "span", "button", "a", "strong", "em"])):
        if tag.name is None:
            continue
        if tag.find(["img", "figure", "picture", "table", "blockquote", "pre", "code"]):
            continue
        text = _compact_text(tag.get_text(" ", strip=True))
        if not text or len(text) > 120:
            continue
        if PUBLISHER_WIDGET_HEADING_RE.match(text):
            _remove_boilerplate_block(tag)
        elif PUBLISHER_WIDGET_SHORT_TEXT_RE.match(text):
            tag.decompose()

    for marker in list(soup.find_all(string=lambda value: isinstance(value, str) and "This audio was generated" in value)):
        container = marker.find_parent(["div", "section", "aside", "p"])
        if container:
            _remove_boilerplate_block(container)


def _is_low_value_link_cluster(tag) -> bool:
    if tag.name not in {"div", "section", "aside", "ul", "ol"}:
        return False
    if tag.find(["article", "table", "blockquote", "pre", "code"]):
        return False
    text = _compact_text(tag.get_text(" ", strip=True))
    if not text or len(text) > 900:
        return False
    links = tag.find_all("a")
    if len(links) < 3:
        return False
    link_text = _compact_text(" ".join(link.get_text(" ", strip=True) for link in links))
    if not link_text:
        return False
    link_ratio = len(link_text) / max(1, len(text))
    heading_text = ""
    heading = tag.find(["h2", "h3", "h4"])
    if heading:
        heading_text = _compact_text(heading.get_text(" ", strip=True)).casefold()
    else:
        first_text = tag.find(string=lambda value: isinstance(value, str) and _compact_text(value))
        if first_text:
            heading_text = _compact_text(str(first_text)).casefold()
    starts_like_footer = any(heading_text.startswith(prefix) for prefix in UTILITY_FOOTER_HEADINGS)
    class_id = " ".join([tag.get("id", ""), " ".join(tag.get("class") or [])]).casefold()
    named_like_footer = any(token in class_id for token in ["related", "recommend", "topics", "tags", "more"])
    return link_ratio >= 0.65 and (starts_like_footer or named_like_footer)


def _remove_generic_link_cluster_boilerplate(soup: BeautifulSoup) -> None:
    for tag in list(soup.find_all(["div", "section", "aside", "ul", "ol"])):
        if _is_low_value_link_cluster(tag):
            tag.decompose()


def _remove_duplicate_article_title_and_meta(soup: BeautifulSoup, chapter_title: Optional[str]) -> None:
    normalized = _normalized_title(chapter_title or "")
    if not normalized:
        return

    body = soup.body or soup
    wrapper_title = body.find("h1")
    for tag in list(body.select("article header, .entry-header, .post-header")):
        title_tag = tag.find(["h1", "h2", "h3"])
        title_text = title_tag.get_text(" ", strip=True) if title_tag else tag.get_text(" ", strip=True)
        title_norm = _normalized_title(title_text)
        if _titles_equivalent(title_text, chapter_title or "") or (normalized and title_norm.startswith(normalized)):
            if tag.find(["img", "figure", "picture"]):
                if title_tag:
                    title_tag.decompose()
                for meta_tag in list(tag.select(".entry-meta, .post-meta, .posted-on, .byline, .post-date, .post-author")):
                    meta_tag.decompose()
                if not tag.get_text(strip=True) and not tag.find(["img", "figure", "picture"]):
                    tag.decompose()
            else:
                tag.decompose()

    for tag in list(body.select("h1, h2, h3, .entry-title, .post-title")):
        if wrapper_title is not None and tag is wrapper_title:
            continue
        if _titles_equivalent(tag.get_text(" ", strip=True), chapter_title or ""):
            tag.decompose()

    for tag in list(body.select(".entry-meta, .post-meta, .posted-on, .byline, .post-date, .post-author")):
        if tag.parent is body and "post-meta" in (tag.get("class") or []):
            continue
        tag.decompose()

    for tag in list(body.find_all(["p", "div", "span"], recursive=True)):
        if tag.find(["img", "figure", "picture", "table", "blockquote", "pre", "code"]):
            continue
        text = _compact_text(tag.get_text(" ", strip=True))
        lowered = text.casefold()
        if len(text) <= 180 and (
            lowered.startswith("posted on ")
            or lowered.startswith("published on ")
            or lowered.startswith("posted by ")
            or lowered.startswith("publié ")
            or lowered.startswith("publie ")
            or lowered.startswith("publicado ")
            or lowered.startswith("veröffentlicht ")
            or re.match(r"^posted on .+ by \S+", lowered)
            or re.match(r"^published on .+ by \S+", lowered)
            or re.match(r"^publié .+", lowered)
            or re.match(r"^publie .+", lowered)
        ):
            tag.decompose()


def _normalize_caption_markup(soup: BeautifulSoup) -> None:
    for tag in list(soup.select(CAPTION_SELECTOR)):
        if tag.name is None:
            continue
        if tag.get("data-dala-upper-caption") == "1":
            if tag.name == "figcaption":
                tag.name = "div"
            for attr in ["data-dala-upper-caption", "style", "align", "width", "height"]:
                if tag.has_attr(attr):
                    del tag[attr]
            classes = [cls for cls in (tag.get("class") or []) if cls not in CAPTION_CLASSES]
            if classes:
                tag["class"] = classes
            elif tag.has_attr("class"):
                del tag["class"]
            continue
        text = _compact_text(tag.get_text(" ", strip=True))
        if not text:
            tag.decompose()
            continue
        if tag.name == "figcaption":
            tag.name = "p"
        classes = [cls for cls in (tag.get("class") or []) if cls not in CAPTION_CLASSES]
        tag["class"] = ["image-caption", *classes]
        tag.string = text
        for attr in ["style", "align", "width", "height"]:
            if tag.has_attr(attr):
                del tag[attr]

    for alt in soup.select(".image-alt"):
        classes = [cls for cls in (alt.get("class") or []) if cls != "image-alt"]
        alt["class"] = ["image-alt", *classes]
        for attr in ["style", "align", "width", "height"]:
            if alt.has_attr(attr):
                del alt[attr]


def _cleanup_output_html(soup: BeautifulSoup, chapter_title: Optional[str] = None) -> None:
    _remove_navigation_boilerplate(soup)
    _remove_social_boilerplate(soup)
    _remove_subscription_boilerplate(soup)
    _remove_topic_footer_boilerplate(soup)
    _remove_publisher_widget_boilerplate(soup)
    _remove_generic_link_cluster_boilerplate(soup)
    _remove_duplicate_article_title_and_meta(soup, chapter_title)
    _normalize_caption_markup(soup)
    for tag in list(soup.find_all(["b", "strong", "em", "i", "span"])):
        if not tag.get_text(strip=True) and not tag.find(["img", "table", "blockquote", "pre", "code"]):
            tag.decompose()


def prepare_book_for_output(book_data: BookData, options: Optional[ConversionOptions] = None) -> BookData:
    cleaned_chapters = []
    no_images = getattr(options, "no_images", False)
    for chapter in book_data.chapters:
        soup = BeautifulSoup(chapter.content_html or "", "html.parser")
        if no_images:
            ImageProcessor.remove_images_for_text_output(soup)
        _cleanup_output_html(soup, chapter.title)
        cleaned_chapters.append(replace(chapter, content_html=str(soup)))

    return replace(book_data, chapters=cleaned_chapters, images=[] if no_images else book_data.images)


def shared_reading_css() -> str:
    return """
            h1 { font-size: 1.45em; line-height: 1.18; margin: 0 0 0.55em; }
            h2 { font-size: 1.2em; line-height: 1.22; margin: 1.05em 0 0.45em; }
            h3, h4 { line-height: 1.25; margin: 0.9em 0 0.35em; }
            p { margin-top: 0; margin-bottom: 0.7em; }
            ul, ol { margin-top: 0.25em; margin-bottom: 0.85em; padding-left: 1.35em; }
            li { margin-bottom: 0.28em; }
            hr { border: 0; border-top: 1px solid #d6d6d6; margin: 1em 0; }
            blockquote { border-left: 3px solid #bbb; margin: 0.85em 0; padding: 0 0 0 0.85em; color: #333; }
            blockquote p { margin-bottom: 0.55em; }
            table { width: 100%; max-width: 100%; margin: 0.9em 0 1.1em; border-collapse: collapse; border-spacing: 0; font-size: 0.92em; line-height: 1.25; text-indent: 0; }
            figure table { text-align: left; margin-left: 0; margin-right: 0; }
            th, td { border: 1px solid #b8b8b8; padding: 0.35em 0.45em; vertical-align: top; text-align: left; text-indent: 0; }
            th { background: #f3f3f3; font-weight: bold; }
            th[data-align="right"], td[data-align="right"] { text-align: right; }
            th p, td p { margin: 0 0 0.25em; text-indent: 0; }
            th p:last-child, td p:last-child { margin-bottom: 0; }
            .img-block { margin: 1em 0 0.75em; page-break-inside: avoid; break-inside: avoid; -webkit-column-break-inside: avoid; text-align: center; }
            .img-block img { max-width: 100%; max-height: 70vh; height: auto; display: block; margin: 0 auto; object-fit: contain; }
            .epub-image { max-width: 100%; height: auto; display: block; }
            figure, .wp-caption { margin: 1em 0 0.75em; text-align: center; page-break-inside: avoid; break-inside: avoid; }
            .image-caption { display: block; margin: 0.22em auto 0.85em; padding: 0; max-width: 92%; font-size: 0.82em; line-height: 1.25; color: #555; font-style: italic; text-align: center; text-indent: 0; }
            .image-alt { display: block; margin: 0.25em 0 0.75em; padding: 0; font-size: 0.82em; line-height: 1.25; color: #666; font-style: italic; text-indent: 0; }
            .post-meta { border-top: 1px solid #d8d8d8; border-bottom: 1px solid #d8d8d8; padding: 0.35em 0; margin: 0 0 1em; font-size: 0.78em; line-height: 1.25; color: #555; }
            .post-meta p { margin: 0.18em 0; }
            .post-meta a { color: #444; }
            .dala-translation { text-indent: 0; }
            .dala-translation-under { margin: -0.2em 0 0.85em 0.85em; padding: 0 0 0 0.65em; border-left: 2px solid #d0d0d0; color: #555; font-size: 0.9em; line-height: 1.34; font-style: italic; }
            .dala-caption-translation { max-width: 92%; margin: -0.55em auto 0.9em; padding-left: 0; border-left: 0; text-align: center; font-size: 0.82em; line-height: 1.25; color: #666; }
            .dala-translation-pair { width: 100%; table-layout: fixed; margin: 0.75em 0 1em; border-collapse: collapse; border: 0; }
            .dala-translation-pair td { width: 50%; vertical-align: top; border: 0; padding: 0.25em 0.55em; text-align: left; text-indent: 0; }
            .dala-caption-translation-pair { max-width: 92%; margin: -0.45em auto 0.9em; font-size: 0.82em; line-height: 1.25; }
            .dala-caption-translation-pair .image-caption, .dala-caption-translation-pair .image-alt { margin: 0; max-width: none; font-size: 1em; line-height: 1.25; text-align: left; }
            .dala-caption-translation-pair .dala-translation-target { font-size: 1em; line-height: 1.25; }
            .dala-translation-pair .dala-translation-source { color: #111; }
            .dala-translation-pair .dala-translation-target { color: #444; font-style: italic; }
            .dala-translation-pair p, .dala-translation-pair blockquote { margin: 0; }
            .dala-translation-ref { font-size: 0.72em; vertical-align: super; margin-left: 0.18em; text-decoration: none; }
            .dala-translation-footnotes { border-top: 1px solid #d0d0d0; margin-top: 1.4em; padding-top: 0.6em; font-size: 0.88em; }
            .dala-translation-footnotes h2 { font-size: 1.05em; margin: 0 0 0.55em; }
            .dala-translation-footnote { margin: 0 0 0.75em; }
            .dala-translation-footnote p { margin: 0 0 0.2em; }
            .dala-translation-backref { font-size: 0.85em; text-decoration: none; }
            pre { background: #f0f0f0; padding: 10px; overflow-x: auto; font-size: 0.9em; white-space: pre-wrap; }
        """


def epub_reading_css() -> str:
    return """
            body { font-family: Georgia, serif; margin: 0.65em; color: #111; line-height: 1.46; }
        """ + shared_reading_css()

class EpubWriter:
    @staticmethod
    def write(book_data: BookData, output_path: str, custom_css: str = None):
        book = epub.EpubBook()
        book.set_identifier(book_data.uid)
        book.set_title(book_data.title)
        book.set_language(book_data.language)
        book.add_author(book_data.author)

        pygments_style = HtmlFormatter(style='default').get_style_defs('.codehilite')
        base_css = epub_reading_css() + """
            .thread-container { margin-top: 25px; padding-top: 15px; border-top: 1px solid #ddd; }
            .comment-header { display: table; width: 100%; table-layout: auto; border-bottom: 1px solid #eee; margin-bottom: 4px; background-color: #f9f9f9; border-radius: 4px; }
            .comment-author { display: table-cell; width: auto; vertical-align: middle; padding: 4px 6px; }
            .comment-author-inner { display: block; font-weight: bold; color: #333; font-size: 0.95em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 45vw; }
            .nav-bar { display: table-cell; width: 1%; vertical-align: middle; white-space: nowrap; padding-right: 4px; }
            .nav-btn { display: inline-block; text-decoration: none; color: #666; font-weight: bold; font-size: 1.1em; padding: 0px 12px; height: 1.6em; line-height: 1.6em; border-left: 1px solid #ddd; text-align: center; margin-left: 14px; }
            .nav-btn:hover { background-color: #eee; color: #000; }
            .nav-btn.ghost { visibility: hidden; }
            .comment-body { margin-top: 2px; }
            .forum-post { border: 1px solid #e0e0e0; border-radius: 6px; padding: 6px; margin-bottom: 8px; background: #fff; }
            .forum-post-header { display: flex; justify-content: space-between; font-weight: 600; margin-bottom: 6px; font-size: 0.95em; color: #333; }
            .forum-author { color: #222; }
            .forum-time { color: #777; font-weight: 400; font-size: 0.9em; }
            .forum-post-body { font-size: 0.97em; color: #222; }
            .page-label { margin: 14px 0 8px 0; padding: 6px 8px; background: #eef5ff; border-left: 3px solid #4a7bd4; font-weight: 600; border-radius: 4px; }
        """ + pygments_style
        if custom_css:
            base_css += f"\n{custom_css}"

        css_item = epub.EpubItem(uid="style_default", file_name="style/default.css", media_type="text/css", content=base_css)
        book.add_item(css_item)

        added_filenames = set()
        added_filenames.add("style/default.css")

        for asset in book_data.images:
            if asset.filename in added_filenames:
                log.warning(f"Skipping duplicate image filename in writer: {asset.filename}")
                continue
            img = epub.EpubImage(uid=asset.uid, file_name=asset.filename, media_type=asset.media_type, content=asset.content)
            book.add_item(img)
            added_filenames.add(asset.filename)

        epub_chapters = []
        for chap in book_data.chapters:
            c = epub.EpubHtml(title=chap.title, file_name=chap.filename, lang='en')
            c.content = chap.content_html
            c.add_item(css_item)
            book.add_item(c)
            epub_chapters.append(c)

        if book_data.toc_structure:
            book.toc = tuple(book_data.toc_structure)
        else:
            book.toc = tuple(epub_chapters)

        book.add_item(epub.EpubNcx())
        book.add_item(epub.EpubNav())
        book.spine = ['nav'] + epub_chapters

        epub.write_epub(output_path, book)
        log.info(f"Wrote EPUB: {output_path}")


class PdfWriter:
    PAGE_SIZES = {
        "letter": {"format": "Letter"},
        "a4": {"format": "A4"},
        "kobo_clara": {"width": "90mm", "height": "120mm"},
    }

    PRESET_CSS = {
        "document": """
            @page { margin: 0.7in 0.65in; }
            body { font-family: "Noto Serif", Georgia, serif; font-size: 11pt; line-height: 1.5; color: #111; }
            img { max-height: 8.5in; }
            .img-block img, figure img { max-height: 8in; }
        """,
        "ereader": """
            @page { margin: 5mm; }
            body { font-family: "Noto Serif", Georgia, serif; font-size: 10pt; line-height: 1.32; color: #111; }
            img { max-height: 108mm; }
            .img-block img, figure img { max-height: 104mm; }
        """,
    }

    @staticmethod
    def _normalize_asset_key(src: str) -> str:
        return src.split("#", 1)[0].split("?", 1)[0].lstrip("./")

    @classmethod
    def _pdf_image_params(cls, options: Optional[ConversionOptions]) -> tuple[int, int, str]:
        max_dim, quality, color_mode, _ = ImageProcessor.image_optimize_params(options)
        return max_dim, quality, color_mode

    @classmethod
    def _pdf_asset_bytes(cls, asset, options: Optional[ConversionOptions]) -> tuple[bytes, str]:
        if asset.media_type.lower() in {"image/svg+xml", "image/gif"}:
            return asset.content, Path(asset.filename).suffix or ".img"
        try:
            from PIL import Image as PillowImage
        except ImportError:
            return asset.content, Path(asset.filename).suffix or ".img"

        max_dim, quality, color_mode = cls._pdf_image_params(options)
        try:
            with PillowImage.open(io.BytesIO(asset.content)) as img:
                img.load()
                if img.width > max_dim or img.height > max_dim:
                    img.thumbnail((max_dim, max_dim), PillowImage.Resampling.LANCZOS)
                if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                    background = PillowImage.new("RGB", img.size, (255, 255, 255))
                    if img.mode == "P":
                        img = img.convert("RGBA")
                    background.paste(img, mask=img.split()[3])
                    img = background
                elif img.mode != "RGB":
                    img = img.convert("RGB")
                if color_mode == "grayscale":
                    img = img.convert("L")
                out = io.BytesIO()
                img.save(out, format="JPEG", quality=quality, optimize=True, subsampling="4:2:0")
                return out.getvalue(), ".jpg"
        except Exception as exc:
            log.debug(f"Could not prepare PDF JPEG asset {asset.filename}: {exc}")
            return asset.content, Path(asset.filename).suffix or ".img"

    @classmethod
    def _asset_file_uris(cls, book_data: BookData, asset_dir: Path, options: Optional[ConversionOptions] = None, pdf_assets: bool = False) -> dict:
        assets = {}
        seen_names = set()
        original_bytes = 0
        written_bytes = 0
        converted = 0
        for idx, asset in enumerate(book_data.images, start=1):
            filename = cls._normalize_asset_key(asset.filename)
            base_name = sanitize_filename(filename.rsplit("/", 1)[-1] or f"image_{idx}") or f"image_{idx}"
            suffix = Path(base_name).suffix
            if not suffix and "/" in asset.media_type:
                guessed = {
                    "image/jpeg": ".jpg",
                    "image/png": ".png",
                    "image/gif": ".gif",
                    "image/webp": ".webp",
                }.get(asset.media_type.lower())
                if guessed:
                    base_name = f"{base_name}{guessed}"
            if base_name in seen_names:
                stem = Path(base_name).stem
                suffix = Path(base_name).suffix
                base_name = f"{stem}_{idx}{suffix}"
            seen_names.add(base_name)

            out_path = asset_dir / base_name
            content = asset.content
            if pdf_assets:
                content, pdf_suffix = cls._pdf_asset_bytes(asset, options)
                if Path(base_name).suffix.lower() != pdf_suffix.lower():
                    out_path = out_path.with_suffix(pdf_suffix)
                    converted += 1
            out_path.write_bytes(content)
            original_bytes += len(asset.content)
            written_bytes += len(content)
            file_uri = out_path.as_uri()
            assets[filename] = file_uri
            assets[filename.rsplit("/", 1)[-1]] = file_uri
        if pdf_assets and book_data.images:
            log.info(
                "PDF image render assets: count=%d converted=%d source_bytes=%d temp_bytes=%d",
                len(book_data.images),
                converted,
                original_bytes,
                written_bytes,
            )
        return assets

    @classmethod
    def _chapter_html(cls, chapter, asset_refs: dict) -> str:
        soup = BeautifulSoup(chapter.content_html or "", "html.parser")
        for img in soup.find_all("img"):
            src = img.get("src")
            if not src:
                continue
            key = cls._normalize_asset_key(src)
            asset_ref = asset_refs.get(key) or asset_refs.get(key.rsplit("/", 1)[-1])
            if asset_ref:
                img["src"] = asset_ref
        body = soup.body or soup
        first_h1 = body.find("h1")
        if first_h1 and _normalized_title(first_h1.get_text(" ", strip=True)) == _normalized_title(chapter.title or ""):
            next_tag = first_h1.find_next_sibling()
            first_h1.decompose()
            if next_tag and next_tag.name == "hr":
                next_tag.decompose()
        for tag in soup.find_all(["h1", "h2", "h3", "h4"]):
            tag["data-keep-with-next"] = "true"
        return body.decode_contents()

    @classmethod
    def _chapter_anchor(cls, idx: int, chapter) -> str:
        raw = chapter.uid or chapter.filename or chapter.title or f"chapter-{idx}"
        anchor = sanitize_filename(str(raw)).lower().replace("_", "-")
        return anchor or f"chapter-{idx}"

    @classmethod
    def _chapter_subentries(cls, chapter) -> list[tuple[str, str]]:
        soup = BeautifulSoup(chapter.content_html or "", "html.parser")
        entries = []
        seen = set()
        for label in soup.select(".page-label[id]"):
            anchor = label.get("id")
            if not anchor or anchor in seen:
                continue
            text = _compact_text(label.get_text(" ", strip=True)) or anchor.replace("_", " ").title()
            entries.append((text, anchor))
            seen.add(anchor)
        return entries

    @classmethod
    def _toc_entries(cls, book_data: BookData, include_contents: bool) -> list[TocEntry]:
        entries: list[TocEntry] = []
        first_chapter_same_title = bool(
            book_data.chapters
            and _normalized_title(book_data.title or "") == _normalized_title(book_data.chapters[0].title or "")
        )
        if book_data.title and not first_chapter_same_title:
            entries.append(TocEntry(book_data.title, None, 0))
        chapter_fallback_start = 2 if include_contents else 0
        for idx, chapter in enumerate(book_data.chapters, start=1):
            chapter_anchor = cls._chapter_anchor(idx, chapter)
            fallback_page = max(0, chapter_fallback_start + idx - 1)
            children = tuple(
                TocEntry(title, anchor, fallback_page)
                for title, anchor in cls._chapter_subentries(chapter)
            )
            entries.append(TocEntry(
                getattr(chapter, "toc_title", None) or chapter.title or f"Chapter {idx}",
                chapter_anchor,
                fallback_page,
                children,
            ))
        return entries

    @classmethod
    def _toc_html(cls, book_data: BookData) -> str:
        entries = cls._toc_entries(book_data, include_contents=len(book_data.chapters) > 1)
        has_children = any(entry.children for entry in entries)
        if len(book_data.chapters) <= 1 and not has_children:
            return ""

        items = []
        for entry in entries:
            if entry.anchor is None and len(entries) > 1:
                continue
            title = html.escape(entry.title)
            href = f' href="#{html.escape(entry.anchor)}"' if entry.anchor else ""
            child_html = ""
            if entry.children:
                child_items = [
                    f'<li><a href="#{html.escape(child.anchor or "")}">{html.escape(child.title)}</a></li>'
                    for child in entry.children
                ]
                child_html = f"<ol>{''.join(child_items)}</ol>"
            items.append(f'<li><a{href}>{title}</a>{child_html}</li>')
        return f"""
        <div class="pdf-toc">
            <h1>Contents</h1>
            <ol>{''.join(items)}</ol>
        </div>
        """

    @classmethod
    def build_html(
        cls,
        book_data: BookData,
        options: Optional[ConversionOptions] = None,
        custom_css: str = None,
        asset_refs: Optional[dict] = None,
    ) -> str:
        preset = (getattr(options, "pdf_preset", None) or "document").lower()
        css = cls.PRESET_CSS.get(preset, cls.PRESET_CSS["document"])
        pygments_style = HtmlFormatter(style="default").get_style_defs(".codehilite")
        if custom_css:
            css += f"\n{custom_css}"

        asset_refs = asset_refs or {}
        chapters = []
        show_document_header = len(book_data.chapters) > 1
        for idx, chapter in enumerate(book_data.chapters, start=1):
            anchor = cls._chapter_anchor(idx, chapter)
            chapters.append(
                f"""
                <div class="chapter" id="{anchor}">
                    <h1>{html.escape(chapter.title or f"Chapter {idx}")}</h1>
                    {cls._chapter_html(chapter, asset_refs)}
                </div>
                """
            )
        toc = cls._toc_html(book_data)
        header = ""
        if show_document_header:
            header = f"""
<header>
    <h1 class="title">{html.escape(book_data.title or "Untitled")}</h1>
    <div class="meta">{html.escape(book_data.author or "")}</div>
</header>
"""

        return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{html.escape(book_data.title or "Untitled")}</title>
<style>
{css}
{pygments_style}
{shared_reading_css()}
body {{ margin: 0; background: white; }}
h1, h2, h3, h4, [data-keep-with-next="true"] {{ break-after: avoid; }}
h1.title {{ font-size: 1.8em; margin: 0 0 0.25em; }}
.meta {{ color: #555; font-size: 0.9em; margin-bottom: 1.5em; }}
.pdf-toc {{ break-before: page; break-after: page; }}
.pdf-toc h1 {{ margin-top: 0; }}
.pdf-toc ol {{ padding-left: 1.4em; }}
.pdf-toc li {{ margin: 0 0 0.45em; }}
.pdf-toc a {{ color: #111; text-decoration: none; }}
.chapter {{ break-before: page; }}
.chapter:first-of-type {{ break-before: auto; }}
img, .img-block, figure {{ break-inside: avoid; max-width: 100%; }}
img {{ max-width: 100%; height: auto; object-fit: contain; }}
pre, code {{ overflow-wrap: anywhere; }}
table {{ max-width: 100%; border-collapse: collapse; }}
a {{ color: #1b4f8f; }}
</style>
</head>
<body>
{header}
{toc}
{''.join(chapters)}
</body>
</html>"""

    @classmethod
    def pdf_render_options(cls, output_path: str, options: Optional[ConversionOptions] = None) -> dict:
        page_size = (getattr(options, "pdf_page_size", None) or "letter").lower()
        pdf_options = dict(cls.PAGE_SIZES.get(page_size, cls.PAGE_SIZES["letter"]))
        pdf_options.update({
            "path": output_path,
            "print_background": True,
            "prefer_css_page_size": False,
        })
        preset = (getattr(options, "pdf_preset", None) or "document").lower()
        if preset == "document":
            pdf_options.update({
                "display_header_footer": True,
                "header_template": "<span></span>",
                "footer_template": (
                    '<div style="width:100%;font-size:8px;color:#777;'
                    'padding:0 0.65in;text-align:right;">'
                    '<span class="pageNumber"></span> / <span class="totalPages"></span>'
                    '</div>'
                ),
            })
        return pdf_options

    @classmethod
    def _chapter_outline_entries(cls, book_data: BookData, include_contents: bool) -> list[tuple[str, Optional[str], int]]:
        entries: list[tuple[str, Optional[str], int]] = []
        for entry in cls._toc_entries(book_data, include_contents):
            entries.append((entry.title, entry.anchor, entry.fallback_page))
            for child in entry.children:
                entries.append((child.title, child.anchor, child.fallback_page))
        return entries

    @classmethod
    def add_native_outline(cls, output_path: str, book_data: BookData, include_contents: bool) -> None:
        try:
            from pypdf import PdfReader
            from pypdf import PdfWriter as PypdfWriter
        except ImportError as exc:
            raise OutputWriteError(
                "PDF native bookmarks require pypdf. Install dependencies with `uv sync`."
            ) from exc

        path = Path(output_path)
        reader = PdfReader(str(path))
        if not reader.pages:
            return

        writer = PypdfWriter()
        if getattr(reader, "pdf_header", None):
            writer.pdf_header = reader.pdf_header
            writer._header = reader.pdf_header.encode("ascii", "ignore")
        pypdf_logger = logging.getLogger("pypdf")
        previous_level = pypdf_logger.level
        pypdf_logger.setLevel(max(previous_level, logging.ERROR))
        try:
            writer.clone_document_from_reader(reader)
        finally:
            pypdf_logger.setLevel(previous_level)

        named_destinations = reader.named_destinations or {}
        for title, anchor, fallback_page in cls._chapter_outline_entries(book_data, include_contents):
            page_number = fallback_page
            if anchor:
                destination = named_destinations.get(f"/{anchor}") or named_destinations.get(anchor)
                if destination is not None:
                    try:
                        page_number = reader.get_page_number(destination.page)
                    except Exception:
                        page_number = fallback_page
            page_number = max(0, min(page_number, len(reader.pages) - 1))
            writer.add_outline_item(title, page_number)

        with tempfile.NamedTemporaryFile(
            prefix=f"{path.stem}-outlined-",
            suffix=path.suffix,
            dir=str(path.parent),
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            writer.write(tmp)
        tmp_path.replace(path)

    @staticmethod
    def pdf_browser_executable(options: Optional[ConversionOptions] = None) -> Optional[str]:
        configured_executable = getattr(options, "browser_executable", None) or os.getenv("DALA_BROWSER_EXECUTABLE")
        resolved_executable = resolve_browser_executable(configured_executable) if configured_executable else None
        if configured_executable and not resolved_executable:
            raise OutputWriteError(f"PDF output browser executable does not exist or is not executable: {configured_executable}")
        return resolved_executable

    @classmethod
    async def write(cls, book_data: BookData, output_path: str, options: Optional[ConversionOptions] = None, custom_css: str = None):
        try:
            from playwright.async_api import Error as PlaywrightError
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise OutputWriteError(
                "PDF output requires Playwright. Install it with `uv sync --extra browser` "
                "and then run `uv run playwright install chromium`, or use system Chromium."
            ) from exc

        pdf_options = cls.pdf_render_options(output_path, options)
        resolved_executable = cls.pdf_browser_executable(options)

        async def launch_browser(playwright, executable_path: Optional[str] = None):
            kwargs = {"headless": True}
            if executable_path:
                kwargs["executable_path"] = executable_path
            else:
                kwargs["channel"] = "chromium"
            return await playwright.chromium.launch(**kwargs)

        try:
            async with async_playwright() as p:
                try:
                    browser = await launch_browser(p, resolved_executable)
                except PlaywrightError as exc:
                    fallback = None if resolved_executable else resolve_browser_executable()
                    if not fallback:
                        raise OutputWriteError(
                            "PDF output could not launch Chromium. Run `uv run playwright install chromium`, "
                            "or install `chromium`/`chromium-browser` on PATH."
                        ) from exc
                    try:
                        browser = await launch_browser(p, fallback)
                    except PlaywrightError as fallback_exc:
                        raise OutputWriteError(
                            f"PDF output could not launch system Chromium at {fallback}."
                        ) from fallback_exc

                try:
                    with tempfile.TemporaryDirectory(prefix="dala-pdf-assets-") as asset_tmp:
                        asset_dir = Path(asset_tmp)
                        asset_refs = cls._asset_file_uris(book_data, asset_dir, options, pdf_assets=True)
                        content = cls.build_html(book_data, options, custom_css, asset_refs=asset_refs)
                        html_path = asset_dir / "index.html"
                        html_path.write_text(content, encoding="utf-8")
                        page = await browser.new_page()
                        await page.goto(html_path.as_uri(), wait_until="load")
                        await page.pdf(**pdf_options)
                        cls.add_native_outline(output_path, book_data, include_contents=len(book_data.chapters) > 1)
                finally:
                    await browser.close()
        except OutputWriteError:
            raise
        except Exception as exc:
            raise OutputWriteError(f"PDF output failed: {exc}") from exc

        try:
            output_size = Path(output_path).stat().st_size
        except OSError:
            output_size = 0
        log.info(f"Wrote PDF: {output_path} ({output_size} bytes)")


async def write_output_book(
    book_data: BookData,
    output_path: str,
    options: Optional[ConversionOptions] = None,
    custom_css: str = None,
) -> None:
    book_data = prepare_book_for_output(book_data, options)
    book_data = await TranslationProcessor.translate_book(book_data, options)
    output_format = normalize_output_format(getattr(options, "output_format", None))
    if output_format == "pdf":
        await PdfWriter.write(book_data, output_path, options, custom_css)
    else:
        EpubWriter.write(book_data, output_path, custom_css)
