from pathlib import Path

import pytest

from app.processor.chunkers import (
    ChunkingStrategy,
    DEFAULT_CHUNK_CHARS,
    DEFAULT_OVERLAP_TOKENS,
    DEFAULT_CHUNK_TOKENS,
    _split_paragraphs,
    chunk_pages,
    estimate_tokens,
)
from app.processor.pdf import PdfExtractor
from app.processor.schemas import PageRecord

FIXTURES = Path(__file__).parent / "fixtures"


def page(
    n: int,
    text: str,
    section: str | None = None,
    source: str = "s3://knowflow/org-1/doc.pdf",
) -> PageRecord:
    return PageRecord(
        document_id="d1",
        organization_id="o1",
        page_number=n,
        content=text,
        section=section,
        source=source,
    )


def all_words(pages):
    return [w for p in pages for w in p.content.split()]


def chunk_words(chunks):
    return [w for c in chunks for w in c.content.split()]


def test_empty_pages_produce_no_chunks():
    assert chunk_pages([]) == []
    assert chunk_pages([], "token") == []


def test_default_strategy_is_paragraph():
    chunks = chunk_pages([page(1, "Hello world. Goodbye.")])
    assert chunks[0].metadata["strategy"] == "paragraph"


def test_unknown_strategy_raises():
    with pytest.raises(ValueError):
        chunk_pages([page(1, "text")], "kaboom")


def test_chunk_indexes_are_contiguous_and_coverage_is_exact():
    pages = [
        page(1, "First page has several words here. And a few more.", section="Intro"),
        page(2, "Second page keeps the flow going onward. Continue on.", section="Body"),
    ]
    for strategy in ("fixed", "token", "paragraph"):
        chunks = chunk_pages(pages, strategy)
        assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
        assert chunk_words(chunks) == all_words(pages), strategy


def test_token_strategy_respects_the_token_budget():
    words = "ab " * 300
    chunks = chunk_pages([page(1, words, section="S")], "token", chunk_tokens=32)

    assert len(chunks) > 1
    assert all(c.token_count <= 32 for c in chunks)
    assert chunk_words(chunks) == all_words([page(1, words)])


def test_fixed_strategy_respects_the_character_budget():
    words = "w01 " * 300
    chunks = chunk_pages([page(1, words)], "fixed", chunk_chars=120)

    assert len(chunks) > 1
    assert all(len(c.content) <= 120 for c in chunks)
    assert chunk_words(chunks) == all_words([page(1, words)])


def test_token_chunks_are_disjoint_but_overlap_chunks_share_a_tail():
    words = " ".join(f"w{i:03d}" for i in range(240))
    pages = [page(1, words, section="S")]

    disjoint = chunk_pages(pages, "token", chunk_tokens=40)
    overlapping = chunk_pages(
        pages, "overlap", chunk_tokens=40, overlap_tokens=12
    )
    assert len(overlapping) > 1
    for a, b in zip(disjoint, disjoint[1:]):
        assert set(a.content.split()) & set(b.content.split()) == set()
    pairs = list(zip(overlapping, overlapping[1:]))
    assert pairs, "overlap must yield multiple chunks"
    shared = [set(a.content.split()) & set(b.content.split()) for a, b in pairs]
    assert all(s for s in shared), "consecutive overlap chunks must share a tail"


def test_overlap_requires_overlap_larger_than_zero_by_less_than_the_budget():
    with pytest.raises(ValueError):
        chunk_pages([page(1, "some words")], "overlap", chunk_tokens=16, overlap_tokens=16)


def test_paragraph_strategy_keeps_blank_line_paragraphs_intact():
    content = (
        "First block with words of its own.\n\n"
        "Second block with words of its own.\n\n"
        "Third block with words of its own.\n"
    )
    chunks = chunk_pages([page(1, content)], "paragraph", chunk_tokens=16)

    assert [c.content for c in chunks] == [
        "First block with words of its own.",
        "Second block with words of its own.",
        "Third block with words of its own.",
    ]
    assert all("\n" not in c.content for c in chunks)


def test_paragraph_detector_rebuilds_blocks_from_single_newlines():
    # chapters-style text: heading line + wrapped sentences, no blank lines
    raw = (
        "Introduction\n"
        "KnowFlow is a platform that turns uploaded documents into answerable,\n"
        "citable ground truth.\n"
        "Every tenant owns its data outright.\n"
        "Architecture\n"
        "The ingest path is a small pipeline, and the storage round trips are cheap.\n"
    )
    assert _split_paragraphs(raw) == [
        "Introduction KnowFlow is a platform that turns uploaded documents into answerable, citable ground truth.",
        "Every tenant owns its data outright.",
        "Architecture The ingest path is a small pipeline, and the storage round trips are cheap.",
    ]


def test_paragraph_strategy_merges_until_budget_crossed():
    tiny = "One tiny paragraph. " * 4  # ~24 tokens total
    chunks = chunk_pages([page(1, tiny)], "paragraph", chunk_tokens=64)
    assert len(chunks) == 1
    assert chunks[0].token_count <= 64


def test_paragraph_strategy_hard_splits_one_overlong_paragraph():
    big = " ".join(f"word{i}" for i in range(400))
    chunks = chunk_pages([page(1, big)], "paragraph", chunk_tokens=32)

    assert len(chunks) > 2
    assert chunk_words(chunks) == big.split()
    assert all(c.token_count <= 128 for c in chunks[:-1] or chunks)


def test_merged_chunks_keep_the_citation_anchor():
    pages = [
        page(1, "Alpha paragraph here. One more sentence for weight.", section="Intro"),
        page(3, "Beta paragraph here. One more sentence for weight.", section="Body"),
    ]
    chunks = chunk_pages(pages, "paragraph", chunk_tokens=512)

    assert len(chunks) == 1
    c = chunks[0]
    assert c.page_number == 1
    assert c.metadata["page_range"] == [1, 3]
    assert c.metadata["section"] == "Intro"
    assert c.metadata["source"] == "s3://knowflow/org-1/doc.pdf"
    assert c.document_id == "d1"
    assert c.organization_id == "o1"
    assert c.token_count == estimate_tokens(c.content)


def test_token_count_and_estimate_agree():
    chunks = chunk_pages([page(1, "One two three four five six seven eight.")], "fixed")
    assert all(c.token_count == estimate_tokens(c.content) for c in chunks)


def test_estimate_matches_the_extraction_rule():
    assert estimate_tokens("a" * 100) == 25


def test_paragraph_strategy_on_a_real_pdf_keeps_sentences_intact():
    result = PdfExtractor().extract(
        (FIXTURES / "chapter.pdf").read_bytes(),
        document_id="d1",
        organization_id="o1",
        source="s3://knowflow/org-1/chapter.pdf",
    )
    chunks = chunk_pages(result.pages, "paragraph")

    assert chunks
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    distinctive = (
        "Paragraph chunks are the production choice because they keep each "
        "retrieval unit semantically coherent."
    )
    hits = [c for c in chunks if distinctive in c.content]
    assert len(hits) == 1


def test_chapter_strategies_differ_and_overlap_adds_redundancy():
    result = PdfExtractor().extract(
        (FIXTURES / "chapter.pdf").read_bytes(),
        document_id="d1",
        organization_id="o1",
    )
    counts = {
        name: len(chunk_pages(result.pages, name, chunk_tokens=128, chunk_chars=128))
        for name in ("fixed", "token", "paragraph")
    }
    counts["overlap"] = len(
        chunk_pages(result.pages, "overlap", chunk_tokens=128, overlap_tokens=32)
    )
    assert len(set(counts.values())) >= 2, counts
    overlap_tokens = sum(
        c.token_count
        for c in chunk_pages(result.pages, "overlap", chunk_tokens=128, overlap_tokens=32)
    )
    assert overlap_tokens > sum(p.token_count for p in result.pages)  # real overlap

    table = "\n".join(f"{k:<12} {v:>4} chunks" for k, v in sorted(counts.items()))
    print(table)