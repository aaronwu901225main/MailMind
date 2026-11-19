"""MailMind v0.5.1: offline indexing and incremental updates.

The FTS index is on the same SQLite database and is maintained by triggers.
New incoming messages preserve email body text as bounded knowledge chunks.
Historic messages can only be backfilled from v0.4's saved metadata, summaries,
and attachment extracted text (the old version did not persist email bodies).
"""
from __future__ import annotations

import hashlib
import logging

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import EmailAttachment, EmailMessage, KnowledgeChunk, Summary

logger = logging.getLogger(__name__)
CHUNK_SIZE = 900
CHUNK_OVERLAP = 120


def split_text(content: str, max_chars: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Bounded deterministic chunks; prefer breaking near paragraph boundaries."""
    content = (content or '').strip()
    if not content:
        return []
    if not 0 <= overlap < max_chars:
        raise ValueError('overlap must be smaller than chunk length')
    result = []
    start = 0
    while start < len(content):
        end = min(start + max_chars, len(content))
        if end < len(content):
            boundary = max(content.rfind('\n', start + max_chars // 2, end),
                           content.rfind(' ', start + max_chars // 2, end))
            if boundary > start + max_chars // 2:
                end = boundary + 1
        piece = content[start:end].strip()
        if piece:
            result.append(piece)
        if end == len(content):
            break
        start = max(start + 1, end - overlap)
    return result


def _sync_source(session: Session, email: EmailMessage, source_type: str,
                 source_id: str, content: str) -> int:
    """Idempotent replacement for one source; leaves other sources unchanged."""
    chunks = split_text(content)
    digests = [hashlib.sha256(part.encode('utf-8')).hexdigest() for part in chunks]
    existing = session.scalars(
        select(KnowledgeChunk).where(
            KnowledgeChunk.email_id == email.id,
            KnowledgeChunk.account_id == email.account_id,
            KnowledgeChunk.source_type == source_type,
            KnowledgeChunk.source_id == source_id,
        ).order_by(KnowledgeChunk.chunk_index)
    ).all()
    if [row.content_hash for row in existing] == digests:
        return 0
    session.execute(delete(KnowledgeChunk).where(
        KnowledgeChunk.email_id == email.id,
        KnowledgeChunk.account_id == email.account_id,
        KnowledgeChunk.source_type == source_type,
        KnowledgeChunk.source_id == source_id,
    ))
    for index, (part, digest) in enumerate(zip(chunks, digests)):
        session.add(KnowledgeChunk(
            account_id=email.account_id, email_id=email.id,
            source_type=source_type, source_id=source_id, chunk_index=index,
            chunk_text=part, content_hash=digest,
        ))
    return len(chunks)


def index_email(session: Session, email: EmailMessage, body: str | None = None) -> int:
    """Index stored sources. `body=None` preserves previously indexed body text."""
    count = 0
    meta = f'主旨：{email.subject}\n寄件者：{email.sender}'
    count += _sync_source(session, email, 'metadata', email.id, meta)
    summary = session.scalar(select(Summary).where(Summary.email_id == email.id))
    if summary:
        count += _sync_source(session, email, 'summary', email.id, summary.content)
    if body is not None:
        count += _sync_source(session, email, 'body', email.id, body[:12000])
    attachments = session.scalars(select(EmailAttachment)
                                  .where(EmailAttachment.email_id == email.id)
                                  .order_by(EmailAttachment.position)).all()
    for attachment in attachments:
        if attachment.extracted_text:
            count += _sync_source(
                session, email, 'attachment', attachment.id,
                f'附件：{attachment.filename}\n{attachment.extracted_text}',
            )
    return count
