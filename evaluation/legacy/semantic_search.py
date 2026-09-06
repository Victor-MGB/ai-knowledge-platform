"""Day 2 - Embeddings + Vector Search.

A zero-dependency experiment (pure stdlib) that teaches the mechanics of
semantic search end-to-end:

    1. EMBEDDING GENERATION
       text -> fixed-length vector of floats. Here we use TF-IDF vectors:
       term frequency x inverse document frequency. Dead words (water, the)
       weigh little; rare words (zebra, plumber) weigh a lot.

    2. VECTOR DIMENSIONS
       len(document vector). In TF-IDF the dimension count == vocabulary
       size. Real embedding models give fixed dense vectors instead
       (e.g. all-MiniLM = 384 dims, text-embedding-3-small = 1536). The
       geometry is identical; only the vectorizer changes.

    3. COSINE SIMILARITY
       cos of the angle between two vectors -> [-1, 1]. 1 = same direction
       => same meaning. Invariant to magnitudes, so it works well with
       TF-IDF counts.

    4. EUCLIDEAN DISTANCE
       straight-line distance between vector tips. Smaller = more similar.
       On unit (normalized) vectors, ordering by euclidean distance agrees
       with ordering by cosine similarity.

    5. SIMILARITY SEARCH
       embed the query the same way, score it against every stored vector.

    6. TOP-K RETRIEVAL
       sort all scores and keep the best K documents.

The catch (shown in the demo): TF-IDF only matches exact words, so "large
flightless birds" misses "penguins". Real embedding models bridge that gap
because they map *meaning* to nearby points. Same search code, richer
vectors - which is exactly what the full KnowFlow platform will use.
"""

import json
import math
import re
from pathlib import Path

TOKEN_RE = re.compile(r"[a-z0-9']+")


def tokenize(text: str) -> list[str]:
    """Lowercase a string and split it into word tokens."""
    return TOKEN_RE.findall(text.lower())


def build_vocabulary(texts: list[str]) -> list[str]:
    """All unique tokens across a corpus. Size == vector dimensions here."""
    vocab: set[str] = set()
    for text in texts:
        vocab.update(tokenize(text))
    return sorted(vocab)


def term_frequency(tokens: list[str]) -> dict[str, int]:
    """Count of each token within one document."""
    tf: dict[str, int] = {}
    for token in tokens:
        tf[token] = tf.get(token, 0) + 1
    return tf


def inverse_document_frequency(texts: list[str]) -> dict[str, float]:
    """Rarity of each token across the corpus: log(N / (1 + df))."""
    document_frequency: dict[str, int] = {}
    for text in texts:
        for token in set(tokenize(text)):
            document_frequency[token] = document_frequency.get(token, 0) + 1
    n = len(texts)
    return {token: math.log(n / (1 + df)) for token, df in document_frequency.items()}


def embed_document(text: str, vocab: list[str], idf: dict[str, float]) -> list[float]:
    """Generate a TF-IDF vector for a document, aligned to the vocabulary."""
    token_index = {token: i for i, token in enumerate(vocab)}
    vector = [0.0] * len(vocab)
    for token, count in term_frequency(tokenize(text)).items():
        if token in token_index and token in idf:
            vector[token_index[token]] = count * idf[token]
    return vector


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine of the angle between vectors a and b: dot(a, b) / (|a||b|)."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def euclidean_distance(a: list[float], b: list[float]) -> float:
    """Straight-line distance between the tips of vectors a and b."""
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def l2_normalize(vec: list[float]) -> list[float]:
    """Scale a vector to unit length, so cosine and euclidean agree."""
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


class VectorStore:
    """Stores document vectors in memory and answers top-K queries."""

    def __init__(self, vocab: list[str], idf: dict[str, float]):
        self.vocab = vocab
        self.idf = idf
        self.documents: list[dict] = []

    @property
    def dimensions(self) -> int:
        return len(self.vocab)

    def add(self, doc_id: str, text: str, normalize: bool = False) -> None:
        vector = embed_document(text, self.vocab, self.idf)
        if normalize:
            vector = l2_normalize(vector)
        self.documents.append({"doc_id": doc_id, "text": text, "vector": vector})

    def search(self, query: str, k: int = 3, metric: str = "cosine"):
        """Embed the query and return the top-k matches.

        metric="cosine":    higher score = more similar
        metric="euclidean": lower score  = more similar
        """
        query_vector = embed_document(query, self.vocab, self.idf)
        if metric == "cosine":
            ranked = sorted(
                ((cosine_similarity(query_vector, d["vector"]), d) for d in self.documents),
                key=lambda r: r[0],
                reverse=True,
            )
        elif metric == "euclidean":
            ranked = sorted(
                ((euclidean_distance(query_vector, d["vector"]), d) for d in self.documents),
                key=lambda r: r[0],
            )
        else:
            raise ValueError(f"unknown metric: {metric}")
        return [(round(score, 4), d["doc_id"], d["text"]) for score, d in ranked[:k]]

    def save(self, path: str | Path) -> None:
        """Persist vectors to disk as JSON (production: pgvector/csv)."""
        payload = {
            "vocab": self.vocab,
            "idf": self.idf,
            "documents": self.documents,
        }
        Path(path).write_text(json.dumps(payload), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "VectorStore":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        store = cls(payload["vocab"], payload["idf"])
        store.documents = payload["documents"]
        return store


def _print_ranked(title: str, ranked) -> None:
    print(f"\n  {title}")
    for score, doc_id, text in ranked:
        print(f"    {score:>8.4f}  [{doc_id}] {text}")


def demo() -> None:
    print("=" * 72)
    print("Day 2 - Embeddings + Vector Search (pure stdlib, no installs)")
    print("=" * 72)

    corpus = {
        "penguin": "penguins live on the Antarctic ice and are champion swimmers",
        "zebra": "a zebra has black and white stripes",
        "plumbing": "to fix a leaking pipe turn off the water valve first",
        "giraffe": "giraffes are the tallest animals and eat leaves from trees",
        "lemonade": "lemonade is made from lemons sugar and water",
        "polar": "polar bears hunt seals on sea ice",
        "plumber": "when the water heater leaks call a plumber to repair it",
        "monkey": "monkeys can be found in tropical rainforests",
    }

    texts = list(corpus.values())
    vocab = build_vocabulary(texts)
    idf = inverse_document_frequency(texts)

    store = VectorStore(vocab, idf)
    for doc_id, text in corpus.items():
        store.add(doc_id, text)

    print(f"\nCorpus: {len(corpus)} documents -> vocabulary of {store.dimensions} words.")
    print(f"-> Our TF-IDF vectors have {store.dimensions} DIMENSIONS (one per vocab word).")
    print(f'-> Vocabulary (first 15): {vocab[:15]}')

    first = store.documents[0]
    nonzero = [(idx, val) for idx, val in enumerate(first["vector"]) if val != 0.0]
    print(f"\nEmbedding of '{first['doc_id']}' = {len(nonzero)} nonzero of"
          f" {store.dimensions} features:")
    print(f"  {[round(v, 3) for _, v in nonzero]}")

    print("\n" + "-" * 72)
    print("QUERY 1: overlap matches - both plumbing documents should surface.")
    q1 = "water pipe is leaking and i need a plumber"
    print(f'  query: "{q1}"')
    _print_ranked("cosine top-3 (higher = more similar):", store.search(q1, k=3))
    _print_ranked("euclidean top-3 (lower = more similar):", store.search(q1, k=3, metric="euclidean"))
    print("  note: euclidean compares magnitude too, so lemonade's SHORT vector")
    print("  sharing one common word ('water') ranks closer to the query. Cosine")
    print("  ignores magnitude and matches intent better - why cosine is standard.")

    print("\n" + "-" * 72)
    print("QUERY 2: word overlap again (black + white + stripes).")
    q2 = "what animal has black and white stripes"
    print(f'  query: "{q2}"')
    _print_ranked("cosine top-3:", store.search(q2, k=3))

    print("\n" + "-" * 72)
    print("QUERY 3: the synonym trap - no word overlaps at all.")
    q3 = "large flightless birds"
    print(f'  query: "{q3}"')
    print("  (corpus has no 'large'/'flightless'/'birds' wording -> tf-idf must fail)")
    _print_ranked("cosine top-3 (all ~0.0 = TF-IDF cannot bridge the gap):", store.search(q3, k=3))
    print("  -> TF-IDF only matches exact tokens. Real embedding models map")
    print("     *meaning* to nearby vectors, so 'flightless birds' finds penguins.")
    print("     The search math stays identical - only the vectorizer changes.")

    print("\n" + "-" * 72)
    print("NORMALIZED VECTORS: cosine ordering == euclidean ordering.")
    normalized = VectorStore(vocab, idf)
    for doc_id, text in corpus.items():
        normalized.add(doc_id, text, normalize=True)
    _print_ranked("cosine top-3 (normalized):", normalized.search(q1, k=3))
    _print_ranked("euclidean top-3 (normalized):", normalized.search(q1, k=3, metric="euclidean"))
    print("  -> on unit vectors, angle and distance rank identically.")

    print("\n" + "-" * 72)
    print("PERSISTENCE: vectors saved to disk, reloaded, search works after.")
    path = Path("vectors.json")
    store.save(path)
    reloaded = VectorStore.load(path)
    _print_ranked("search on the reloaded store:", reloaded.search(q1, k=2))
    print(f"  -> saved {len(reloaded.documents)} vectors to {path}")

    print("\nDone. Vectors in a real system would live in pgvector; this is the")
    print("exact scoring pipeline every retrieval layer uses.")


if __name__ == "__main__":
    demo()