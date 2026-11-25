"""Bounded, account-scoped cosine search + BM25/semantic reciprocal rank fusion.

v0.5.2 intentionally uses exact NumPy search for small local corpora. Query
embedding is opt-in and cached locally; ingestion NEVER calls embeddings.
"""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import EMBEDDING_MODEL, VECTOR_SEARCH_CANDIDATES
from app.services.knowledge_search import search_chunks

logger = logging.getLogger(__name__)

VECTOR_SQL = text("""
SELECT k.id AS chunk_id, k.email_id, k.source_type, k.source_id,
       k.chunk_index, k.chunk_text, e.subject, e.sender,
       a.filename, v.vector, v.dimension
FROM knowledge_embeddings AS v
JOIN knowledge_chunks AS k
  ON k.id = v.chunk_id AND k.content_hash = v.content_hash
JOIN emails AS e ON e.id = k.email_id AND e.account_id = k.account_id
LEFT JOIN email_attachments AS a
  ON k.source_type = 'attachment' AND a.id = k.source_id
WHERE k.account_id = :account_id AND v.model_name = :model_name
ORDER BY k.id DESC
LIMIT :candidate_limit
""")


def vector_search(session: Session, account_id: str, query: str, *,
                  limit: int = 10, embedder=None,
                  model_name: str = EMBEDDING_MODEL,
                  candidate_limit: int = VECTOR_SEARCH_CANDIDATES) -> list[dict]:
    """Brute-force normalized dot product, scoped in SQL before vector ranking.

    At most 5000 indexed chunks are considered; this is *not* an ANN index.
    """
    if not account_id or not (query or '').strip():
        return []
    limit = max(1, min(int(limit), 20))
    candidate_limit = max(limit, min(int(candidate_limit), 5000))
    rows = session.execute(VECTOR_SQL, {
        'account_id': account_id, 'model_name': model_name,
        'candidate_limit': candidate_limit,
    }).mappings().all()
    if not rows:
        return []  # Don't load a model when the account has no vectors.
    import numpy as np
    from app.services.embedding_store import encode_query, load_embedder
    if embedder is None:
        embedder = load_embedder(model_name, allow_download=False)
    q = encode_query(embedder, query.strip()[:500])
    matching = []
    for row in rows:
        if row['dimension'] != q.size or len(row['vector']) != q.size * 4:
            logger.warning('Ignoring invalid vector for chunk %s', row['chunk_id'])
            continue
        v = np.frombuffer(row['vector'], dtype='<f4')
        if not np.isfinite(v).all():
            continue
        cosine = float(np.dot(q, v))
        result = {
            'chunk_id': row['chunk_id'], 'email_id': row['email_id'],
            'subject': row['subject'], 'sender': row['sender'],
            'source_type': row['source_type'], 'source_id': row['source_id'],
            'filename': row['filename'], 'chunk_index': row['chunk_index'],
            'snippet': row['chunk_text'][:600], 'score': cosine,
        }
        matching.append(result)
    matching.sort(key=lambda r: (-r['score'], r['chunk_id']))
    return matching[:limit]


def hybrid_search(session: Session, account_id: str, query: str, *,
                  limit: int = 10, use_semantic: bool = False,
                  embedder=None, model_name: str = EMBEDDING_MODEL,
                  rrf_k: int = 60) -> list[dict]:
    """Fuse account-scoped lexical + semantic rankings. Graceful lexical fallback.

    Ranked by RRF, not raw cosine + BM25 (which have incompatible scales).
    """
    query = (query or '').strip()[:500]
    if not query:
        return []
    limit = max(1, min(int(limit), 20))
    depth = min(20, max(limit * 3, 10))
    lexical = search_chunks(session, account_id, query, depth)
    semantic = []
    if use_semantic:
        try:
            semantic = vector_search(session, account_id, query, limit=depth,
                                     embedder=embedder, model_name=model_name)
        except (RuntimeError, OSError, ImportError, ValueError):
            logger.warning('Semantic search unavailable; using lexical only', exc_info=True)
    if not semantic:
        return [{**row, 'retrieval_modes': ['fts5'], 'rrf_score': 1.0 / (rrf_k + rank)}
                for rank, row in enumerate(lexical, 1)][:limit]

    combined = {}
    for mode, hits in [('fts5', lexical), ('vector', semantic)]:
        for rank, hit in enumerate(hits, 1):
            key = hit['chunk_id']
            if key not in combined:
                combined[key] = {**hit, 'retrieval_modes': [], 'rrf_score': 0.0}
            combined[key]['retrieval_modes'].append(mode)
            combined[key]['rrf_score'] += 1.0 / (rrf_k + rank)
            # Preserve vector cosine as a separate diagnostic, not the combined score.
            if mode == 'vector':
                combined[key]['vector_cosine'] = hit['score']
    ranked = sorted(combined.values(), key=lambda row: (-row['rrf_score'], row['chunk_id']))
    return ranked[:limit]
