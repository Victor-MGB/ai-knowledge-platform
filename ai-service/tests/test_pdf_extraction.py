from pathlib import Path

import pytest

from app.processor.pdf import PdfError, PdfExtractor

FIXTURES = Path(__file__).parent / "fixtures"


def extract(name: str, **kwargs):
    return PdfExtractor().extract(
        (FIXTURES / name).read_bytes(),
        document_id="doc-1",
        organization_id="org-1",
        source="s3://knowflow/org-1/file.pdf",
        **kwargs,
    )


def test_multi_page_preserves_order_and_page_numbers():
    result = extract("three_pages.pdf")

    assert result.page_count == 3
    assert [p.page_number for p in result.pages] == [1, 2, 3]
    assert [p.content for p in result.pages] == [
        "Introduction to KnowFlow",
        "Architecture and storage",
        "Search and retrieval",
    ]
    assert not result.empty
    assert result.truncated is False


def test_sections_come_from_the_pdf_outline():
    result = extract("three_pages.pdf")

    # each page carries the nearest preceding outline title
    assert [p.section for p in result.pages] == [
        "Introduction",
        "Architecture",
        "Retrieval",
    ]


def test_plain_pdf_without_outline_has_no_sections():
    result = extract("two_page_plain.pdf")
    assert result.page_count == 2
    assert all(p.section is None for p in result.pages)


def test_source_and_document_id_are_preserved_for_citations():
    result = extract("two_page_plain.pdf")

    for page in result.pages:
        assert page.source == "s3://knowflow/org-1/file.pdf"
        assert page.document_id == "doc-1"
        assert page.organization_id == "org-1"


def test_whitespace_is_normalised_without_losing_words():
    result = extract("whitespace.pdf")

    assert result.pages[0].content == "Line one with many spaces"
    assert result.pages[1].content == "spaced out trailing spaces"


def test_blank_page_pdf_is_empty_not_failed():
    result = extract("blank_page.pdf")
    assert result.empty is True
    assert result.page_count == 0


def test_zero_page_pdf_is_empty_not_failed():
    result = extract("zero_pages.pdf")
    assert result.empty is True
    assert result.page_count == 0


def test_token_count_is_a_deterministic_estimate():
    result = extract("two_page_plain.pdf")
    assert all(p.token_count >= 1 for p in result.pages)


def test_corrupt_pdf_raises_pdf_error_with_reason():
    with pytest.raises(PdfError) as exc_info:
        extract("corrupt.pdf")
    assert "unreadable PDF" in exc_info.value.reason


def test_page_limit_truncates_and_flags():
    result = PdfExtractor(page_limit=1).extract(
        (FIXTURES / "three_pages.pdf").read_bytes(),
        document_id="d",
        organization_id="o",
    )
    assert result.page_count == 1
    assert result.truncated is True


def test_parallel_path_matches_sequential_exactly():
    content = (FIXTURES / "three_pages.pdf").read_bytes()

    seq = PdfExtractor(parallel_page_threshold=10**9).extract(
        content, document_id="d", organization_id="o"
    )
    par = PdfExtractor(parallel_page_threshold=2).extract(
        content, document_id="d", organization_id="o"
    )

    assert par.page_count == seq.page_count
    assert [p.page_number for p in par.pages] == [p.page_number for p in seq.pages]
    assert [p.content for p in par.pages] == [p.content for p in seq.pages]
    assert [p.section for p in par.pages] == [p.section for p in seq.pages]
    assert [p.token_count for p in par.pages] == [p.token_count for p in seq.pages]
    assert par.truncated == seq.truncated
    assert par.page_errors == seq.page_errors


def test_tiny_docs_stay_on_the_inline_path():
    # below the threshold the extractor must not spawn processes
    extractor = PdfExtractor(parallel_page_threshold=2)
    assert extractor._parallel_worth_it([0, 1]) is True
    assert extractor._parallel_worth_it([0]) is False