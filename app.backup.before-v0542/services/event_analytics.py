"""Account-scoped read-only analytics over validated structured events.

No arbitrary SQL, no model arithmetic, no cross-currency sum. Date boundaries
are [start_date, end_date) and use extracted event-local calendar dates.
"""
from __future__ import annotations

import json
from datetime import date
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from app.models import StructuredEvent, EventExtractionRun, EmailMessage
from app.services.event_schema import EVENT_SCHEMAS, CURRENCY_DECIMALS


def _dates(start_date: str | None, end_date: str | None):
    start = date.fromisoformat(start_date) if start_date else None
    end = date.fromisoformat(end_date) if end_date else None
    if start and end and start >= end:
        raise ValueError('end_date_must_be_after_start_date')
    if start and end and (end - start).days > 366:
        raise ValueError('maximum_date_range_is_one_year')
    return start, end


def _event_query(account_id, event_type, start_date, end_date, verified_only=True):
    if not account_id:
        raise ValueError('missing_account_scope')
    if event_type not in EVENT_SCHEMAS and event_type is not None:
        raise ValueError('unsupported_event_type')
    start, end = _dates(start_date, end_date)
    stmt = select(StructuredEvent).where(StructuredEvent.account_id == account_id)
    if event_type:
        stmt = stmt.where(StructuredEvent.event_type == event_type)
    if verified_only:
        stmt = stmt.where(StructuredEvent.validation_status == 'verified')
    if start:
        stmt = stmt.where(StructuredEvent.occurred_on >= start.isoformat())
    if end:
        stmt = stmt.where(StructuredEvent.occurred_on < end.isoformat())
    return stmt


def _serialize(event: StructuredEvent, evidence=False):
    output = {'event_id': event.id, 'event_type': event.event_type,
              'occurred_on': event.occurred_on, 'event_status': event.event_status,
              'currency': event.currency, 'amount_minor': event.amount_minor,
              'title': event.title, 'validation_status': event.validation_status,
              'source_email_id': event.email_id, 'source_chunk_id': event.source_chunk_id,
              'extraction_method': event.extraction_method}
    if evidence:
        output.update({'payload': json.loads(event.payload_json),
                       'evidence_quote': event.evidence_quote,
                       'schema_version': event.schema_version})
    return output


def search_events(db: Session, account_id: str, *, event_type: str | None = None,
                  start_date: str | None = None, end_date: str | None = None,
                  limit: int = 20, include_review: bool = False) -> list[dict]:
    limit = max(1, min(int(limit), 50))
    stmt = _event_query(account_id, event_type, start_date, end_date,
                        verified_only=not include_review)
    rows = db.scalars(stmt.order_by(StructuredEvent.occurred_on.desc(),
                                    StructuredEvent.id).limit(limit)).all()
    return [_serialize(row) for row in rows]


def get_event_detail(db: Session, account_id: str, event_id: str) -> dict:
    event = db.scalar(select(StructuredEvent).where(
        StructuredEvent.id == event_id, StructuredEvent.account_id == account_id))
    if not event:
        raise ValueError('event_not_found_or_forbidden')
    return _serialize(event, evidence=True)


def aggregate_events(db: Session, account_id: str, *, event_type: str,
                     start_date: str, end_date: str, status: str = 'purchase') -> dict:
    """Verified-only aggregation; transaction sums grouped by currency.

    The public `purchase` query means *notified spending*, NOT settled spend.
    Authorization-only banks are included while refunds/reversals are excluded.
    This cannot be used as an official statement or claim complete coverage.
    """
    if event_type not in EVENT_SCHEMAS:
        raise ValueError('unsupported_event_type')
    if not start_date or not end_date:
        raise ValueError('date_range_required')
    stmt = _event_query(account_id, event_type, start_date, end_date)
    if event_type == 'transaction':
        if status not in {'purchase', 'refund', 'reversal', 'authorization'}:
            raise ValueError('unsupported_transaction_status')
        # Backwards compatible: 'purchase' represents spending notifications,
        # including authorization-only ones that are never followed by a
        # settlement email. Keep each source's original status in its record.
        if status == 'purchase':
            stmt = stmt.where(StructuredEvent.event_status.in_(['purchase', 'authorization']))
        else:
            stmt = stmt.where(StructuredEvent.event_status == status)
        rows = db.execute(select(
            StructuredEvent.currency,
            func.count(StructuredEvent.id),
            func.sum(StructuredEvent.amount_minor),
        ).where(stmt.whereclause).group_by(StructuredEvent.currency)).all()
        values = [{'currency': code, 'transaction_count': count,
                   'amount_minor': int(amount or 0),
                   'minor_unit_digits': CURRENCY_DECIMALS.get(code, 2)}
                  for code, count, amount in rows if code is not None]
        result = {'event_type': 'transaction', 'status': status, 'start_date': start_date,
                  'end_date_exclusive': end_date, 'totals_by_currency': values,
                  'is_complete': False,
                  'warning': ('僅統計已驗證且已匯入的交易通知，purchase 查詢亦包含授權型消費通知；'
                              '並非最終請款或正式帳單。無法證明所有交易都有寄送郵件，跨封重複通知尚未對帳；'
                              '退款與取消交易未併入消費合計，幣別分開計算。')}
    else:
        count = db.scalar(select(func.count(StructuredEvent.id)).where(stmt.whereclause)) or 0
        result = {'event_type': 'deadline', 'start_date': start_date,
                  'end_date_exclusive': end_date, 'verified_event_count': int(count),
                  'is_complete': False,
                  'warning': '僅含已匯入且確認的截止事項；未知格式或未收信件可能遺漏。'}
    return result


def get_extraction_coverage(db: Session, account_id: str) -> dict:
    """Counts all-time audited mail extraction, NOT completeness of bank spend.

    Email timestamps are ingestion timestamps, not transaction timestamps.
    Multiple run modes may exist per email; use the latest one for counts.
    """
    if not account_id:
        raise ValueError('missing_account_scope')
    runs = db.scalars(select(EventExtractionRun).where(
        EventExtractionRun.account_id == account_id,
    ).order_by(EventExtractionRun.created_at.desc(), EventExtractionRun.id.desc())).all()
    latest = {}
    for r in runs:
        latest.setdefault(r.email_id, r)
    emails_total = db.scalar(select(func.count(EmailMessage.id)).where(
        EmailMessage.account_id == account_id)) or 0
    no_source = sum(r.status == 'no_source' for r in latest.values())
    processed = sum(r.status == 'processed' for r in latest.values())
    no_verified = sum(r.status == 'processed' and r.verified_count == 0
                      for r in latest.values())
    reviews = sum(r.review_count for r in latest.values())
    return {'account_email_count': int(emails_total), 'emails_with_extraction_attempt': len(latest),
            'unattempted_email_count': max(0, int(emails_total) - len(latest)),
            'processed_email_count': processed, 'no_original_source_count': no_source,
            'processed_without_verified_event_count': no_verified,
            'review_event_count': reviews,
            'coverage_warning': ('這是郵件擷取工作的覆蓋率，不是信用卡交易的完整率；'
                                 '也不能確定每封有交易的通知都已經收到。')}
