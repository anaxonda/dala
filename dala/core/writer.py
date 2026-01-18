from ebooklib import epub
from pygments.formatters import HtmlFormatter
from . .models import log, BookData

class EpubWriter:
    @staticmethod
    def write(book_data: BookData, output_path: str, custom_css: str = None):
        book = epub.EpubBook()
        book.set_identifier(book_data.uid)
        book.set_title(book_data.title)
        book.set_language(book_data.language)
        book.add_author(book_data.author)

        pygments_style = HtmlFormatter(style='default').get_style_defs('.codehilite')
        base_css = """
            body { font-family: sans-serif; margin: 0.5em; background-color: #fdfdfd; line-height: 1.5; }
            .img-block { margin: 0.5em 0; page-break-inside: avoid; break-inside: avoid; -webkit-column-break-inside: avoid; text-align: center; }
            .img-block img { max-width: 100%; max-height: 70vh; height: auto; display: block; margin: 0 auto; object-fit: contain; }
            .img-block .caption { margin: 0.25em 0 0; font-size: 0.9em; color: #555; }
            .epub-image { max-width: 100%; height: auto; display: block; }
            figure { margin: 0; text-align: center; }
            figcaption { font-size: 0.8em; color: #666; font-style: italic; margin-top: 0; }
            .post-meta { background: #f5f5f5; padding: 10px; margin-bottom: 20px; border-radius: 5px; font-size: 0.9em; }
            .thread-container { margin-top: 25px; padding-top: 15px; border-top: 1px solid #ddd; }
            .comment-header { display: table; width: 100%; table-layout: auto; border-bottom: 1px solid #eee; margin-bottom: 4px; background-color: #f9f9f9; border-radius: 4px; }
            .comment-author { display: table-cell; width: auto; vertical-align: middle; padding: 4px 6px; }
            .comment-author-inner { display: block; font-weight: bold; color: #333; font-size: 0.95em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 45vw; }
            .nav-bar { display: table-cell; width: 1%; vertical-align: middle; white-space: nowrap; padding-right: 4px; }
            .nav-btn { display: inline-block; text-decoration: none; color: #666; font-weight: bold; font-size: 1.1em; padding: 0px 12px; height: 1.6em; line-height: 1.6em; border-left: 1px solid #ddd; text-align: center; margin-left: 14px; }
            .nav-btn:hover { background-color: #eee; color: #000; }
            .nav-btn.ghost { visibility: hidden; }
            .comment-body { margin-top: 2px; }
            pre { background: #f0f0f0; padding: 10px; overflow-x: auto; font-size: 0.9em; }
            p { margin-top: 0; margin-bottom: 0.4em; }
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

        for asset in book_data.images:
            img = epub.EpubImage(uid=asset.uid, file_name=asset.filename, media_type=asset.media_type, content=asset.content)
            book.add_item(img)

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
