import asyncio
import hashlib
import html
import json
import os
import re
import sqlite3
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from bs4 import BeautifulSoup, Tag

from ..models import BookData, Chapter, ConversionOptions, log
from ..utils.llm import LLMHelper


class TranslationError(RuntimeError):
    """Raised when requested translation cannot complete."""


TRANSLATABLE_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "blockquote", "li", "figcaption", "div"}
SKIP_ANCESTOR_TAGS = {"pre", "code", "table"}
CAPTION_CLASSES = {"image-caption", "image-alt", "caption", "wp-caption-text"}
CONTENT_SCOPE_CLASSES = {
    "bbwrapper",
    "bbcode-body",
    "comment-body",
    "comment-body-inner",
    "comment-content",
    "comment-copy",
    "comment-message",
    "comment-text",
    "comment__body",
    "forum-post",
    "forum-post-body",
    "message-body",
    "message-content",
    "messagecontent",
}
ALWAYS_SKIP_CLASSES = {
    "post-meta",
    "ai-summary",
    "comment-header",
    "forum-post-header",
    "nav-bar",
    "nav-btn",
    "page-label",
    "dala-translation-skip",
    "dala-translation",
    "dala-translation-pair",
}
MAX_BATCH_ITEMS = 20
MAX_BATCH_CHARS = 9000
DEFAULT_LLM_TRANSLATION_CONCURRENCY = 3
DEFAULT_GOOGLE_TRANSLATE_CHUNK_SIZE = 5
DEFAULT_GOOGLE_TRANSLATE_CONCURRENCY = 5
LANGUAGE_ALIASES = {
    "afrikaans": "af",
    "arabic": "ar",
    "chinese": "zh",
    "chinese simplified": "zh-cn",
    "chinese traditional": "zh-tw",
    "dutch": "nl",
    "english": "en",
    "french": "fr",
    "german": "de",
    "greek": "el",
    "hebrew": "he",
    "hindi": "hi",
    "indonesian": "id",
    "italian": "it",
    "japanese": "ja",
    "korean": "ko",
    "polish": "pl",
    "portuguese": "pt",
    "russian": "ru",
    "spanish": "es",
    "swedish": "sv",
    "turkish": "tr",
    "ukrainian": "uk",
    "vietnamese": "vi",
}


@dataclass(frozen=True)
class TranslationUnit:
    tag: Tag
    text: str
    kind: str = "body"


@dataclass(frozen=True)
class GlossaryTerm:
    source: str
    target: str
    placeholder: str


def normalize_translation_display(value: Optional[str]) -> str:
    display = (value or "underneath").strip().lower().replace("-", "_")
    if display in {"under", "inline", "below"}:
        display = "underneath"
    if display in {"side", "side_by_side", "sidebyside"}:
        display = "side_by_side"
    if display in {"popup", "footnote", "popup_footnote", "popup-footnote"}:
        display = "popup_footnote"
    if display in {"replace", "translated_only", "translated-only"}:
        display = "replace"
    if display not in {"underneath", "side_by_side", "popup_footnote", "replace"}:
        raise TranslationError(f"Unsupported translation display mode: {value}")
    return display


def normalize_translation_provider(value: Optional[str]) -> str:
    provider = (value or "llm").strip().lower()
    if provider not in {"llm", "google"}:
        raise TranslationError(f"Unsupported translation provider: {value}")
    return provider


def normalize_translation_scope(value: Optional[str]) -> str:
    scope = (value or "article-captions").strip().lower().replace("_", "-")
    aliases = {
        "captions": "article-captions",
        "article-with-captions": "article-captions",
        "article_caption": "article-captions",
        "all": "all-readable",
        "comments": "all-readable",
        "full": "all-readable",
    }
    scope = aliases.get(scope, scope)
    if scope not in {"article", "article-captions", "all-readable"}:
        raise TranslationError(f"Unsupported translation scope: {value}")
    return scope


def comparable_language(value: Optional[str]) -> str:
    lang = (value or "").strip().lower().replace("_", "-")
    if not lang or lang == "auto":
        return lang
    lang = LANGUAGE_ALIASES.get(lang, lang)
    return lang.split("-", 1)[0]


def translation_languages_match(source_lang: Optional[str], target_lang: Optional[str]) -> bool:
    source = comparable_language(source_lang)
    target = comparable_language(target_lang)
    return bool(source and target and source != "auto" and source == target)


def translation_cache_path() -> Path:
    configured = os.getenv("DALA_TRANSLATION_CACHE")
    if configured:
        return Path(configured).expanduser()
    return Path(os.getenv("XDG_CACHE_HOME", "~/.cache")).expanduser() / "dala" / "translations.sqlite"


def normalized_source_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


class TranslationCache:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or translation_cache_path()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path))
        if not self._initialized:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS translations (
                    cache_key TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    provider_model TEXT NOT NULL,
                    source_lang TEXT NOT NULL,
                    target_lang TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    translated_text TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()
            self._initialized = True
        return conn

    @staticmethod
    def key(provider: str, provider_model: str, source_lang: str, target_lang: str, text: str) -> str:
        material = "\n".join([
            provider,
            provider_model or "",
            source_lang or "auto",
            target_lang,
            normalized_source_text(text),
        ])
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def get_many(
        self,
        provider: str,
        provider_model: str,
        source_lang: str,
        target_lang: str,
        texts: Sequence[str],
    ) -> Dict[str, str]:
        if not texts:
            return {}
        keys = {
            text: self.key(provider, provider_model, source_lang, target_lang, text)
            for text in texts
        }
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT cache_key, translated_text FROM translations WHERE cache_key IN ({','.join('?' for _ in keys)})",
                list(keys.values()),
            ).fetchall()
        finally:
            conn.close()
        by_key = {key: value for key, value in rows}
        return {text: by_key[key] for text, key in keys.items() if key in by_key}

    def set_many(
        self,
        provider: str,
        provider_model: str,
        source_lang: str,
        target_lang: str,
        translations: Dict[str, str],
    ) -> None:
        if not translations:
            return
        conn = self._connect()
        try:
            conn.executemany(
                """
                INSERT OR REPLACE INTO translations
                    (cache_key, provider, provider_model, source_lang, target_lang, source_hash, translated_text)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        self.key(provider, provider_model, source_lang, target_lang, source),
                        provider,
                        provider_model or "",
                        source_lang or "auto",
                        target_lang,
                        hashlib.sha256(normalized_source_text(source).encode("utf-8")).hexdigest(),
                        translated,
                    )
                    for source, translated in translations.items()
                ],
            )
            conn.commit()
        finally:
            conn.close()

    def clear(self) -> bool:
        if self.path.exists():
            self.path.unlink()
            self._initialized = False
            return True
        return False


def batched_texts(texts: Sequence[str]) -> Iterable[List[str]]:
    batch: List[str] = []
    char_count = 0
    for text in texts:
        text_len = len(text)
        if batch and (len(batch) >= MAX_BATCH_ITEMS or char_count + text_len > MAX_BATCH_CHARS):
            yield batch
            batch = []
            char_count = 0
        batch.append(text)
        char_count += text_len
    if batch:
        yield batch


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def chunked_texts(texts: Sequence[str], size: int) -> Iterable[List[str]]:
    for idx in range(0, len(texts), size):
        yield list(texts[idx:idx + size])


def parse_translation_glossary(raw: Optional[str]) -> List[GlossaryTerm]:
    if not raw:
        return []
    pairs: List[Tuple[str, str]] = []
    stripped = raw.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                pairs.extend((str(k).strip(), str(v).strip()) for k, v in parsed.items())
        except json.JSONDecodeError:
            pass
    if not pairs:
        for line in stripped.splitlines():
            clean = line.strip()
            if not clean or clean.startswith("#"):
                continue
            if "=" in clean:
                source, target = clean.split("=", 1)
            elif "\t" in clean:
                source, target = clean.split("\t", 1)
            else:
                source, target = clean, clean
            pairs.append((source.strip(), target.strip()))
    terms = []
    seen = set()
    for source, target in sorted(pairs, key=lambda item: len(item[0]), reverse=True):
        if not source or source in seen:
            continue
        seen.add(source)
        terms.append(GlossaryTerm(source=source, target=target or source, placeholder=f"__DALA_TERM_{len(terms)}__"))
    return terms


def glossary_hash(terms: Sequence[GlossaryTerm]) -> str:
    if not terms:
        return ""
    material = "\n".join(f"{term.source}={term.target}" for term in terms)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def protect_glossary_terms(text: str, terms: Sequence[GlossaryTerm]) -> str:
    protected = text
    for term in terms:
        protected = protected.replace(term.source, term.placeholder)
    return protected


def restore_glossary_terms(text: str, terms: Sequence[GlossaryTerm]) -> str:
    restored = text
    for term in terms:
        restored = restored.replace(term.placeholder, term.target)
    return restored


async def translate_with_llm(
    texts: Sequence[str],
    source_lang: str,
    target_lang: str,
    provider: Optional[str],
    model: Optional[str],
    api_key: Optional[str],
    glossary_terms: Optional[Sequence[GlossaryTerm]] = None,
) -> Dict[str, str]:
    if not texts:
        return {}
    payload = [{"id": str(idx), "text": text} for idx, text in enumerate(texts)]
    glossary = ""
    if glossary_terms:
        glossary_payload = [{"source": term.placeholder, "target": term.target} for term in glossary_terms]
        glossary = (
            "Protected glossary placeholders are present in the text. Preserve each placeholder exactly, "
            "then the caller will restore it to the configured target term. Glossary: "
            f"{json.dumps(glossary_payload, ensure_ascii=False)} "
        )
    prompt = (
        "Translate each item in this JSON array literally and naturally. "
        "Do not summarize. Do not add commentary. Preserve paragraph meaning and tone. "
        f"{glossary}"
        "Return only a JSON array with objects containing the same id and a translated text field. "
        f"Source language: {source_lang or 'auto'}. Target language: {target_lang}.\n\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )
    result = await LLMHelper._call_llm(
        prompt,
        model,
        api_key,
        provider=provider,
        request_options={
            "gemini_generation_config": {
                "temperature": 0,
                "response_mime_type": "application/json",
                "max_output_tokens": 8192,
            },
            "chat_payload": {
                "temperature": 0,
                "max_tokens": 8192,
            },
            "openrouter_payload": {
                "reasoning_effort": "none",
                "reasoning": {"effort": "none", "exclude": True},
                "include_reasoning": False,
            },
        },
    )
    if not result:
        raise TranslationError("LLM translation returned no result.")
    cleaned = result.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise TranslationError("LLM translation did not return valid JSON.") from exc
    by_id = {
        str(item.get("id")): str(item.get("text") or "").strip()
        for item in parsed
        if isinstance(item, dict)
    }
    translations = {}
    for idx, text in enumerate(texts):
        translated = by_id.get(str(idx))
        if not translated:
            raise TranslationError("LLM translation response was missing one or more items.")
        translations[text] = translated
    return translations


async def translate_with_google(texts: Sequence[str], source_lang: str, target_lang: str) -> Dict[str, str]:
    if not texts:
        return {}
    try:
        from deep_translator import GoogleTranslator
    except ImportError as exc:
        raise TranslationError(
            "Google translation requires deep-translator. Install dependencies with `uv sync`."
        ) from exc

    def _translate() -> List[str]:
        translator = GoogleTranslator(source=source_lang or "auto", target=target_lang)
        return translator.translate_batch(list(texts))

    try:
        translated = await asyncio.to_thread(_translate)
    except Exception as exc:
        raise TranslationError(f"Google translation failed: {exc}") from exc
    if len(translated) != len(texts):
        raise TranslationError("Google translation returned an unexpected number of items.")
    return {source: str(target or "").strip() for source, target in zip(texts, translated)}


async def translate_google_chunk(texts: Sequence[str], source_lang: str, target_lang: str) -> Dict[str, str]:
    return await translate_with_google(texts, source_lang, target_lang)


def chapter_should_translate(chapter: Chapter, scope: str) -> bool:
    if chapter.is_comments and scope != "all-readable":
        return False
    if chapter.uid == "forum_thread" and scope != "all-readable":
        return False
    if not chapter.is_article and scope != "all-readable":
        return False
    return True


def tag_class_set(tag: Tag) -> Set[str]:
    return {str(class_name).strip().lower() for class_name in (tag.get("class") or [])}


def tag_has_class(tag: Tag, classes: Set[str]) -> bool:
    return bool(classes.intersection(tag_class_set(tag)))


def is_caption_tag(tag: Tag) -> bool:
    return tag.name == "figcaption" or tag_has_class(tag, CAPTION_CLASSES)


def should_translate_tag(tag: Tag, scope: str) -> bool:
    if tag.name not in TRANSLATABLE_TAGS:
        return False
    if tag.name == "div":
        if not tag_has_class(tag, CONTENT_SCOPE_CLASSES):
            return False
        if tag.find(["h1", "h2", "h3", "h4", "h5", "h6", "p", "blockquote", "li", "div"]):
            return False
    if tag_has_class(tag, ALWAYS_SKIP_CLASSES):
        return False
    if tag_has_class(tag, CONTENT_SCOPE_CLASSES) and scope != "all-readable":
        return False
    if is_caption_tag(tag) and scope == "article":
        return False
    if tag.find_parent(SKIP_ANCESTOR_TAGS):
        return False
    for parent in tag.parents:
        if not isinstance(parent, Tag):
            continue
        if tag_has_class(parent, ALWAYS_SKIP_CLASSES):
            return False
        if tag_has_class(parent, CONTENT_SCOPE_CLASSES) and scope != "all-readable":
            return False
    if tag.name == "li" and tag.find(["ul", "ol", "table", "pre", "code", "img", "picture", "figure", "p"]):
        return False
    if tag.find(["img", "picture", "figure", "table", "pre", "code"]):
        return False
    text = normalized_source_text(tag.get_text(" ", strip=True))
    if len(text) < 2:
        return False
    if re.fullmatch(r"[\W\d_]+", text):
        return False
    return True


def collect_translation_units(soup: BeautifulSoup, scope: str) -> List[TranslationUnit]:
    units: List[TranslationUnit] = []
    seen = set()
    for tag in soup.find_all(list(TRANSLATABLE_TAGS)):
        if not isinstance(tag, Tag) or not should_translate_tag(tag, scope):
            continue
        text = normalized_source_text(tag.get_text(" ", strip=True))
        if not text or id(tag) in seen:
            continue
        seen.add(id(tag))
        units.append(TranslationUnit(tag=tag, text=text, kind="caption" if is_caption_tag(tag) else "body"))
    return units


def make_translation_node(soup: BeautifulSoup, text: str, target_lang: str, kind: str = "body") -> Tag:
    classes = "dala-translation dala-translation-under"
    if kind == "caption":
        classes += " dala-caption-translation"
    div = soup.new_tag("div", attrs={"class": classes, "lang": target_lang})
    div.string = text
    return div


def make_side_by_side_node(soup: BeautifulSoup, source_tag: Tag, translated: str, source_lang: str, target_lang: str, kind: str = "body") -> Tag:
    table = soup.new_tag("table", attrs={"class": "dala-translation-pair"})
    if kind == "caption":
        table["class"] = "dala-translation-pair dala-caption-translation-pair"
    tbody = soup.new_tag("tbody")
    tr = soup.new_tag("tr")
    source_td = soup.new_tag("td", attrs={"class": "dala-translation-source", "lang": source_lang or "auto"})
    target_td = soup.new_tag("td", attrs={"class": "dala-translation-target", "lang": target_lang})
    source_fragment = BeautifulSoup(str(source_tag), "html.parser")
    for child in list(source_fragment.contents):
        source_td.append(child)
    target_td.string = translated
    tr.append(source_td)
    tr.append(target_td)
    tbody.append(tr)
    table.append(tbody)
    return table


def make_footnote_nodes(soup: BeautifulSoup, idx: int, translated: str, target_lang: str) -> Tuple[Tag, Tag]:
    note_id = f"dala-translation-note-{idx}"
    ref_id = f"dala-translation-ref-{idx}"
    ref = soup.new_tag(
        "a",
        attrs={
            "id": ref_id,
            "href": f"#{note_id}",
            "class": "dala-translation-ref",
            "epub:type": "noteref",
        },
    )
    ref.string = f"[{idx}]"
    aside = soup.new_tag(
        "aside",
        attrs={
            "id": note_id,
            "class": "dala-translation-footnote",
            "epub:type": "footnote",
            "lang": target_lang,
        },
    )
    back = soup.new_tag("a", attrs={"href": f"#{ref_id}", "class": "dala-translation-backref"})
    back.string = "↩"
    p = soup.new_tag("p")
    p.string = translated
    aside.append(p)
    aside.append(back)
    return ref, aside


class TranslationProcessor:
    @staticmethod
    async def translate_texts(
        texts: Sequence[str],
        options: ConversionOptions,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> Dict[str, str]:
        provider = normalize_translation_provider(options.translation_provider)
        source_lang = options.translation_source_lang or "auto"
        target_lang = options.translation_target_lang
        if not target_lang:
            raise TranslationError("--translate requires a target language.")
        if translation_languages_match(source_lang, target_lang):
            log.info("Skipping translation because source and target languages are both %s.", target_lang)
            return {text: normalized_source_text(text) for text in texts if normalized_source_text(text)}
        glossary_terms = parse_translation_glossary(getattr(options, "translation_glossary", None))
        provider_model = f"{getattr(options, 'llm_provider', 'auto')}:{options.llm_model or ''}" if provider == "llm" else "google"
        term_hash = glossary_hash(glossary_terms)
        if term_hash:
            provider_model = f"{provider_model}|glossary:{term_hash}"
        cache = TranslationCache() if options.translation_cache else None

        translations: Dict[str, str] = {}
        unique_texts = list(dict.fromkeys(normalized_source_text(text) for text in texts if normalized_source_text(text)))
        if cache:
            cached = cache.get_many(provider, provider_model, source_lang, target_lang, unique_texts)
            translations.update(cached)
        missing = [text for text in unique_texts if text not in translations]
        processed = len(unique_texts) - len(missing)
        if not missing:
            log.info("Translation cache satisfied %d/%d text blocks using %s.", processed, len(unique_texts), provider)
            return translations

        llm_concurrency = env_int("DALA_TRANSLATION_CONCURRENCY", DEFAULT_LLM_TRANSLATION_CONCURRENCY, 1, 6)
        google_chunk_size = env_int("DALA_GOOGLE_TRANSLATE_CHUNK_SIZE", DEFAULT_GOOGLE_TRANSLATE_CHUNK_SIZE, 1, 50)
        google_concurrency = env_int("DALA_GOOGLE_TRANSLATE_CONCURRENCY", DEFAULT_GOOGLE_TRANSLATE_CONCURRENCY, 1, 5)
        batches = list(batched_texts(missing)) if provider == "llm" else list(chunked_texts(missing, google_chunk_size))
        concurrency = llm_concurrency if provider == "llm" else google_concurrency
        log.info(
            "Translating %d uncached text blocks using %s in %d batches at concurrency %d.",
            len(missing),
            provider,
            len(batches),
            concurrency,
        )

        async def translate_batch_once(batch: Sequence[str]) -> Dict[str, str]:
            protected_by_source = {text: protect_glossary_terms(text, glossary_terms) for text in batch}
            protected_batch = list(protected_by_source.values())
            if provider == "llm":
                batch_result = await translate_with_llm(
                    protected_batch,
                    source_lang,
                    target_lang,
                    getattr(options, "llm_provider", "auto"),
                    options.llm_model,
                    options.llm_api_key,
                    glossary_terms,
                )
            else:
                batch_result = await translate_google_chunk(protected_batch, source_lang, target_lang)
            return {
                source: restore_glossary_terms(batch_result.get(protected, ""), glossary_terms)
                for source, protected in protected_by_source.items()
            }

        async def translate_batch_with_fallback(batch: Sequence[str], depth: int = 0) -> Dict[str, str]:
            started = asyncio.get_running_loop().time()
            try:
                result = await translate_batch_once(batch)
            except Exception as first_exc:
                log.warning(
                    "Translation batch failed (%s, %d items, depth %d); retrying once: %s",
                    provider,
                    len(batch),
                    depth,
                    first_exc,
                )
                try:
                    result = await translate_batch_once(batch)
                except Exception as second_exc:
                    if len(batch) <= 1:
                        raise
                    midpoint = len(batch) // 2
                    log.warning(
                        "Translation batch retry failed (%s, %d items, depth %d); splitting: %s",
                        provider,
                        len(batch),
                        depth,
                        second_exc,
                    )
                    left, right = await asyncio.gather(
                        translate_batch_with_fallback(batch[:midpoint], depth + 1),
                        translate_batch_with_fallback(batch[midpoint:], depth + 1),
                    )
                    return {**left, **right}
            duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)
            log.info(
                "Translated batch using %s: items=%d chars=%d duration_ms=%d",
                provider,
                len(batch),
                sum(len(text) for text in batch),
                duration_ms,
            )
            return result

        semaphore = asyncio.Semaphore(concurrency)

        async def translate_guarded(batch: Sequence[str]) -> Dict[str, str]:
            async with semaphore:
                return await translate_batch_with_fallback(batch)

        tasks = [asyncio.create_task(translate_guarded(batch)) for batch in batches]
        for task in asyncio.as_completed(tasks):
            restored_result = await task
            translations.update(restored_result)
            if cache:
                cache.set_many(provider, provider_model, source_lang, target_lang, restored_result)
            processed += len(restored_result)
            log.info("Translated %d/%d text blocks using %s.", processed, len(unique_texts), provider)
            if progress_callback:
                progress_callback(processed, len(unique_texts))
        return translations

    @staticmethod
    async def test_provider(text: str, options: ConversionOptions) -> str:
        translated = await TranslationProcessor.translate_texts([text], options)
        return translated.get(normalized_source_text(text), "")

    @staticmethod
    async def translate_book(book_data: BookData, options: Optional[ConversionOptions]) -> BookData:
        if not options or not getattr(options, "translation_enabled", False):
            return book_data
        display = normalize_translation_display(options.translation_display)
        scope = normalize_translation_scope(getattr(options, "translation_scope", "article-captions"))
        if translation_languages_match(options.translation_source_lang, options.translation_target_lang):
            log.info(
                "Skipping book translation because source and target languages are both %s.",
                options.translation_target_lang,
            )
            return book_data
        output_format = (getattr(options, "output_format", None) or "epub").lower()
        if display == "popup_footnote" and output_format == "pdf":
            log.warning("Popup footnote translation is EPUB-only; using underneath translation for PDF.")
            display = "underneath"

        chapter_units: List[Tuple[int, BeautifulSoup, List[TranslationUnit]]] = []
        all_texts: List[str] = []
        for idx, chapter in enumerate(book_data.chapters):
            if not chapter_should_translate(chapter, scope):
                continue
            soup = BeautifulSoup(chapter.content_html or "", "html.parser")
            units = collect_translation_units(soup, scope)
            if not units:
                continue
            chapter_units.append((idx, soup, units))
            all_texts.extend(unit.text for unit in units)
            if display == "replace" and normalized_source_text(chapter.title):
                all_texts.append(chapter.title)

        if not chapter_units:
            log.info("Translation requested but no article text blocks were eligible.")
            return book_data

        if display == "replace" and normalized_source_text(book_data.title):
            all_texts.append(book_data.title)

        translations = await TranslationProcessor.translate_texts(all_texts, options)
        target_lang = options.translation_target_lang or ""
        source_lang = options.translation_source_lang or "auto"
        translated_chapters = list(book_data.chapters)
        footnote_index = 1

        for chapter_idx, soup, units in chapter_units:
            footnotes: List[Tag] = []
            for unit in units:
                tag = unit.tag
                source_text = unit.text
                translated = translations.get(source_text)
                if not translated:
                    raise TranslationError(f"Missing translation for text block: {source_text[:80]}")
                if display == "underneath":
                    tag.insert_after(make_translation_node(soup, translated, target_lang, unit.kind))
                elif display == "side_by_side":
                    pair = make_side_by_side_node(soup, tag, translated, source_lang, target_lang, unit.kind)
                    tag.replace_with(pair)
                elif display == "popup_footnote":
                    ref, aside = make_footnote_nodes(soup, footnote_index, translated, target_lang)
                    tag.append(ref)
                    footnotes.append(aside)
                    footnote_index += 1
                elif display == "replace":
                    tag.clear()
                    tag.string = translated
            if footnotes:
                section = soup.new_tag("section", attrs={"class": "dala-translation-footnotes"})
                heading = soup.new_tag("h2")
                heading.string = "Translations"
                section.append(heading)
                for note in footnotes:
                    section.append(note)
                (soup.body or soup).append(section)
            chapter_title = book_data.chapters[chapter_idx].title
            if display == "replace":
                chapter_title = translations.get(normalized_source_text(chapter_title), chapter_title)
            translated_chapters[chapter_idx] = replace(
                book_data.chapters[chapter_idx],
                title=chapter_title,
                content_html=str(soup),
            )

        log.info(
            "Translated %d text blocks across %d chapters using %s.",
            len(all_texts),
            len(chapter_units),
            normalize_translation_provider(options.translation_provider),
        )
        book_title = book_data.title
        if display == "replace":
            book_title = translations.get(normalized_source_text(book_title), book_title)

        return replace(
            book_data,
            title=book_title,
            chapters=translated_chapters,
            language=options.translation_target_lang or book_data.language,
            extra_metadata={
                **(book_data.extra_metadata or {}),
                "translation_provider": normalize_translation_provider(options.translation_provider),
                "translation_source_lang": options.translation_source_lang or "auto",
                "translation_target_lang": options.translation_target_lang or "",
                "translation_display": display,
                "translation_scope": scope,
            },
        )
