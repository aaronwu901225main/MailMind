"""Local CPU embeddings and normalized float32 SQLite storage.

Batch model inference is OFFLINE ONLY. Webhook ingestion never invokes this module.
Keep SentenceTransformer optional until the user explicitly indexes vectors.
"""
from __future__ import annotations

import logging
from functools import lru_cache

import numpy as np
from sqlalchemy import delete, exists, select
from sqlalchemy.orm import Session

from app.config import EMBEDDING_MODEL
from app.models import KnowledgeChunk, KnowledgeEmbedding

logger = logging.getLogger(__name__)


@lru_cache(maxsize=2)
def load_embedder(model_name: str = EMBEDDING_MODEL, *, allow_download: bool = False):
    """Queries must use local files only; an offline batch job can explicitly download."""
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError('請先安裝 requirements-embeddings.txt') from exc
    return SentenceTransformer(model_name, device='cpu',
                               trust_remote_code=False, local_files_only=not allow_download)


def _normalized_vectors(embeddings, count: int) -> np.ndarray:
    values = np.asarray(embeddings, dtype=np.float32)
    if values.ndim != 2 or values.shape[0] != count or not values.shape[1]:
        raise ValueError('embedding dimensions mismatch')
    if not np.isfinite(values).all():
        raise ValueError('embedding has NaN/Inf')
    lengths = np.linalg.norm(values, axis=1, keepdims=True)
    if (lengths == 0).any():
        raise ValueError('embedding has zero vector')
    return np.ascontiguousarray(values / lengths, dtype=np.float32)


def encode_passages(embedder, texts: list[str]) -> np.ndarray:
    """Multilingual-E5 requires the passage prefix for documents."""
    if not texts:
        return np.empty((0, 0), dtype=np.float32)
    raw = embedder.encode(['passage: ' + t for t in texts],
                          batch_size=min(8, len(texts)), show_progress_bar=False)
    return _normalized_vectors(raw, len(texts))


def encode_query(embedder, query: str) -> np.ndarray:
    """Multilingual-E5 requires the query prefix for queries."""
    raw = embedder.encode(['query: ' + query], batch_size=1,
                          show_progress_bar=False)
    return _normalized_vectors(raw, 1)[0]


def delete_orphan_embeddings(session: Session) -> int:
    """Remove vectors whose knowledge_chunks were replaced by source reindexing."""
    stmt = delete(KnowledgeEmbedding).where(~exists(
        select(KnowledgeChunk.id).where(KnowledgeChunk.id == KnowledgeEmbedding.chunk_id)
    ))
    return session.execute(stmt).rowcount


def pending_chunks(session: Session, account_id: str,
                   model_name: str = EMBEDDING_MODEL, limit: int = 8) -> list[KnowledgeChunk]:
    """Select rows absent/stale for the requested model. Account filter is mandatory."""
    e = KnowledgeEmbedding
    k = KnowledgeChunk
    stmt = (select(k).outerjoin(e, e.chunk_id == k.id)
            .where(k.account_id == account_id)
            .where((e.chunk_id.is_(None)) |
                   (e.content_hash != k.content_hash) |
                   (e.model_name != model_name))
            .order_by(k.id).limit(min(max(int(limit), 1), 128)))
    return list(session.scalars(stmt).all())


def store_chunk_embeddings(session: Session, chunks: list[KnowledgeChunk],
                           embedder, model_name: str = EMBEDDING_MODEL) -> int:
    """Update one account-scoped batch atomically, with content hash freshness checks."""
    if not chunks:
        return 0
    if len({c.account_id for c in chunks}) != 1:
        raise ValueError('multiple accounts in embedding batch')
    vectors = encode_passages(embedder, [c.chunk_text for c in chunks])
    for chunk, vector in zip(chunks, vectors):
        if chunk.id is None:
            raise ValueError('unsaved KnowledgeChunk')
        existing = session.get(KnowledgeEmbedding, chunk.id)
        if existing is None:
            existing = KnowledgeEmbedding(chunk_id=chunk.id, model_name=model_name,
                                          content_hash=chunk.content_hash,
                                          dimension=int(vector.shape[0]), vector=vector.tobytes())
            session.add(existing)
        else:
            existing.model_name = model_name
            existing.content_hash = chunk.content_hash
            existing.dimension = int(vector.shape[0])
            existing.vector = vector.tobytes()
    session.flush()
    return len(chunks)
