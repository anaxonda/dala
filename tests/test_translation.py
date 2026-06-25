import pytest
from bs4 import BeautifulSoup

from dala.core.translation import TranslationCache, TranslationError, TranslationProcessor, normalize_translation_display
from dala.models import BookData, Chapter, ConversionOptions
from dala.utils.llm import LLMHelper


def make_translation_book():
    return BookData(
        title="Article",
        author="Author",
        uid="urn:translation-test",
        language="en",
        description="",
        source_url="https://example.com",
        chapters=[
            Chapter(
                title="Article",
                filename="article.xhtml",
                content_html="""
                <html><body>
                  <div class="post-meta"><p>Author metadata</p></div>
                  <h1>Article title</h1>
                  <p>Hello world.</p>
                  <ul><li>First list item</li><li><p>Nested list paragraph</p></li></ul>
                  <pre><code>print("Hello world")</code></pre>
                  <p class="image-caption">A useful caption</p>
                </body></html>
                """,
                uid="article",
                is_article=True,
            ),
            Chapter(
                title="Comments",
                filename="comments.xhtml",
                content_html="<html><body><p>Do not translate comments.</p></body></html>",
                uid="comments",
                is_comments=True,
            ),
        ],
    )


@pytest.mark.asyncio
async def test_translation_underneath_includes_captions_and_simple_lists(monkeypatch):
    async def fake_translate_texts(texts, options):
        return {text: f"ES: {text}" for text in texts}

    monkeypatch.setattr(TranslationProcessor, "translate_texts", fake_translate_texts)
    translated = await TranslationProcessor.translate_book(
        make_translation_book(),
        ConversionOptions(
            translation_enabled=True,
            translation_target_lang="es",
            translation_display="underneath",
            translation_cache=False,
        ),
    )

    article = BeautifulSoup(translated.chapters[0].content_html, "html.parser")
    comments = BeautifulSoup(translated.chapters[1].content_html, "html.parser")
    blocks = [tag.get_text(" ", strip=True) for tag in article.select(".dala-translation-under")]

    assert "ES: Article title" in blocks
    assert "ES: Hello world." in blocks
    assert "ES: First list item" in blocks
    assert "Author metadata" not in " ".join(blocks)
    assert "ES: A useful caption" in blocks
    assert "ES: Nested list paragraph" in blocks
    assert not comments.select(".dala-translation-under")
    assert translated.language == "es"


@pytest.mark.asyncio
async def test_translation_underneath_includes_ai_summary(monkeypatch):
    async def fake_translate_texts(texts, options):
        return {text: f"ES: {text}" for text in texts}

    book = make_translation_book()
    book.chapters[0].content_html = """
    <html><body>
      <div class="ai-summary"><h3>AI Summary</h3>This is the summary.</div>
      <p>Body text.</p>
    </body></html>
    """

    monkeypatch.setattr(TranslationProcessor, "translate_texts", fake_translate_texts)
    translated = await TranslationProcessor.translate_book(
        book,
        ConversionOptions(
            translation_enabled=True,
            translation_target_lang="es",
            translation_display="underneath",
            translation_cache=False,
        ),
    )

    article = BeautifulSoup(translated.chapters[0].content_html, "html.parser")
    blocks = [tag.get_text(" ", strip=True) for tag in article.select(".dala-translation-under")]

    assert "ES: AI Summary" in blocks
    assert "ES: This is the summary." in blocks


@pytest.mark.asyncio
async def test_translation_replace_includes_ai_summary(monkeypatch):
    async def fake_translate_texts(texts, options):
        return {text: f"ES: {text}" for text in texts}

    book = make_translation_book()
    book.chapters[0].content_html = """
    <html><body>
      <div class="ai-summary"><h3>AI Summary</h3><p>This is the summary.</p></div>
      <p>Body text.</p>
    </body></html>
    """

    monkeypatch.setattr(TranslationProcessor, "translate_texts", fake_translate_texts)
    translated = await TranslationProcessor.translate_book(
        book,
        ConversionOptions(
            translation_enabled=True,
            translation_target_lang="es",
            translation_display="replace",
            translation_cache=False,
        ),
    )

    article = BeautifulSoup(translated.chapters[0].content_html, "html.parser")

    assert article.select_one(".ai-summary h3").get_text(" ", strip=True) == "ES: AI Summary"
    assert article.select_one(".ai-summary p").get_text(" ", strip=True) == "ES: This is the summary."


@pytest.mark.asyncio
async def test_translation_side_by_side_uses_bitextual_style_table(monkeypatch):
    async def fake_translate_texts(texts, options):
        return {text: f"FR: {text}" for text in texts}

    monkeypatch.setattr(TranslationProcessor, "translate_texts", fake_translate_texts)
    translated = await TranslationProcessor.translate_book(
        make_translation_book(),
        ConversionOptions(
            translation_enabled=True,
            translation_target_lang="fr",
            translation_source_lang="en",
            translation_display="side_by_side",
            translation_cache=False,
        ),
    )

    soup = BeautifulSoup(translated.chapters[0].content_html, "html.parser")
    pair = soup.select_one("table.dala-translation-pair")

    assert pair is not None
    assert pair.select_one(".dala-translation-source")["lang"] == "en"
    assert pair.select_one(".dala-translation-target")["lang"] == "fr"
    assert "Article title" in pair.select_one(".dala-translation-source").get_text(" ", strip=True)
    assert "FR: Article title" in pair.select_one(".dala-translation-target").get_text(" ", strip=True)


@pytest.mark.asyncio
async def test_translation_popup_footnote_marks_epub_notes(monkeypatch):
    async def fake_translate_texts(texts, options):
        return {text: f"DE: {text}" for text in texts}

    monkeypatch.setattr(TranslationProcessor, "translate_texts", fake_translate_texts)
    translated = await TranslationProcessor.translate_book(
        make_translation_book(),
        ConversionOptions(
            output_format="epub",
            translation_enabled=True,
            translation_target_lang="de",
            translation_display="popup_footnote",
            translation_cache=False,
        ),
    )

    soup = BeautifulSoup(translated.chapters[0].content_html, "html.parser")

    assert soup.select_one(".dala-translation-ref")["epub:type"] == "noteref"
    assert soup.select_one(".dala-translation-footnote")["epub:type"] == "footnote"
    assert "DE: Article title" in soup.select_one(".dala-translation-footnote").get_text(" ", strip=True)


@pytest.mark.asyncio
async def test_translation_popup_footnote_falls_back_to_underneath_for_pdf(monkeypatch):
    async def fake_translate_texts(texts, options):
        return {text: f"IT: {text}" for text in texts}

    monkeypatch.setattr(TranslationProcessor, "translate_texts", fake_translate_texts)
    translated = await TranslationProcessor.translate_book(
        make_translation_book(),
        ConversionOptions(
            output_format="pdf",
            translation_enabled=True,
            translation_target_lang="it",
            translation_display="popup_footnote",
            translation_cache=False,
        ),
    )

    soup = BeautifulSoup(translated.chapters[0].content_html, "html.parser")

    assert soup.select(".dala-translation-under")
    assert not soup.select(".dala-translation-footnote")


def test_normalize_translation_display_accepts_replace_aliases():
    assert normalize_translation_display("replace") == "replace"
    assert normalize_translation_display("translated-only") == "replace"


@pytest.mark.asyncio
async def test_translation_replace_outputs_translated_text_only(monkeypatch):
    async def fake_translate_texts(texts, options):
        return {text: f"ES: {text}" for text in texts}

    monkeypatch.setattr(TranslationProcessor, "translate_texts", fake_translate_texts)
    translated = await TranslationProcessor.translate_book(
        make_translation_book(),
        ConversionOptions(
            translation_enabled=True,
            translation_target_lang="es",
            translation_display="replace",
            translation_cache=False,
        ),
    )

    soup = BeautifulSoup(translated.chapters[0].content_html, "html.parser")

    assert soup.find("p", string="ES: Hello world.") is not None
    assert not soup.select(".dala-translation")
    assert not soup.select(".dala-translation-pair")
    assert soup.find("p", string="Hello world.") is None
    assert translated.title == "ES: Article"
    assert translated.chapters[0].title == "ES: Article"
    assert translated.language == "es"


@pytest.mark.asyncio
async def test_translation_replace_skips_marked_transcript_body(monkeypatch):
    seen = []

    async def fake_translate_texts(texts, options):
        seen.extend(texts)
        return {text: f"ES: {text}" for text in texts}

    monkeypatch.setattr(TranslationProcessor, "translate_texts", fake_translate_texts)
    book = BookData(
        title="Video",
        author="Author",
        uid="urn:skip-transcript",
        language="en",
        description="",
        source_url="https://youtube.com/watch?v=abc",
        chapters=[
            Chapter(
                title="Video",
                filename="transcript.xhtml",
                content_html="""
                <html><body>
                  <h1>Video title</h1>
                  <div class="transcript-body dala-translation-skip" lang="es">
                    <p>Transcripción ya traducida.</p>
                  </div>
                </body></html>
                """,
                uid="transcript",
                is_article=True,
            )
        ],
    )

    translated = await TranslationProcessor.translate_book(
        book,
        ConversionOptions(
            translation_enabled=True,
            translation_target_lang="es",
            translation_display="replace",
            translation_cache=False,
        ),
    )
    text = BeautifulSoup(translated.chapters[0].content_html, "html.parser").get_text(" ", strip=True)

    assert "Video title" in seen
    assert "Video" in seen
    assert "Transcripción ya traducida." not in seen
    assert "ES: Video title" in text
    assert "Transcripción ya traducida." in text
    assert "ES: Transcripción ya traducida." not in text


def test_translation_cache_round_trip(tmp_path):
    cache = TranslationCache(tmp_path / "translations.sqlite")
    cache.set_many("google", "google", "auto", "es", {"Hello": "Hola"})

    assert cache.get_many("google", "google", "auto", "es", ["Hello", "Missing"]) == {"Hello": "Hola"}


@pytest.mark.asyncio
async def test_translation_article_scope_skips_captions(monkeypatch):
    async def fake_translate_texts(texts, options):
        return {text: f"ES: {text}" for text in texts}

    monkeypatch.setattr(TranslationProcessor, "translate_texts", fake_translate_texts)
    translated = await TranslationProcessor.translate_book(
        make_translation_book(),
        ConversionOptions(
            translation_enabled=True,
            translation_target_lang="es",
            translation_scope="article",
            translation_cache=False,
        ),
    )

    soup = BeautifulSoup(translated.chapters[0].content_html, "html.parser")
    blocks = [tag.get_text(" ", strip=True) for tag in soup.select(".dala-translation-under")]

    assert "ES: Hello world." in blocks
    assert "A useful caption" not in " ".join(blocks)


@pytest.mark.asyncio
async def test_translation_missing_provider_result_raises_clear_error(monkeypatch):
    async def fake_translate_texts(texts, options):
        return {}

    monkeypatch.setattr(TranslationProcessor, "translate_texts", fake_translate_texts)

    with pytest.raises(TranslationError, match="Missing translation for text block"):
        await TranslationProcessor.translate_book(
            make_translation_book(),
            ConversionOptions(
                translation_enabled=True,
                translation_target_lang="es",
                translation_cache=False,
            ),
        )


@pytest.mark.asyncio
async def test_translation_all_readable_includes_comments(monkeypatch):
    async def fake_translate_texts(texts, options):
        return {text: f"ES: {text}" for text in texts}

    monkeypatch.setattr(TranslationProcessor, "translate_texts", fake_translate_texts)
    translated = await TranslationProcessor.translate_book(
        make_translation_book(),
        ConversionOptions(
            translation_enabled=True,
            translation_target_lang="es",
            translation_scope="all-readable",
            translation_cache=False,
        ),
    )

    comments = BeautifulSoup(translated.chapters[1].content_html, "html.parser")

    assert "ES: Do not translate comments." in comments.get_text(" ", strip=True)


@pytest.mark.asyncio
async def test_translation_all_readable_includes_substack_comment_body_divs(monkeypatch):
    async def fake_translate_texts(texts, options):
        return {text: f"FR: {text}" for text in texts}

    monkeypatch.setattr(TranslationProcessor, "translate_texts", fake_translate_texts)
    book = BookData(
        title="Substack",
        author="Author",
        uid="urn:substack-comments",
        language="en",
        description="",
        source_url="https://example.substack.com/p/post",
        chapters=[
            Chapter(
                title="Substack Comments",
                filename="comments.xhtml",
                content_html="""
                <html><body>
                  <h1>Substack Comments</h1>
                  <div class="thread-container">
                    <div>
                      <div class="comment-header"><div class="comment-author">Reader</div><div class="nav-bar">↑ →</div></div>
                      <div class="comment-body">This comment should be translated.</div>
                    </div>
                  </div>
                </body></html>
                """,
                uid="comments",
                is_comments=True,
            )
        ],
    )

    translated = await TranslationProcessor.translate_book(
        book,
        ConversionOptions(
            translation_enabled=True,
            translation_target_lang="fr",
            translation_scope="all-readable",
            translation_cache=False,
        ),
    )
    soup = BeautifulSoup(translated.chapters[0].content_html, "html.parser")

    assert "FR: This comment should be translated." in soup.get_text(" ", strip=True)
    assert "FR: Reader" not in soup.get_text(" ", strip=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("body_class", ["comment-content", "comment-text", "message-body", "bbWrapper"])
async def test_translation_all_readable_includes_common_comment_and_forum_body_divs(monkeypatch, body_class):
    async def fake_translate_texts(texts, options):
        return {text: f"FR: {text}" for text in texts}

    monkeypatch.setattr(TranslationProcessor, "translate_texts", fake_translate_texts)
    book = BookData(
        title="Comments",
        author="Author",
        uid="urn:comment-classes",
        language="en",
        description="",
        source_url="https://example.com/thread",
        chapters=[
            Chapter(
                title="Thread",
                filename="thread.xhtml",
                content_html=f"""
                <html><body>
                  <div class="thread-container">
                    <div class="comment-header">Reader</div>
                    <div class="{body_class}">This body class should be translated.</div>
                  </div>
                </body></html>
                """,
                uid="comments",
                is_comments=True,
            )
        ],
    )

    translated = await TranslationProcessor.translate_book(
        book,
        ConversionOptions(
            translation_enabled=True,
            translation_target_lang="fr",
            translation_scope="all-readable",
            translation_cache=False,
        ),
    )
    text = BeautifulSoup(translated.chapters[0].content_html, "html.parser").get_text(" ", strip=True)

    assert "FR: This body class should be translated." in text
    assert "FR: Reader" not in text


@pytest.mark.asyncio
async def test_translation_all_readable_includes_forum_thread_body_but_not_headers(monkeypatch):
    async def fake_translate_texts(texts, options):
        return {text: f"FR: {text}" for text in texts}

    monkeypatch.setattr(TranslationProcessor, "translate_texts", fake_translate_texts)
    book = BookData(
        title="Forum Thread",
        author="Forum",
        uid="urn:forum-thread",
        language="en",
        description="",
        source_url="https://forum.example.com/thread",
        chapters=[
            Chapter(
                title="Forum Thread",
                filename="thread.xhtml",
                content_html="""
                <html><body>
                  <div class="forum-post">
                    <div class="forum-post-header"><span class="forum-author">Forum User</span></div>
                    <div class="forum-post-body">Forum post body should be translated.</div>
                  </div>
                </body></html>
                """,
                uid="forum_thread",
                is_article=True,
            )
        ],
    )

    untranslated = await TranslationProcessor.translate_book(
        book,
        ConversionOptions(
            translation_enabled=True,
            translation_target_lang="fr",
            translation_scope="article-captions",
            translation_cache=False,
        ),
    )
    assert "FR:" not in BeautifulSoup(untranslated.chapters[0].content_html, "html.parser").get_text(" ", strip=True)

    translated = await TranslationProcessor.translate_book(
        book,
        ConversionOptions(
            translation_enabled=True,
            translation_target_lang="fr",
            translation_scope="all-readable",
            translation_cache=False,
        ),
    )
    text = BeautifulSoup(translated.chapters[0].content_html, "html.parser").get_text(" ", strip=True)

    assert "FR: Forum post body should be translated." in text
    assert "FR: Forum User" not in text


@pytest.mark.asyncio
async def test_translation_glossary_preserves_terms(monkeypatch):
    async def fake_llm(texts, source_lang, target_lang, provider, model, api_key, glossary_terms=None):
        return {text: f"TR: {text}" for text in texts}

    monkeypatch.setattr("dala.core.translation.translate_with_llm", fake_llm)
    result = await TranslationProcessor.translate_texts(
        ["Read KOReader on Coye today."],
        ConversionOptions(
            translation_enabled=True,
            translation_provider="llm",
            translation_target_lang="es",
            translation_glossary="KOReader=KOReader\nCoye=Coye",
            translation_cache=False,
        ),
    )

    assert result["Read KOReader on Coye today."] == "TR: Read KOReader on Coye today."


@pytest.mark.asyncio
async def test_llm_translation_sends_no_reasoning_payload(monkeypatch):
    captured = {}

    async def fake_call(prompt, model, api_key, provider=None, request_options=None):
        captured["provider"] = provider
        captured["request_options"] = request_options
        return '[{"id":"0","text":"Hola"}]'

    monkeypatch.setattr("dala.core.translation.LLMHelper._call_llm", fake_call)

    result = await TranslationProcessor.translate_texts(
        ["Hello"],
        ConversionOptions(
            translation_enabled=True,
            translation_provider="llm",
            translation_target_lang="es",
            llm_provider="openrouter",
            llm_model="deepseek/deepseek-v4-flash",
            translation_cache=False,
        ),
    )

    assert result == {"Hello": "Hola"}
    assert captured["provider"] == "openrouter"
    assert captured["request_options"]["chat_payload"]["temperature"] == 0
    assert captured["request_options"]["openrouter_payload"]["reasoning_effort"] == "none"
    assert captured["request_options"]["openrouter_payload"]["include_reasoning"] is False


@pytest.mark.asyncio
async def test_llm_transcript_formatting_forwards_provider(monkeypatch):
    captured = {}

    async def fake_call(prompt, model, api_key, provider=None, request_options=None):
        captured["model"] = model
        captured["api_key"] = api_key
        captured["provider"] = provider
        return "<p>Formatted transcript.</p>"

    monkeypatch.setattr("dala.utils.llm.LLMHelper._call_llm", fake_call)

    result = await LLMHelper.format_transcript(
        "raw transcript",
        model="deepseek/deepseek-v4-flash",
        api_key="test-key",
        provider="openrouter",
    )

    assert result == "<p>Formatted transcript.</p>"
    assert captured == {
        "model": "deepseek/deepseek-v4-flash",
        "api_key": "test-key",
        "provider": "openrouter",
    }


@pytest.mark.asyncio
async def test_llm_summary_forwards_provider(monkeypatch):
    captured = {}

    async def fake_call(prompt, model, api_key, provider=None, request_options=None):
        captured["model"] = model
        captured["provider"] = provider
        return "<p>Summary.</p>"

    monkeypatch.setattr("dala.utils.llm.LLMHelper._call_llm", fake_call)

    result = await LLMHelper.generate_summary(
        "article text",
        model="gemini-3.1-flash-lite",
        provider="gemini",
    )

    assert result == "<p>Summary.</p>"
    assert captured == {
        "model": "gemini-3.1-flash-lite",
        "provider": "gemini",
    }


@pytest.mark.asyncio
async def test_google_translation_chunks_concurrently_and_preserves_order(monkeypatch):
    calls = []

    async def fake_google(texts, source_lang, target_lang):
        calls.append(list(texts))
        return {text: f"ES: {text}" for text in texts}

    monkeypatch.setenv("DALA_GOOGLE_TRANSLATE_CHUNK_SIZE", "2")
    monkeypatch.setenv("DALA_GOOGLE_TRANSLATE_CONCURRENCY", "5")
    monkeypatch.setattr("dala.core.translation.translate_google_chunk", fake_google)

    texts = ["one", "two", "three", "four", "five"]
    result = await TranslationProcessor.translate_texts(
        texts,
        ConversionOptions(
            translation_enabled=True,
            translation_provider="google",
            translation_target_lang="es",
            translation_cache=False,
        ),
    )

    assert result == {text: f"ES: {text}" for text in texts}
    assert calls == [["one", "two"], ["three", "four"], ["five"]]


@pytest.mark.asyncio
async def test_llm_translation_retries_and_splits_bad_batch(monkeypatch):
    calls = []

    async def fake_llm(texts, source_lang, target_lang, provider, model, api_key, glossary_terms=None):
        calls.append(list(texts))
        if len(texts) > 1:
            raise RuntimeError("bad json")
        return {texts[0]: f"ES: {texts[0]}"}

    monkeypatch.setenv("DALA_TRANSLATION_CONCURRENCY", "1")
    monkeypatch.setattr("dala.core.translation.translate_with_llm", fake_llm)

    result = await TranslationProcessor.translate_texts(
        ["one", "two"],
        ConversionOptions(
            translation_enabled=True,
            translation_provider="llm",
            translation_target_lang="es",
            translation_cache=False,
        ),
    )

    assert result == {"one": "ES: one", "two": "ES: two"}
    assert calls == [["one", "two"], ["one", "two"], ["one"], ["two"]]


@pytest.mark.asyncio
async def test_translation_skips_book_when_source_and_target_match(monkeypatch):
    called = False

    async def fake_translate_texts(texts, options):
        nonlocal called
        called = True
        return {text: f"ES: {text}" for text in texts}

    monkeypatch.setattr(TranslationProcessor, "translate_texts", fake_translate_texts)
    book = make_translation_book()
    translated = await TranslationProcessor.translate_book(
        book,
        ConversionOptions(
            translation_enabled=True,
            translation_source_lang="English",
            translation_target_lang="en",
            translation_cache=False,
        ),
    )

    assert translated is book
    assert called is False


@pytest.mark.asyncio
async def test_translation_texts_returns_identity_when_source_and_target_match():
    result = await TranslationProcessor.translate_texts(
        ["Hello world."],
        ConversionOptions(
            translation_enabled=True,
            translation_source_lang="en-US",
            translation_target_lang="en",
            translation_cache=False,
        ),
    )

    assert result == {"Hello world.": "Hello world."}
