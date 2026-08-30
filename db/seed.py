"""Day 3 - seed + vector search demo for the KnowFlow pgvector schema.

Pure stdlib (no DB driver): builds multi-tenant sample data with hand-crafted
384-dim vectors, pipes INSERT SQL to psql, then runs top-K vector search and
proves tenant isolation. Vectors cluster by topic (a band of dimensions gets
high weight + deterministic noise), so cosine distance ranks sensibly.

Topics and their dimension bands (the "meaning" behind each vector):
    canine  | dims  0-15 | dogs
    feline  | dims 16-31 | cats
    dessert | dims 32-47 | recipes / sweets
    finance | dims 48-63 | investing
    ai_ml   | dims 64-79 | machine learning

Run:  python3 db/seed.py
"""

import os
import subprocess
import uuid

HOST, PORT, USER, DB = "localhost", "5434", "knowflow", "knowflow"
MODEL = "all-MiniLM-L6-v2"
DIMS = 384

TOPICS = {
    "canine":  (0, 15, "dogs, treats, obedience"),
    "feline":  (16, 31, "cats, litter, grooming"),
    "dessert": (32, 47, "recipes, sugar, baking"),
    "finance": (48, 63, "stocks, portfolio, risk"),
    "ai_ml":   (64, 79, "models, training, data"),
}


def make_vector(topic: str, variant: int) -> str:
    """Deterministic 384-dim vector: 1.5 at the topic band + tiny noise."""
    lo, hi, _ = TOPICS[topic]
    values = []
    for i in range(DIMS):
        if lo <= i <= hi:
            values.append(round(1.5 + 0.01 * variant + 0.001 * ((i * 7 + variant) % 5), 4))
        else:
            values.append(round(0.01 + 0.001 * ((i * 13 + variant) % 7), 4))
    return "[" + ",".join(str(v) for v in values) + "]"


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
            ("Canine Care Guide", "canine", 3,
             ["Daily walks keep adult dogs healthy and calm.",
              "Reward training with small treats so obedience sticks.",
              "Schedule vet checkups every year for your dog."]),
            ("Dessert Recipes", "dessert", 3,
             ["Whisk sugar and eggs before adding melted butter.",
              "Bake until the edges are golden brown and set.",
              "Cool the cake on a rack to keep the texture light."]),
            ("Machine Learning Primer", "ai_ml", 2,
             ["Models learn from training data by minimizing error.",
              "Avoid overfitting by evaluating on held-out data."]),
        ],
        "globex": [
            ("Feline Health Handbook", "feline", 3,
             ["Cats groom themselves, so brushing is mostly about coat health.",
              "Keep the litter box clean to reduce stress in cats.",
              "Indoor cats benefit from toys that mimic prey movement."]),
            ("Investment Strategies 2026", "finance", 3,
             ["Diversify across sectors to reduce portfolio risk.",
              "Rebalance quarterly to lock in disciplined stock allocation.",
              "Long-term growth favors steady investing over timing."]),
            ("Canine Training Basics", "canine", 2,
             ["Crate training gives dogs a safe space to settle.",
              "Short daily sessions beat long rare lessons for dogs."]),
        ],
    }

    lines = ["TRUNCATE embeddings, chunks, documents, users, organizations CASCADE;"]
    variant = 0

    for slug, name in (("acme", "Acme Labs"), ("globex", "Globex Corp")):
        org_id = str(uuid.uuid4())
        lines.append(f"INSERT INTO organizations (id, name, slug) "
                     f"VALUES ('{org_id}', '{name}', '{slug}');")
        user_id = str(uuid.uuid4())
        lines.append(f"INSERT INTO users (id, organization_id, email, password_hash, role) "
                     f"VALUES ('{user_id}', '{org_id}', 'owner@{slug}.example', "
                     f"'bcrypt-placeholder', 'owner');")

        for title, topic, n_chunks, chunk_texts in docs[slug]:
            doc_id = str(uuid.uuid4())
            lines.append(f"INSERT INTO documents (id, organization_id, uploaded_by, title, "
                         f"source_type, status) VALUES ('{doc_id}', '{org_id}', '{user_id}', "
                         f"'{title}', 'md', 'ready');")
            for seq, content in enumerate(chunk_texts[:n_chunks]):
                chunk_id = str(uuid.uuid4())
                lines.append(f"INSERT INTO chunks (id, organization_id, document_id, seq, "
                             f"content, token_count) VALUES ('{chunk_id}', '{org_id}', '{doc_id}', "
                             f"{seq}, '{content}', {len(content.split())});")
                lines.append(f"INSERT INTO embeddings (organization_id, chunk_id, model, embedding) "
                             f"VALUES ('{org_id}', '{chunk_id}', '{MODEL}', "
                             f"'{make_vector(topic, variant)}'::vector);")
                variant += 1

    run_sql("\n".join(lines))


def top_k(org_label: str, org_name: str, qvec: str, k: int = 3, filtered: bool = True):
    note = "  (org filter REMOVED — cross-tenant leakage demo)" if not filtered else ""
    where = f"WHERE o.slug = '{org_label}'" if filtered else ""
    sql = f"""
SELECT d.title                                        AS document,
       c.seq                                          AS chunk,
       ROUND((1 - (e.embedding <=> '{qvec}'::vector))::numeric, 4) AS similarity
FROM embeddings e
JOIN chunks c       ON c.id = e.chunk_id
JOIN documents d    ON d.id = c.document_id
JOIN organizations o ON o.id = e.organization_id
{where}
ORDER BY e.embedding <=> '{qvec}'::vector
LIMIT {k};
"""
    print(f"\n  {org_name} — top-{k} by cosine similarity{note}")
    print(f"  sql filter: {where or '(none)'}")
    print(run_sql(sql))


def main():
    print("=" * 76)
    print("Day 3 - seed multi-tenant data + test vector search (pgvector)")
    print("=" * 76)

    seed()
    print("Seeded 2 organizations, 6 documents, 16 chunks, 16 embeddings (384-dim).")

    q_canine = make_vector("canine", 99)
    q_dessert = make_vector("dessert", 99)
    q_train = make_vector("canine", 0)

    print("\n" + "-" * 76)
    print("1. TENANT-FILTERED SEARCH - a 'dogs' query, asked inside Acme Labs")
    top_k("acme", "Acme Labs", q_canine)

    print("\n" + "-" * 76)
    print("2. SAME QUERY VECTOR, DIFFERENT TENANT - Globex sees only its own dog docs")
    top_k("globex", "Globex Corp", q_canine)

    print("\n" + "-" * 76)
    print("3. UNRELATED TOPIC - a 'dessert' query in Acme; finance docs do not leak in")
    top_k("acme", "Acme Labs", q_dessert)

    print("\n" + "-" * 76)
    print("4. SECURITY NOTE - the same Acme dog query WITHOUT the org filter")
    print("   returns Acme chunks to any caller. Tenant filtering is why this")
    print("   becomes a row-level-security policy on the hardening day.")
    top_k("acme", "Acme Labs", q_train, k=3, filtered=False)

    print("\n" + "-" * 76)
    print("5. INDEX BACKING - the ANN index pgvector uses for these queries")
    print(run_sql("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'embeddings';"))


if __name__ == "__main__":
    main()