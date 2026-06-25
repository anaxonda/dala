import pytest

import dala.cli as main
from dala.core.image_budget import ImageBudgetExceeded, assert_image_budget, prepare_books_for_bundle
from dala.core.image_processor import ImageProcessor
from dala.models import BookData, Chapter, ConversionOptions, ImageAsset, normalize_image_preset


def _book(source_url, image_content, image_name="images/hero.jpg"):
    return BookData(
        title=source_url,
        author="A",
        uid=f"urn:{source_url}",
        language="en",
        description="",
        source_url=source_url,
        chapters=[
            Chapter(
                title="Article",
                filename="index.xhtml",
                content_html=f'<p><img src="{image_name}" /></p>',
                uid="chapter",
                is_article=True,
            )
        ],
        images=[
            ImageAsset(
                uid=f"img-{source_url}",
                filename=image_name,
                media_type="image/jpeg",
                content=image_content,
                original_url=f"https://cdn.example/{source_url}/hero.jpg",
            )
        ],
    )


def test_prepare_books_for_bundle_renames_colliding_image_filenames():
    books, stats = prepare_books_for_bundle([
        _book("one", b"first"),
        _book("two", b"second"),
    ])

    assert stats.remapped_count == 1
    assert books[0].images[0].filename == "images/hero.jpg"
    assert books[1].images[0].filename != "images/hero.jpg"
    assert books[1].images[0].filename in books[1].chapters[0].content_html


def test_prepare_books_for_bundle_dedupes_identical_image_content():
    books, stats = prepare_books_for_bundle([
        _book("one", b"same"),
        _book("two", b"same"),
    ])

    assert stats.duplicate_count == 1
    assert len(books[0].images) == 1
    assert books[1].images == []
    assert 'src="images/hero.jpg"' in books[1].chapters[0].content_html


def test_create_bundle_uses_prepared_image_refs():
    bundle = main.create_bundle([
        _book("one", b"first"),
        _book("two", b"second"),
    ], "Bundle", "Author")

    filenames = [img.filename for img in bundle.images]
    assert len(filenames) == 2
    assert len(set(filenames)) == 2
    assert all(any(name in chapter.content_html for chapter in bundle.chapters) for name in filenames)


def test_create_bundle_prefixes_toc_entries_with_published_date():
    book = _book("one", b"first")
    book.title = "Article One"
    book.extra_metadata["published_date"] = "2025-08-15"

    bundle = main.create_bundle([book], "Bundle", "Author")

    assert bundle.toc_structure[0].title == "2025-08-15 - Article One"
    assert bundle.chapters[0].toc_title == "2025-08-15 - Article One"


def test_bundle_filename_title_adds_today_or_date_range(monkeypatch):
    class FixedDateTime:
        @staticmethod
        def now():
            class FixedNow:
                @staticmethod
                def strftime(fmt):
                    assert fmt == "%Y-%m-%d"
                    return "2026-06-25"
            return FixedNow()

    monkeypatch.setattr(main, "datetime", FixedDateTime)

    assert main.bundle_filename_title("Daily Read", ConversionOptions()) == "Daily Read_2026-06-25"
    assert main.bundle_filename_title(
        "August Posts",
        ConversionOptions(start_date="2025-08", end_date="2025-08-31"),
    ) == "August Posts_2025-08_to_2025-08-31"
    assert main.bundle_filename_title(
        "site_2025-08_to_2025-08-31",
        ConversionOptions(start_date="2025-08", end_date="2025-08-31"),
    ) == "site_2025-08_to_2025-08-31"


def test_assert_image_budget_fails_before_write():
    book = _book("large", b"x" * 1024, "images/a.jpg")
    book.images.append(ImageAsset(
        uid="img-b",
        filename="images/b.jpg",
        media_type="image/jpeg",
        content=b"y" * 1024,
        original_url="https://cdn.example/b.jpg",
    ))

    with pytest.raises(ImageBudgetExceeded, match="2 images exceeds 1"):
        assert_image_budget(book, ConversionOptions(max_bundle_images=1))


def test_full_image_preset_has_no_default_count_or_byte_budget():
    book = _book("large", b"x" * 1024, "images/a.jpg")
    book.images.extend(
        ImageAsset(
            uid=f"img-{i}",
            filename=f"images/{i}.jpg",
            media_type="image/jpeg",
            content=b"x" * 1024,
            original_url=f"https://cdn.example/{i}.jpg",
        )
        for i in range(450)
    )

    stats = assert_image_budget(book, ConversionOptions(image_preset="full"))
    assert stats.image_count == 451


def test_compact_image_preset_uses_smaller_optimization_params():
    assert ImageProcessor.image_optimize_params(ConversionOptions(image_preset="balanced")) == (1000, 65, "color", "source")
    assert ImageProcessor.image_optimize_params(ConversionOptions(image_preset="compact")) == (720, 50, "color", "webp")
    assert ImageProcessor.image_optimize_params(ConversionOptions(image_preset="optimized", image_color="grayscale")) == (720, 50, "grayscale", "webp")


def test_legacy_image_preset_names_normalize():
    assert normalize_image_preset("baseline") == "balanced"
    assert normalize_image_preset("optimized") == "compact"
    assert normalize_image_preset("compact") == "compact"
    assert normalize_image_preset("unknown") == "balanced"
