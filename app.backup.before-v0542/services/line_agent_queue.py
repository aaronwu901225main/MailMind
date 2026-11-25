"""Conservative account-bound queue rules for LINE questions.

This module has no model calls and no LINE HTTP calls. All decisions are made
from the verified LINE source identity, never from a user-supplied account ID.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import AGENT_DAILY_LIMIT, AGENT_MAX_QUEUE, AGENT_MONTHLY_PUSH_LIMIT, MAX_LINE_PUSHES_PER_MONTH
from app.models import DeliveryLog, EmailAccount, LineAgentJob, User

TZ = ZoneInfo('Asia/Taipei')


def month_start_utc(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(TZ)
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def day_start_utc(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(TZ)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def quota_ok(db: Session, now: datetime | None = None) -> bool:
    """Conservative local budget; LINE platform quotas remain authoritative.

    Count both MailMind email pushes and agent pushes to avoid using agent quota
    without considering existing email notifications. Pending pushes in the other
    worker can still race; keep a buffer and monitor LINE OA Manager.
    """
    if AGENT_MONTHLY_PUSH_LIMIT <= 0 or MAX_LINE_PUSHES_PER_MONTH <= 0:
        return False
    start = month_start_utc(now)
    sent_agent = db.scalar(select(func.count(LineAgentJob.id)).where(
        LineAgentJob.status == 'sent', LineAgentJob.created_at >= start)) or 0
    if sent_agent >= AGENT_MONTHLY_PUSH_LIMIT:
        return False
    sent_emails = db.scalar(select(func.count(DeliveryLog.id)).where(
        DeliveryLog.channel == 'line',
        DeliveryLog.status.in_(['sent', 'success', 'delivered']),
        DeliveryLog.created_at >= start)) or 0
    # Leave some room for other LINE notifications and uneven quota accounting.
    return sent_emails + sent_agent < max(0, MAX_LINE_PUSHES_PER_MONTH - 5)


def extract_question(text: str) -> str | None:
    """Only explicit prefixes activate the agent; other LINE features pass through."""
    if not isinstance(text, str):
        return None
    text = text.strip()
    for command in ('查郵件', '問郵件'):
        if text == command:
            return ''
        if text.startswith(command):
            remainder = text[len(command):]
            if remainder and (remainder[0].isspace() or remainder[0] in '：:'):
                return remainder.lstrip('：: \r\n\t\u3000')
    return None


def enqueue_question(db: Session, *, line_user_id: str, event_id: str,
                     question: str, now: datetime | None = None) -> tuple[str, str | None]:
    """Return (status, job_id): queued/duplicate/unbound/ambiguous/limited/busy/invalid.

    No endpoint caller is allowed to choose user_id or account_id. One linked
    account per LINE user is supported in this MVP; ambiguity fails closed.
    """
    now = now or datetime.now(timezone.utc)
    if not line_user_id or not event_id or not question.strip() or len(question) > 400:
        return 'invalid', None
    event_hash = hashlib.sha256(('line-v053:' + event_id).encode('utf-8')).hexdigest()
    previous = db.scalar(select(LineAgentJob.id).where(LineAgentJob.event_hash == event_hash))
    if previous:
        return 'duplicate', previous

    user = db.scalar(select(User).where(User.line_user_id == line_user_id))
    if user is None:
        return 'unbound', None
    accounts = db.scalars(select(EmailAccount).where(EmailAccount.user_id == user.id)).all()
    if len(accounts) != 1:
        return ('ambiguous' if accounts else 'unbound'), None
    count = db.scalar(select(func.count(LineAgentJob.id)).where(
        LineAgentJob.user_id == user.id,
        LineAgentJob.created_at >= day_start_utc(now))) or 0
    if count >= AGENT_DAILY_LIMIT or not quota_ok(db, now):
        return 'limited', None
    queued_count = db.scalar(select(func.count(LineAgentJob.id)).where(
        LineAgentJob.status.in_(['pending', 'processing']))) or 0
    if queued_count >= AGENT_MAX_QUEUE:
        return 'busy', None

    job = LineAgentJob(event_hash=event_hash, user_id=user.id,
                       account_id=accounts[0].id, line_user_id=line_user_id,
                       question=question.strip(), status='pending', created_at=now)
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if db.scalar(select(LineAgentJob.id).where(LineAgentJob.event_hash == event_hash)):
            return 'duplicate', None
        raise
    return 'queued', job.id
