"""Reverse search: match a plain-English business problem to technologies using embeddings.

Embeddings come from a small open-source model (BAAI/bge-small-en-v1.5) that runs
locally via fastembed, so semantic search needs no API key and costs nothing.
"""

import json

import numpy as np

from . import config

BATCH_SIZE = 64


class Embedder:
    """Wraps the local embedding model. Loaded lazily: the first use downloads ~100MB once."""

    def __init__(self, model_name: str | None = None):
        self.model_name = model_name or config.EMBEDDING_MODEL
        self._model = None

    @property
    def model(self):
        if self._model is None:
            from fastembed import TextEmbedding
            self._model = TextEmbedding(self.model_name)
        return self._model

    def documents(self, texts: list[str]) -> np.ndarray:
        return _normalise(np.array(list(self.model.embed(texts)), dtype=np.float32))

    def query(self, text: str) -> np.ndarray:
        # BGE models embed search queries slightly differently from documents.
        return _normalise(np.array(list(self.model.query_embed([text])), dtype=np.float32))[0]


def _normalise(vectors: np.ndarray) -> np.ndarray:
    # Unit-length vectors make a plain dot product equal to cosine similarity.
    return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)


_default_embedder: Embedder | None = None


def default_embedder() -> Embedder:
    global _default_embedder
    if _default_embedder is None:
        _default_embedder = Embedder()
    return _default_embedder


def embedding_text(tech: dict) -> str:
    """The text we embed for each technology: the business framing first, then the science."""
    apps = tech.get("applications") or []
    if isinstance(apps, str):
        apps = json.loads(apps)
    parts = [
        tech["title"],
        tech.get("summary") or "",
        "Applications: " + "; ".join(apps) if apps else "",
        (tech.get("abstract") or "")[:2000],
    ]
    return "\n".join(p for p in parts if p)


def embed_pending(conn, embedder: Embedder | None = None, force: bool = False, log=print) -> int:
    """Compute embeddings for technologies that don't have one yet."""
    embedder = embedder or default_embedder()
    sql = "SELECT * FROM technologies" + ("" if force else " WHERE embedding IS NULL")
    rows = [dict(r) for r in conn.execute(sql)]
    log(f"Embedding {len(rows)} technologies with {embedder.model_name}")

    for start in range(0, len(rows), BATCH_SIZE):
        batch = rows[start:start + BATCH_SIZE]
        vectors = embedder.documents([embedding_text(t) for t in batch])
        conn.executemany(
            "UPDATE technologies SET embedding=? WHERE id=?",
            [(v.tobytes(), t["id"]) for v, t in zip(vectors, batch)],
        )
        conn.commit()
        log(f"  embedded {start + len(batch)}/{len(rows)}")
    return len(rows)


class VectorIndex:
    """All technology embeddings held in memory as one matrix for fast cosine search."""

    def __init__(self, conn):
        rows = conn.execute("SELECT id, embedding FROM technologies WHERE embedding IS NOT NULL").fetchall()
        self.ids = [r["id"] for r in rows]
        self.matrix = (np.vstack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
                       if rows else np.empty((0, 0), dtype=np.float32))

    def __len__(self):
        return len(self.ids)

    def search(self, query_vector: np.ndarray, k: int = 10) -> list[tuple[int, float]]:
        if not self.ids:
            return []
        scores = self.matrix @ query_vector
        top = np.argsort(-scores)[:k]
        return [(self.ids[i], float(scores[i])) for i in top]
