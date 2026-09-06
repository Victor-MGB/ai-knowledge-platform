"""Manual runner for the Day-11 embedding phase, for ops and backfills.

Usage (from ai-service/, with the venv active):
    python -m app.embedding.cli --document-id <uuid> [--json]

Mirrors POST /v1/embed/documents/{id} without needing the server up.
"""

import argparse
import json
import sys


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run KnowFlow embedding (chunk -> vectors via the configured model) for one document"
    )
    parser.add_argument("--document-id", required=True, help="document id (uuid)")
    parser.add_argument("--json", action="store_true", help="emit JSON, exit 0 on embedded")
    args = parser.parse_args()

    from ..core.config import get_settings
    from .service import build_embedding_pipeline

    result = build_embedding_pipeline(get_settings()).embed_document(args.document_id)

    if args.json:
        print(json.dumps(result.model_dump(), indent=2))
    else:
        print(
            f"[{result.status}] document={result.document_id} "
            f"chunks={result.chunks} vectors={result.vectors} "
            f"model={result.model} dimensions={result.dimensions}"
        )
        if result.detail:
            print(f"  detail: {result.detail}")

    return 0 if result.status == "embedded" else 1


if __name__ == "__main__":
    sys.exit(main())