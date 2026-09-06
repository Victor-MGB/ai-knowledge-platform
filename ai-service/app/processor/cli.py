"""Manual runner for the Day 9-10 pipeline, for ops and backfills.

Usage (from ai-service/, with the venv active):
    python -m app.processor.cli --document-id <uuid> [--json]
    python -m app.processor.cli --document-id <uuid> --chunk-strategy overlap --overlap-tokens 128

Mirrors POST /v1/process/documents/{id} without needing the server up.
"""

import argparse
import json
import sys

STRATEGIES = ("fixed", "token", "overlap", "paragraph")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run KnowFlow processing (PDF extraction + chunking) for one document"
    )
    parser.add_argument("--document-id", required=True, help="document id (uuid)")
    parser.add_argument(
        "--chunk-strategy",
        choices=STRATEGIES,
        default="paragraph",
        help="chunking strategy (default: paragraph, the production choice)",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=None,
        help="budget per chunk: characters for fixed, tokens for the rest",
    )
    parser.add_argument(
        "--overlap-tokens",
        type=int,
        default=None,
        help="shared tail between consecutive chunks (overlap strategy)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON, exit 0 on ready")
    args = parser.parse_args()

    from ..core.config import get_settings
    from .processor import build_processor_service

    service = build_processor_service(get_settings())
    result = service.process(
        args.document_id,
        args.chunk_strategy,
        chunk_tokens=args.chunk_size,
        chunk_chars=args.chunk_size,
        overlap_tokens=args.overlap_tokens,
    )

    if args.json:
        print(json.dumps(result.model_dump(), indent=2))
    else:
        print(f"[{result.status}] document={result.document_id} "
              f"pages={result.pages} chunks={result.chunks} "
              f"strategy={result.chunk_strategy} page_errors={result.page_errors} "
              f"tokens={result.total_tokens} empty={result.empty} "
              f"truncated={result.truncated}")
        if result.detail:
            print(f"  detail: {result.detail}")

    return 0 if result.status == "ready" else 1


if __name__ == "__main__":
    sys.exit(main())