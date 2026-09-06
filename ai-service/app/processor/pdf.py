import concurrent.futures
import io
import math
import os
import re
import warnings
from dataclasses import dataclass

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from .schemas import ExtractResult, PageRecord


@dataclass
class PdfError(Exception):
    """The bytes are a real PDF but cannot be processed as one, or not a PDF."""

    reason: str = ""


class PdfExtractor:
    """Turns PDF bytes into per-page text with citation-ready metadata.

    Decisions:
    - One PageRecord per page, ordered by page_number. Page text is normalised
      so chunking can count tokens deterministically: horizontal whitespace
      collapses, blank-line runs become ``\n\n`` paragraph breaks (a soft new-
      line from PDF layout stays a single ``\n``). Blank pages are skipped
      over silently.
    - A document-level parsing failure raises PdfError (the processor stores
      the reason on documents.error and marks the document failed). A
      page-level text-stream failure degrades to a blank page and is counted,
      so one bad page cannot bury a whole document.
    - `section` is the nearest preceding outline/bookmark title for the page,
      giving us real structural anchors for citation (heading-splitting is the
      chunker's job on Day 10, this is the outline signal when the PDF has one).
    """

    def __init__(
        self,
        page_limit: int = 1000,
        tokens_per_char: float = 0.25,
        parallel_page_threshold: int | None = None,
    ):
        self.page_limit = page_limit
        self.tokens_per_char = tokens_per_char
        # pages at/above this count go through the process-parallel extractor;
        # None = auto (8x the CPU count, min 8). Small docs stay on the
        # single-threaded path so per-page overhead never costs them anything.
        self.parallel_page_threshold = parallel_page_threshold

    def extract(
        self,
        content: bytes,
        *,
        document_id: str,
        organization_id: str,
        source: str | None = None,
    ) -> ExtractResult:
        try:
            reader = PdfReader(io.BytesIO(content), strict=False)
        except (PdfReadError, ValueError, NotImplementedError) as exc:
            raise PdfError(
                f"unreadable PDF: {type(exc).__name__}: {exc}"
            ) from exc
        except Exception as exc:  # pypdf can throw anything; bytes are untrusted
            raise PdfError(f"unexpected parse failure: {type(exc).__name__}: {exc}") from exc

        sections = self._sections_by_page(reader)
        total = len(reader.pages)
        truncated = total > self.page_limit
        indices = list(range(min(total, self.page_limit)))

        if self._parallel_worth_it(indices):
            texts = self._extract_parallel(content, indices)
        else:
            texts = [(index, self._page_text(reader.pages[index])) for index in indices]

        pages: list[PageRecord] = []
        page_errors = 0
        for index, text in texts:
            if text is None:
                page_errors += 1
                continue  # failed stream reads as a page error, not a blank page
            if not text.strip():
                continue  # blank pages are skipped; numbering stays stable
            page_number = index + 1
            pages.append(
                PageRecord(
                    document_id=document_id,
                    organization_id=organization_id,
                    page_number=page_number,
                    content=text,
                    token_count=self._estimate_tokens(text),
                    section=sections.get(index),
                    source=source,
                )
            )

        is_empty = page_errors == 0 and len(pages) == 0
        return ExtractResult(
            pages=pages,
            page_errors=page_errors,
            truncated=truncated,
            empty=is_empty,
        )

    def _parallel_worth_it(self, indices: list[int]) -> bool:
        if len(indices) < 2:
            return False
        threshold = self.parallel_page_threshold
        if threshold is None:
            threshold = max(8, (os.cpu_count() or 1) * 8)
        return len(indices) >= threshold

    def _extract_parallel(
        self, content: bytes, indices: list[int]
    ) -> list[tuple[int, str | None]]:
        cpu = os.cpu_count() or 1
        workers = min(cpu, 4)
        slice_len = max(1, math.ceil(len(indices) / (workers * 2)))
        slices = [indices[s : s + slice_len] for s in range(0, len(indices), slice_len)]
        try:
            with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as ex:
                futures = [ex.submit(_extract_page_slice, content, sl) for sl in slices]
                texts: list[tuple[int, str | None]] = []
                for future in concurrent.futures.as_completed(futures):
                    texts.extend(future.result())
        except Exception:  # noqa: BLE001 - restricted envs can refuse to fork; degrade
            reader = PdfReader(io.BytesIO(content), strict=False)
            texts = [(index, self._page_text(reader.pages[index])) for index in indices]
        texts.sort(key=lambda pair: pair[0])
        return texts

    def _page_text(self, page) -> str | None:
        return _page_text_from_page(page)

    def _sections_by_page(self, reader: PdfReader) -> dict[int, str]:
        """Map page index -> the nearest preceding outline title."""
        entries: list[tuple[int, str]] = []
        for dest in self._flat_outline(reader.outline):
            page_index = self._page_index_of(reader, dest)
            title = getattr(dest, "title", None)
            if page_index is None or not title:
                continue
            entries.append((page_index, str(title)))
        entries.sort(key=lambda pair: pair[0])

        resolved: dict[int, str] = {}
        last: str | None = None
        pos = 0
        total = len(reader.pages) if reader.pages else 0
        for page_index in range(total):
            while pos < len(entries) and entries[pos][0] <= page_index:
                last = entries[pos][1]
                pos += 1
            if last is not None:
                resolved[page_index] = last
        return resolved

    @staticmethod
    def _flat_outline(outline):
        for item in outline or []:
            if isinstance(item, list):
                yield from PdfExtractor._flat_outline(item)
            else:
                yield item

    @staticmethod
    def _page_index_of(reader: PdfReader, dest) -> int | None:
        try:
            return reader.get_destination_page_number(dest)
        except Exception:
            return None

    def _estimate_tokens(self, text: str) -> int:
        return max(1, math.ceil(len(text) * self.tokens_per_char))


def _page_text_from_page(page) -> str | None:
    """Run one pypdf page through extraction + normalisation.

    Shared by the inline path (warm reader) and the process-parallel path
    (each worker re-parses the bytes and calls this per assigned page), so
    both routes agree byte-for-byte on page output.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            raw = page.extract_text() or ""
    except Exception:  # noqa: BLE001 - a broken page degrades, never crashes the doc
        return None
    return normalize_text(raw)


def _extract_page_slice(content: bytes, indices: list[int]) -> list[tuple[int, str | None]]:
    """Worker entry: parse the PDF bytes and extract the given page indices."""
    reader = PdfReader(io.BytesIO(content), strict=False)
    return [(index, _page_text_from_page(reader.pages[index])) for index in indices]


def normalize_text(raw: str) -> str:
    """Whitespace-normalise WITHOUT collapsing paragraph structure.

    Day-9 collapsed every newline to a space (fine for token counting, fatal
    for paragraph chunking). Now: horizontal runs collapse to one space, line
    breaks survive as ``\n``, and blank-line runs collapse to a single
    paragraph break ``\n\n`` so Day-10 chunkers can find semantic units.
    Token counting stays deterministic — it measures characters, and the
    estimator counts whatever text is actually stored.
    """
    text = re.sub(r"[ \t\u00a0\f\v]+", " ", raw)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()