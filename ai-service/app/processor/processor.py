from ..core.config import Settings
from .chunkers import ChunkingStrategy, chunk_pages
from .downloader import ObjectDownloader, ObjectNotFoundError, S3Downloader
from .pdf import PdfError, PdfExtractor
from .repository import DocumentRepository, PostgresDocumentRepository
from .schemas import PageRecord, ProcessingResult


class ProcessorService:
    """The Day 9-10 ingestion step: claim -> download -> extract -> chunk -> persist.

    Outcome contract (statuses):
      ready            — pages AND chunks stored, document.status = ready
      failed           — nothing stored, document.status = failed + reason
      not_found        — no such document
      not_queued       — not claimable (already processing / ready / failed)
      unsupported_type — no parser yet (docx/md/html/txt); leaves it queued

    Two transactions: the first claims the document (queued -> processing) so
    two workers can't duplicate. The second persists pages, chunks and the
    ready flip in one go — chunking and extraction run before it, untracked;
    a failure anywhere marks the document failed instead of leaving it stuck
    queued. Chunking default is PARAGRAPH (the Day-10 production strategy);
    strategies are swappable per call for experiments or re-chunking.
    """

    def __init__(
        self,
        repository: DocumentRepository,
        downloader: ObjectDownloader,
        extractor: PdfExtractor,
        *,
        bucket: str,
        chunker=None,
        default_strategy: ChunkingStrategy = ChunkingStrategy.PARAGRAPH,
    ):
        self._repo = repository
        self._store = downloader
        self._extractor = extractor
        self._bucket = bucket
        self._chunker = chunker if chunker is not None else chunk_pages
        self._default_strategy = ChunkingStrategy(default_strategy)

    def process(
        self,
        document_id: str,
        strategy: ChunkingStrategy | str | None = None,
        *,
        chunk_tokens: int | None = None,
        chunk_chars: int | None = None,
        overlap_tokens: int | None = None,
    ) -> ProcessingResult:
        document_id = str(document_id)  # psycopg hands back UUID objects
        strategy = ChunkingStrategy(strategy) if strategy else self._default_strategy
        document = self._repo.get(document_id)
        if document is None:
            return ProcessingResult(document_id=document_id, status="not_found")

        if document.source_type != "pdf":
            return ProcessingResult(
                document_id=document_id,
                status="unsupported_type",
                detail=f"no parser for source_type={document.source_type} yet",
            )

        if document.status != "queued":
            return ProcessingResult(
                document_id=document_id,
                status="not_queued",
                detail=f"current status is {document.status}",
            )

        with self._repo.transaction():
            claimed = self._repo.claim(document_id)
        if claimed is None or claimed.storage_key is None:
            return ProcessingResult(
                document_id=document_id,
                status="not_queued",
                detail="claim lost the race (another worker is processing it)",
            )

        try:
            payload = self._store.get(claimed.storage_key)
            source = f"s3://{self._bucket}/{claimed.storage_key}"
            result = self._extractor.extract(
                payload,
                document_id=claimed.id,
                organization_id=claimed.organization_id,
                source=source,
            )
        except ObjectNotFoundError as exc:
            return self._fail(document_id, f"document missing from storage: {exc}")
        except PdfError as exc:
            return self._fail(document_id, f"pdf extraction failed: {exc.reason}")
        except Exception as exc:  # storage/network; do not let a worker crash
            return self._fail(document_id, f"processor error: {type(exc).__name__}: {exc}")

        try:
            chunks = (
                self._chunker(
                    result.pages,
                    strategy,
                    chunk_tokens=chunk_tokens,
                    chunk_chars=chunk_chars,
                    overlap_tokens=overlap_tokens,
                )
                if result.pages
                else []
            )
        except ValueError as exc:  # hostile/implausible parameters
            return self._fail(document_id, f"chunking parameters rejected: {exc}")
        except Exception as exc:
            return self._fail(document_id, f"chunking failed: {type(exc).__name__}: {exc}")

        try:
            with self._repo.transaction():
                self._repo.insert_pages(result.pages)
                self._repo.insert_chunks(chunks)
                self._repo.mark_ready(document_id)
        except Exception as exc:
            return self._fail(
                document_id,
                f"could not persist extraction+chunks: {type(exc).__name__}: {exc}",
            )

        return ProcessingResult(
            document_id=document_id,
            status="ready",
            pages=result.page_count,
            chunks=len(chunks),
            chunk_strategy=strategy.value,
            page_errors=result.page_errors,
            total_tokens=result.total_tokens,
            empty=result.empty,
            truncated=result.truncated,
            detail=(
                f"document ready: {len(chunks)} chunk(s) "
                f"(strategy={strategy.value})"
            ),
        )

    def _fail(self, document_id: str, detail: str) -> ProcessingResult:
        try:
            with self._repo.transaction():
                self._repo.mark_failed(document_id, detail)
        except Exception:
            pass  # the original failure is the reportable one
        return ProcessingResult(document_id=document_id, status="failed", detail=detail)


def build_processor_service(settings: Settings) -> ProcessorService:
    from ..core.db import connect

    connection = connect(settings.database_url)
    return ProcessorService(
        repository=PostgresDocumentRepository(connection),
        downloader=S3Downloader(settings),
        extractor=PdfExtractor(page_limit=settings.pdf_page_limit),
        bucket=settings.s3_bucket,
    )