"""SQLite FTS5 trigram + safe account-scoped keyword fallback.

This is lexical retrieval (v0.5.1), not vector semantic retrieval yet.
"""
from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


DDL = [
    """CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
        chunk_text, content='knowledge_chunks', content_rowid='id', tokenize='trigram'
    )""",
    """CREATE TRIGGER IF NOT EXISTS knowledge_chunks_ai AFTER INSERT ON knowledge_chunks BEGIN
        INSERT INTO knowledge_fts(rowid, chunk_text) VALUES(new.id, new.chunk_text);
    END""",
    """CREATE TRIGGER IF NOT EXISTS knowledge_chunks_ad AFTER DELETE ON knowledge_chunks BEGIN
        INSERT INTO knowledge_fts(knowledge_fts, rowid, chunk_text)
        VALUES('delete', old.id, old.chunk_text);
    END""",
    """CREATE TRIGGER IF NOT EXISTS knowledge_chunks_au AFTER UPDATE OF chunk_text ON knowledge_chunks BEGIN
        INSERT INTO knowledge_fts(knowledge_fts, rowid, chunk_text)
        VALUES('delete', old.id, old.chunk_text);
        INSERT INTO knowledge_fts(rowid, chunk_text) VALUES(new.id, new.chunk_text);
    END""",
]


def ensure_knowledge_fts(engine) -> bool:
    """Create FTS + triggers without deleting existing data. Fail open to SQL fallback.

    The `knowledge_chunks` ORM table must already exist (init_database/create_all).
    """
    if engine.dialect.name != 'sqlite':
        logger.warning('Knowledge FTS is supported only on SQLite')
        return False
    try:
        with engine.begin() as conn:
            existed = bool(conn.exec_driver_sql(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_fts'"
            ).first())
            for command in DDL:
                conn.exec_driver_sql(command)
            if not existed:
                # Handles a DB where knowledge_chunks was populated before FTS installation.
                conn.exec_driver_sql("INSERT INTO knowledge_fts(knowledge_fts) VALUES('rebuild')")
        return True
    except DatabaseError:
        logger.exception('FTS5 trigram unavailable; fallback to substring lookup')
        return False


FTS_SQL = text("""
SELECT k.id AS chunk_id, k.email_id, k.source_type, k.source_id,
       k.chunk_index, k.chunk_text, e.subject, e.sender,
       a.filename, bm25(knowledge_fts) AS score
FROM knowledge_fts
JOIN knowledge_chunks AS k ON k.id = knowledge_fts.rowid
JOIN emails AS e ON e.id = k.email_id AND e.account_id = k.account_id
LEFT JOIN email_attachments AS a
       ON k.source_type = 'attachment' AND a.id = k.source_id
WHERE knowledge_fts MATCH :query AND k.account_id = :account_id
ORDER BY score ASC, e.created_at DESC
LIMIT :max_results
""")

FALLBACK_SQL = text("""
SELECT k.id AS chunk_id, k.email_id, k.source_type, k.source_id,
       k.chunk_index, k.chunk_text, e.subject, e.sender,
       a.filename, 0.0 AS score
FROM knowledge_chunks AS k
JOIN emails AS e ON e.id = k.email_id AND e.account_id = k.account_id
LEFT JOIN email_attachments AS a
       ON k.source_type = 'attachment' AND a.id = k.source_id
WHERE k.account_id = :account_id
  AND instr(lower(k.chunk_text), lower(:query)) > 0
ORDER BY e.created_at DESC, k.id DESC
LIMIT :max_results
""")

LATEST_SQL = text("""
SELECT k.id AS chunk_id, k.email_id, k.source_type, k.source_id,
       k.chunk_index, k.chunk_text, e.subject, e.sender,
       a.filename, 0.0 AS score
FROM knowledge_chunks AS k
JOIN emails AS e ON e.id = k.email_id AND e.account_id = k.account_id
LEFT JOIN email_attachments AS a
       ON k.source_type = 'attachment' AND a.id = k.source_id
WHERE k.account_id = :account_id
ORDER BY e.created_at DESC, k.id DESC
LIMIT :max_results
""")


def search_chunks(session: Session, account_id: str, keyword: str, limit: int = 10) -> list[dict]:
    """Never accept untrusted account_id from an LLM tool call (MCP wrapper scopes it)."""
    keyword = (keyword or '').strip()[:200]
    limit = max(1, min(int(limit), 20))
    args = {'account_id': account_id, 'query': keyword, 'max_results': limit}
    rows = []
    if not keyword:
        rows = session.execute(LATEST_SQL, args).mappings().all()
    elif len(keyword) >= 3:
        # Quoted FTS phrase: punctuation, double-quotes and operator words are data.
        expression = '"' + keyword.replace('"', '""') + '"'
        try:
            rows = session.execute(FTS_SQL, dict(args, query=expression)).mappings().all()
        except DatabaseError:
            # A failed statement can invalidate a transaction in some DB backends;
            # using SQLite here allows another read. Separate operation on fallback.
            logger.warning('FTS5 query failed; falling back to substring scan', exc_info=True)
    if keyword and not rows:
        rows = session.execute(FALLBACK_SQL, args).mappings().all()
    return [
        {
            'chunk_id': row['chunk_id'],
            'email_id': row['email_id'],
            'subject': row['subject'],
            'sender': row['sender'],
            'source_type': row['source_type'],
            'source_id': row['source_id'],
            'filename': row['filename'],
            'chunk_index': row['chunk_index'],
            'snippet': row['chunk_text'][:600],
            'score': float(row['score']),
        }
        for row in rows
    ]
