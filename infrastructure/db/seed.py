"""Day 10 - reset dev tenant data for the KnowFlow schema.

Pure stdlib (no DB driver): builds multi-tenant sample data, pipes INSERT SQL
to psql, and prints what exists. The Day-3 vector-search demo is gone: the
hand-seeded `chunks`/`embeddings` skeleton was dropped in migration 006, and
chunking now flows through the real pipeline (backend upload -> ai-service
PDF extraction -> chunking), so emulated vectors have nothing to vend.

Run:  python3 db/seed.py
"""

import os
import subprocess
import uuid

HOST, PORT, USER, DB = "localhost", "5434", "knowflow", "knowflow"


def run_sql(sql: str) -> str:
    env = dict(os.environ, PGPASSWORD="knowflow")
    result = subprocess.run(
        ["psql", "-h", HOST, "-p", PORT, "-U", USER, "-d", DB, "-c", sql],
        capture_output=True, text=True, env=env,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr)
    return result.stdout


def seed() -> None:
    docs = {
        "acme": [
            "Canine Care Guide",
            "Dessert Recipes",
            "Machine Learning Primer",
        ],
        "globex": [
            "Feline Health Handbook",
            "Investment Strategies 2026",
            "Canine Training Basics",
        ],
    }

    lines = ["TRUNCATE organizations CASCADE;"]  # users/documents/pages/chunks cascade
    for slug, name in (("acme", "Acme Labs"), ("globex", "Globex Corp")):
        org_id = str(uuid.uuid4())
        lines.append(f"INSERT INTO organizations (id, name, slug) "
                     f"VALUES ('{org_id}', '{name}', '{slug}');")
        user_id = str(uuid.uuid4())
        lines.append(f"INSERT INTO users (id, organization_id, email, password_hash, role) "
                     f"VALUES ('{user_id}', '{org_id}', 'owner@{slug}.example', "
                     f"'bcrypt-placeholder', 'owner');")
        for title in docs[slug]:
            lines.append(f"INSERT INTO documents (id, organization_id, uploaded_by, title, "
                         f"source_type, status) VALUES ('{uuid.uuid4()}', '{org_id}', '{user_id}', "
                         f"'{title}', 'md', 'ready');")

    run_sql("\n".join(lines))


def main():
    print("=" * 76)
    print("Day 10 - reset dev tenant data (organizations + users + documents)")
    print("=" * 76)

    seed()
    print("Seeded 2 organizations, 2 users, 6 documents.")
    print("\nNext step for a doc is the real pipeline, not hand-seeded text:")
    print("  upload via POST /api/v1/documents, then")
    print("  POST /v1/process/documents/{id} -> document_pages + chunks.")
    print(run_sql("SELECT d.id, d.title, d.status, "
                  "       (SELECT count(*) FROM document_pages p WHERE p.document_id = d.id) AS pages, "
                  "       (SELECT count(*) FROM chunks c WHERE c.document_id = d.id) AS chunks "
                  "  FROM documents d ORDER BY d.title;"))


if __name__ == "__main__":
    main()