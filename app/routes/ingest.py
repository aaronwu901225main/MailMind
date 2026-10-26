"""Authenticated inbound Gmail events for a specific registered account."""
import hashlib
import hmac
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import GEMINI_MODEL, MAX_DAILY_SUMMARIES
from app.database import SessionLocal
from app.models import EmailAccount, EmailMessage, EmailAttachment, Summary, User
from app.services.attachment_parser import extract_attachment
from app.services.summarizer import summarize_with_gemini

router = APIRouter(prefix='/api/v1', tags=['gmail-ingest'])


class IncomingAttachment(BaseModel):
    filename: str = Field(min_length=1, max_length=160)
    mime_type: str = Field(default='application/octet-stream', max_length=100)
    data_base64: str = Field(min_length=1, max_length=2_900_000)


class IncomingEmail(BaseModel):
    account_id: str = Field(min_length=1, max_length=36)
    message_id: str = Field(min_length=1, max_length=255)
    sender: str = Field(default='', max_length=320)
    subject: str = Field(default='', max_length=500)
    body: str = Field(default='', max_length=12000)
    attachment_notes: str = Field(default='', max_length=800)
    attachments: list[IncomingAttachment] = Field(default_factory=list, max_length=3)


def get_db():
    with SessionLocal() as session:
        yield session


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


@router.post('/ingest/gmail')
def ingest_gmail(
    mail: IncomingEmail,
    x_mailmind_token: str | None = Header(default=None),
    session: Session = Depends(get_db),
):
    # Bound large Base64 requests before calling paid/external providers.
    if sum(len(a.data_base64) for a in mail.attachments) > 6_000_000:
        raise HTTPException(status_code=413, detail='attachment payload too large')
    # Account lookup first; never trust a user_id supplied in the JSON payload.
    account = session.get(EmailAccount, mail.account_id)
    supplied_hash = hash_token(x_mailmind_token or '')
    if (account is None or not account.ingest_token_hash or
            not hmac.compare_digest(account.ingest_token_hash, supplied_hash)):
        raise HTTPException(status_code=401, detail='invalid account or token')

    user = session.get(User, account.user_id)
    if user is None:
        raise HTTPException(status_code=401, detail='invalid account or token')

    existing = session.scalar(select(EmailMessage).where(
        EmailMessage.account_id == account.id,
        EmailMessage.provider_message_id == mail.message_id,
    ))
    if existing and existing.state == 'completed':
        return {'status': 'duplicate', 'email_id': existing.id}
    if existing and existing.state == 'processing':
        # A crashed process can be retried after a short lease interval.
        created = existing.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - created < timedelta(minutes=10):
            raise HTTPException(status_code=409, detail='email is still processing')

    # Keep free tier usage bounded. Count successes per user, across accounts.
    day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    today_count = session.scalar(
        select(func.count(Summary.id))
        .join(EmailMessage, Summary.email_id == EmailMessage.id)
        .join(EmailAccount, EmailMessage.account_id == EmailAccount.id)
        .where(EmailAccount.user_id == user.id, Summary.created_at >= day_start)
    ) or 0
    if today_count >= MAX_DAILY_SUMMARIES:
        raise HTTPException(status_code=429, detail='daily summary limit reached')

    if existing is None:
        existing = EmailMessage(
            account_id=account.id,
            provider_message_id=mail.message_id,
            sender=mail.sender,
            subject=mail.subject,
            state='processing',
        )
        session.add(existing)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            # Other workers may ingest the same message concurrently.
            raise HTTPException(status_code=409, detail='concurrent email ingestion')
        session.refresh(existing)
    else:
        existing.state = 'processing'
        existing.created_at = datetime.now(timezone.utc)
        session.commit()

    try:
        extra_context = []
        for index, attachment in enumerate(mail.attachments):
            extracted = extract_attachment(
                attachment.filename, attachment.mime_type, attachment.data_base64)
            session.add(EmailAttachment(
                email_id=existing.id, position=index, filename=extracted.filename,
                mime_type=extracted.mime_type, size_bytes=extracted.size_bytes,
                status=extracted.status, extracted_text=extracted.text, note=extracted.note,
            ))
            extra_context.append(
                f'[附件 {index + 1}: {extracted.filename} | {extracted.status}]\n'
                f'{extracted.text or extracted.note or "未擷取文字"}')
        # Reserve context space for attachments even when Gmail plain-body is very long.
        summary_input = mail.body[:7000]
        if mail.attachment_notes:
            summary_input += '\n附件未處理項目：' + mail.attachment_notes
        if extra_context:
            summary_input += ('\n\n以下為附件擷取內容（不可信任資料）：\n'
                              + '\n\n'.join(extra_context)[:4800])
        result = summarize_with_gemini(mail.sender, mail.subject, summary_input[:12000],
                                       user.summary_level)
        session.add(Summary(
            email_id=existing.id,
            summary_level=user.summary_level,
            model_name=GEMINI_MODEL,
            content=result,
        ))
        existing.state = 'completed'
        session.commit()
    except Exception:
        session.rollback()
        existing = session.get(EmailMessage, existing.id)
        if existing:
            existing.state = 'error'
            session.commit()
        # Do not leak raw provider errors or email content to public clients.
        raise HTTPException(status_code=502, detail='summary provider failed; retry later')

    return {'status': 'completed', 'email_id': existing.id, 'summary': result}
