from dala.models import BookData, Chapter, ImageAsset


def make_chapter(
    title: str = "Chapter",
    filename: str = "chapter.xhtml",
    content_html: str = "<p>Body</p>",
    uid: str = "chapter",
    **kwargs,
) -> Chapter:
    return Chapter(title=title, filename=filename, content_html=content_html, uid=uid, **kwargs)


def make_book(
    title: str = "Book",
    author: str = "Author",
    uid: str = "urn:test",
    chapters: list[Chapter] | None = None,
    images: list[ImageAsset] | None = None,
    source_url: str = "",
    **kwargs,
) -> BookData:
    return BookData(
        title=title,
        author=author,
        uid=uid,
        language="en",
        description="",
        source_url=source_url,
        chapters=chapters if chapters is not None else [make_chapter()],
        images=images if images is not None else [],
        **kwargs,
    )


def make_forum_book(page_count: int = 2, **kwargs) -> BookData:
    body = []
    for page in range(1, page_count + 1):
        body.append(f'<div class="page-label" id="page_{page}">Page {page}</div>')
        body.append(f"<p>Page {page} body</p>")
    return make_book(
        title="Forum Thread",
        author="Forum",
        uid="urn:forum",
        chapters=[
            make_chapter(
                title="Forum Thread",
                filename="thread.xhtml",
                content_html="".join(body),
                uid="forum_thread",
            )
        ],
        **kwargs,
    )
