"""Offline indexing of source-backed email events; never called from webhooks.

Rerunning with the same extractor/source hash/mode is idempotent. An existing
email run is not silently overwritten, preserving the audit trail.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from sqlalchemy import select, delete
from sqlalchemy.orm import Session

from app.models import EmailMessage, EventExtractionRun, KnowledgeChunk, StructuredEvent
from app.services.event_extraction import extract_document, validate_source_anchor
from app.services.event_segmentation import reconstruct
from app.services.event_schema import EXTRACTOR_VERSION, SCHEMA_VERSION, validate_event


def _raw_chunks(db: Session, account_id: str, email_id: str) -> list[KnowledgeChunk]:
    return db.scalars(select(KnowledgeChunk).where(
        KnowledgeChunk.account_id == account_id,
        KnowledgeChunk.email_id == email_id,
        KnowledgeChunk.source_type.in_(['body', 'attachment']),
    ).order_by(KnowledgeChunk.source_type, KnowledgeChunk.source_id,
               KnowledgeChunk.chunk_index)).all()


def _source_digest(chunks: list[KnowledgeChunk]) -> str:
    h = hashlib.sha256()
    for c in chunks:
        h.update(f'{c.source_type}:{c.source_id}:{c.chunk_index}:{c.content_hash}\n'.encode())
    return h.hexdigest()


def _fingerprint(rec: dict) -> str:
    """Unique *occurrence* fingerprint, not a cross-email content hash.

    Two identical transactions in one email at distinct offsets are distinct.
    """
    key = (rec['event_type'], rec['source_id'], rec['source_chunk_id'],
           rec['span_start'], rec['span_end'])
    return hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()


def index_one_email(db: Session, account_id: str, email_id: str, *,
                    mode: str = 'local', allow_cloud: bool = False, generate=None) -> dict:
    """Commit an audited run and events atomically. No independent manual SQL.

    mode=hybrid tries the local rules first and Gemini on chunks without a
    verified event. The operator must opt into cloud use twice (CLI + env).
    """
    if mode not in {'local', 'hybrid'}:
        raise ValueError('unsupported_extraction_mode')
    if mode == 'hybrid' and not allow_cloud:
        raise ValueError('cloud_extraction_requires_explicit_consent')
    mail = db.scalar(select(EmailMessage).where(
        EmailMessage.account_id == account_id, EmailMessage.id == email_id))
    if not mail:
        raise ValueError('email_not_found_or_forbidden')
    chunks = _raw_chunks(db, account_id, email_id)
    digest = _source_digest(chunks)
    latest = db.scalar(select(EventExtractionRun).where(
        EventExtractionRun.account_id == account_id,
        EventExtractionRun.email_id == email_id,
    ).order_by(EventExtractionRun.created_at.desc(), EventExtractionRun.id.desc()))
    previous = db.scalar(select(EventExtractionRun).where(
        EventExtractionRun.account_id == account_id,
        EventExtractionRun.email_id == email_id,
        EventExtractionRun.extractor_version == EXTRACTOR_VERSION,
        EventExtractionRun.source_digest == digest,
        EventExtractionRun.mode == mode,
    ))
    if previous and latest and previous.id == latest.id:
        return {'email_id': email_id, 'status': 'skipped',
                'verified': previous.verified_count, 'review': previous.review_count}

    # Do not take financial values from previous Gemini summaries. They can be
    # truncated and are not a complete transaction ledger.
    if not chunks:
        db.execute(delete(StructuredEvent).where(StructuredEvent.account_id == account_id,
                                                  StructuredEvent.email_id == email_id))
        run = previous or EventExtractionRun(account_id=account_id, email_id=email_id,
            extractor_version=EXTRACTOR_VERSION, source_digest=digest, mode=mode)
        run.created_at = datetime.now(timezone.utc)
        run.status = 'no_source'
        run.reason_code = 'original_body_unavailable'
        run.verified_count = run.review_count = 0
        db.add(run)
        db.commit()
        return {'email_id': email_id, 'status': 'no_source', 'verified': 0, 'review': 0}

    # Segment *whole source documents*, not arbitrary overlapping FTS chunks.
    # A message may contain multiple separate transactions; an attachment is
    # a separate source, never blended with the body.
    candidates = []
    by_id = {c.id: c for c in chunks}
    source_groups = {}
    for c in chunks:
        source_groups.setdefault((c.source_type, c.source_id), []).append(c)
    documents = {key: reconstruct(group) for key, group in source_groups.items()}
    for group in source_groups.values():
        candidates.extend(extract_document(group))
    if mode == 'hybrid':
        from app.services.event_cloud import extract_gemini
        verified_sources = {(r['source_type'], r['source_id']) for r in candidates
                            if r['validation_status'] == 'verified'}
        fallbacks = [c for c in chunks
                     if (c.source_type, c.source_id) not in verified_sources]
        if fallbacks:
            candidates.extend(extract_gemini(fallbacks, allow_cloud=allow_cloud,
                                             generate=generate))

    # v0.5.1 knowledge chunks intentionally overlap (~120 chars). A transaction
    # entirely inside that overlap may be extracted from BOTH chunks. Remove
    # just those boundary duplicates while keeping genuinely repeated purchases.
    def overlap_key(rec):
        f = rec['fields']
        return (rec['event_type'], str(f.get('occurred_on')),
                f.get('amount_minor'), f.get('currency'), f.get('event_status'))

    by_position = {(c.source_type, c.source_id, c.chunk_index): c for c in chunks}
    filtered = []
    for rec in candidates:
        c = by_id.get(rec['source_chunk_id'])
        if c is None:
            continue
        previous_chunk = by_position.get((c.source_type, c.source_id, c.chunk_index - 1))
        duplicate = False
        if previous_chunk is not None:
            a, b = previous_chunk.chunk_text, c.chunk_text
            # Only contiguous, exact suffix/prefix overlaps count.
            length = next((n for n in range(min(256, len(a), len(b)), 4, -1)
                           if a[-n:] == b[:n]), 0)
            if length and rec['span_end'] <= length:
                duplicate = any(
                    old['source_chunk_id'] == previous_chunk.id
                    and old['span_start'] >= len(a) - length
                    and overlap_key(old) == overlap_key(rec)
                    for old in filtered
                )
        if not duplicate:
            filtered.append(rec)
    candidates = filtered

    # Replacing all events of this email atomically avoids stale amounts when
    # source text changes or switching extraction mode (e.g. local -> hybrid).
    db.execute(delete(StructuredEvent).where(StructuredEvent.account_id == account_id,
                                              StructuredEvent.email_id == email_id))
    verified = review = 0
    seen = set()
    for rec in candidates:
        chunk = by_id.get(rec['source_chunk_id'])
        if chunk is None:
            continue
        fingerprint = _fingerprint(rec)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        fields = rec['fields']
        valid = rec['validation_status'] == 'verified'
        if valid:
            try:
                validate_event(fields)
                document = (documents.get((rec['source_type'], rec['source_id']))
                            if rec['extraction_method'] == 'regex' else None)
                valid = validate_source_anchor(chunk, rec, document)
            except ValueError:
                valid = False
        # Recompute from original trusted DB data. Do not trust cloud chunk IDs.
        event = StructuredEvent(
            account_id=account_id, email_id=email_id, source_chunk_id=chunk.id,
            source_type=chunk.source_type, event_type=rec['event_type'],
            event_fingerprint=fingerprint,
            occurred_on=(str(fields['occurred_on']) if fields.get('occurred_on') else None),
            amount_minor=fields.get('amount_minor'), currency=fields.get('currency'),
            event_status=fields.get('event_status', 'unknown'),
            title=fields.get('title') or fields.get('merchant'),
            payload_json=json.dumps(fields, ensure_ascii=False, default=str),
            evidence_quote=rec['evidence_quote'][:500],
            extraction_method=rec['extraction_method'],
            validation_status='verified' if valid else 'review',
            schema_version=SCHEMA_VERSION,
        )
        db.add(event)
        if valid:
            verified += 1
        else:
            review += 1
    run = previous or EventExtractionRun(account_id=account_id, email_id=email_id,
        extractor_version=EXTRACTOR_VERSION, source_digest=digest, mode=mode)
    run.created_at = datetime.now(timezone.utc)
    run.status = 'processed'
    run.reason_code = None
    run.verified_count = verified
    run.review_count = review
    db.add(run)
    db.commit()
    return {'email_id': email_id, 'status': 'processed',
            'verified': verified, 'review': review}
