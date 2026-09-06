"""Day-10 chunking strategies over extracted pages.

All strategies consume ordered PageRecord text and emit ChunkRecord rows that
preserve the citation chain: `page_number` = page the chunk starts on, and
`metadata` carries section / page_range / s3:// source. Token math uses the
same 0.25 chars/token estimate as extraction, so a budget here is roughly a
real token budget for any embedding model.

Strategies (each is a trade-off, measured on Day 10 in docs/architecture.md):
  fixed     - hard CHARACTER budget, splits at word boundaries
  token     - greedy TOKEN budget (estimate 0.25 tokens/char)
  overlap   - sliding token windows; consecutive chunks share an overlap tail
  paragraph - semantic units (blank-line paragraphs); merges up to the budget,
              hard-split only when a single paragraph exceeds it
Production default is PARAGRAPH: it produces the fewest, most coherent units
and keeps section/paragraph structure, which matters when the RAG layer has
to cite a claim back to source.
"""

import math
import re
from dataclasses import dataclass
from enum import Enum

from .schemas import ChunkRecord, PageRecord

TOKENS_PER_CHAR = 0.25
DEFAULT_CHUNK_TOKENS = 512
DEFAULT_CHUNK_CHARS = 2048
DEFAULT_OVERLAP_TOKENS = 128

MIN_CHUNK_SIZE = 16
MAX_CHUNK_TOKENS = 4096
PARAGRAPH_BREAK = "\n\n"
MAX_PARAGRAPH_SPLIT = MAX_CHUNK_TOKENS << 2  # a single overlong unit we will not keep slicing


class ChunkingStrategy(str, Enum):
    FIXED = "fixed"
    TOKEN = "token"
    OVERLAP = "overlap"
    PARAGRAPH = "paragraph"


def estimate_tokens(text: str, tokens_per_char: float = TOKENS_PER_CHAR) -> int:
    """Keep extraction and chunking on the same deterministic token math."""
    return max(1, math.ceil(len(text) * tokens_per_char))


def _coerce(strategy: ChunkingStrategy | str) -> ChunkingStrategy:
    if isinstance(strategy, ChunkingStrategy):
        return strategy
    try:
        return ChunkingStrategy(strategy)
    except ValueError:
        raise ValueError(
            f"unknown chunking strategy {strategy!r}; "
            f"choose from {[s.value for s in ChunkingStrategy]}"
        ) from None


@dataclass(frozen=True)
class _Unit:
    """A traversable unit of text with its citation origin.

    For the word-stream strategies a unit is one word; for paragraph it is
    one paragraph. Every unit remembers which page/section/source it came
    from so a merged chunk never loses its anchor.
    """

    text: str
    page: int
    section: str | None
    source: str | None


def _word_units(pages: list[PageRecord]):
    for page in pages:
        for word in page.content.split():
            yield _Unit(word, page.page_number, page.section, page.source)


def _para_units(pages: list[PageRecord]):
    """Paragraphs reconstructed from page text.

    Blank-line runs are authoritative breaks; but reportlab-plus-pypdf (and
    most real PDFs) only ever produce single newlines between paragraphs, so a
    light heuristic also starts a new paragraph after a sentence-ending line
    or at a heading-looking line (short, not ending in punctuation, and
    followed by a line that starts a sentence). This is the section/heading
    heuristic Day 9 deferred to chunking, and it is deliberately bounded:
    worst case a page reads as one paragraph, never garbage."""

    for page in pages:
        for text in _split_paragraphs(page.content):
            yield _Unit(text, page.page_number, page.section, page.source)


_SENTENCE_END = (".", "!", "?", ":", ";")


def _ends_sentence(line: str) -> bool:
    return line.endswith(_SENTENCE_END)


def _looks_like_heading(line: str, nxt: str | None) -> bool:
    if nxt is None or len(line) > 60 or _ends_sentence(line):
        return False
    return bool(re.match(r"[A-Z]", nxt))


def _split_paragraphs(text: str) -> list[str]:
    """Reconstruct author paragraphs from ragged PDF line text."""
    lines = [ln.strip() for ln in re.split(r"\n+", text) if ln.strip()]
    if not lines:
        return []
    blocks: list[list[str]] = [[lines[0]]]
    for i in range(1, len(lines)):
        prev, cur = lines[i - 1], lines[i]
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        if _ends_sentence(prev) or _looks_like_heading(cur, nxt):
            blocks.append([])
        blocks[-1].append(cur)
    return [" ".join(b) for b in blocks]


def _est_joined(units: list[_Unit], sep: str, extra_text: str = "") -> int:
    text = sep.join([u.text for u in units])
    if extra_text:
        text += sep + extra_text
    return estimate_tokens(text)


def chunk_fixed(pages: list[PageRecord], *, chunk_chars: int = DEFAULT_CHUNK_CHARS) -> list[list[_Unit]]:
    """Hard character budget, word-aligned: predictable byte cost per chunk."""
    if chunk_chars < 1:
        raise ValueError("fixed chunk_chars must be >= 1")
    groups: list[list[_Unit]] = []
    buffer: list[_Unit] = []
    for unit in _word_units(pages):
        joined = " ".join([u.text for u in buffer] + [unit.text])
        if buffer and len(joined) > chunk_chars:
            groups.append(buffer)
            buffer = [unit]
        else:
            buffer.append(unit)
    if buffer:
        groups.append(buffer)
    return groups


def chunk_token(pages: list[PageRecord], *, chunk_tokens: int = DEFAULT_CHUNK_TOKENS) -> list[list[_Unit]]:
    """Greedy token budget: ~chunk_tokens per chunk at our char estimate."""
    if chunk_tokens < 1:
        raise ValueError("token chunk_tokens must be >= 1")
    groups: list[list[_Unit]] = []
    buffer: list[_Unit] = []
    for unit in _word_units(pages):
        projected = _est_joined(buffer, " ", unit.text)
        if buffer and projected > chunk_tokens:
            groups.append(buffer)
            buffer = [unit]
        else:
            buffer.append(unit)
    if buffer:
        groups.append(buffer)
    return groups


def chunk_overlap(
    pages: list[PageRecord],
    *,
    chunk_tokens: int = DEFAULT_CHUNK_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[list[_Unit]]:
    """Sliding token windows: consecutive chunks share an overlap tail.

    A chunk is a window sized to chunk_tokens; the next window starts after a
    hop that leaves ~overlap_tokens worth of text behind as shared context.
    """
    if overlap_tokens < 0:
        raise ValueError("overlap_tokens must be >= 0")
    if chunk_tokens < 1:
        raise ValueError("overlap chunk_tokens must be >= 1")
    if overlap_tokens >= chunk_tokens:
        raise ValueError("overlap_tokens must be < chunk_tokens")
    words = list(_word_units(pages))
    if not words:
        return []

    groups: list[list[_Unit]] = []
    start = 0
    total = len(words)
    while start < total:
        end = start + 1
        while end < total and _est_joined(words[start:end], " ", words[end].text) <= chunk_tokens:
            end += 1
        groups.append(words[start:end])
        if end >= total:
            break
        if overlap_tokens <= 0:
            start = end
            continue
        # hop: drop the leading part of the window, keep ~overlap_tokens tail
        tail_len = len(words[end - 1].text)
        j = end - 1
        while j - 1 >= start:
            grown = tail_len + 1 + len(words[j - 1].text)  # separator + word
            if math.ceil(grown * TOKENS_PER_CHAR) > overlap_tokens:
                break
            tail_len = grown
            j -= 1
        nxt = j
        if nxt <= start:
            nxt = start + 1  # guarantee progress even for hostile params
        start = nxt
    return groups


def chunk_paragraph(pages: list[PageRecord], *, chunk_tokens: int = DEFAULT_CHUNK_TOKENS) -> list[list[_Unit]]:
    """Semantic units: group paragraphs up to the budget; hard-split a single
    paragraph only when it exceeds the budget on its own."""
    if chunk_tokens < 1:
        raise ValueError("paragraph chunk_tokens must be >= 1")
    groups: list[list[_Unit]] = []
    buffer: list[_Unit] = []
    for unit in _para_units(pages):
        if estimate_tokens(unit.text) > chunk_tokens:
            if buffer:
                groups.append(buffer)
                buffer = []
            groups.extend(_split_overlong_paragraph(unit, chunk_tokens))
            continue
        if buffer:
            projected = _est_joined(buffer, PARAGRAPH_BREAK, unit.text)
        else:
            projected = estimate_tokens(unit.text)
        if buffer and projected > chunk_tokens:
            groups.append(buffer)
            buffer = [unit]
        else:
            buffer.append(unit)
    if buffer:
        groups.append(buffer)
    return groups


def _split_overlong_paragraph(unit: _Unit, chunk_tokens: int) -> list[list[_Unit]]:
    """One paragraph too big for a chunk: slice it at word granularity like
    the token strategy would, keeping the unit's page/section/source."""
    words = [_Unit(w, unit.page, unit.section, unit.source) for w in unit.text.split()]
    groups: list[list[_Unit]] = []
    buffer: list[_Unit] = []
    for word in words:
        projected = _est_joined(buffer, " ", word.text)
        if buffer and projected > chunk_tokens:
            groups.append(buffer)
            buffer = [word]
        else:
            buffer.append(word)
    if buffer:
        groups.append(buffer)
    return groups


def chunk_pages(
    pages: list[PageRecord],
    strategy: ChunkingStrategy | str = ChunkingStrategy.PARAGRAPH,
    *,
    chunk_tokens: int | None = None,
    chunk_chars: int | None = None,
    overlap_tokens: int | None = None,
) -> list[ChunkRecord]:
    """The public entry point: pages in, chunk rows out.

    Returns chunks ordered by chunk_index, contiguously from 0. An empty page
    list yields no chunks. `chunk_size` from the API maps to whichever budget
    the strategy uses (chars for fixed, tokens for the rest).
    """
    strategy = _coerce(strategy)
    tokens = chunk_tokens or DEFAULT_CHUNK_TOKENS
    chars = chunk_chars or DEFAULT_CHUNK_CHARS
    overlap = overlap_tokens if overlap_tokens is not None else DEFAULT_OVERLAP_TOKENS
    clamp = min(max(tokens, MIN_CHUNK_SIZE), MAX_CHUNK_TOKENS)

    if not pages:
        return []
    if any(p.page_number < 1 for p in pages):
        raise ValueError("page_number must be >= 1")

    if strategy is ChunkingStrategy.FIXED:
        groups = chunk_fixed(pages, chunk_chars=chars)
    elif strategy is ChunkingStrategy.TOKEN:
        groups = chunk_token(pages, chunk_tokens=clamp)
    elif strategy is ChunkingStrategy.OVERLAP:
        groups = chunk_overlap(pages, chunk_tokens=clamp, overlap_tokens=overlap)
    else:
        groups = chunk_paragraph(pages, chunk_tokens=clamp)

    return _materialize(pages, groups, strategy)


def _materialize(pages, groups: list[list[_Unit]], strategy: ChunkingStrategy) -> list[ChunkRecord]:
    source = pages[0]
    joiner = PARAGRAPH_BREAK if strategy is ChunkingStrategy.PARAGRAPH else " "
    records: list[ChunkRecord] = []
    for index, units in enumerate(groups):
        content = joiner.join(u.text for u in units).strip()
        first, last = units[0], units[-1]
        section = first.section or next((u.section for u in units if u.section), None)
        records.append(
            ChunkRecord(
                document_id=source.document_id,
                organization_id=source.organization_id,
                chunk_index=index,
                page_number=first.page,
                content=content,
                token_count=estimate_tokens(content),
                metadata={
                    "strategy": strategy.value,
                    "section": section,
                    "source": first.source,
                    "page_range": [first.page, last.page],
                },
            )
        )
    return records