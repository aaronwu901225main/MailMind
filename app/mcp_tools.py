"""User-scoped, read-only tools. No caller-supplied filesystem paths or SQL."""
from sqlalchemy import or_, select
from app.config import MCP_ACCOUNT_ID
from app.database import SessionLocal
from app.models import EmailAccount, EmailMessage, EmailAttachment, Summary


def account_id_or_raise(account_id: str | None = None) -> str:
    account_id = account_id if account_id is not None else MCP_ACCOUNT_ID
    if not account_id:
        raise ValueError('請先設定 MAILMIND_MCP_ACCOUNT_ID')
    with SessionLocal() as db:
        if db.get(EmailAccount, account_id) is None:
            raise ValueError('沒有找到指定的 Gmail 帳號')
    return account_id


def search_emails(keyword: str, limit: int = 10) -> list[dict]:
    """Search email subjects, sender and stored summaries for this specific account."""
    account_id = account_id_or_raise()
    limit = max(1, min(int(limit), 20))
    with SessionLocal() as db:
        stmt = (select(EmailMessage, Summary)
                .outerjoin(Summary, Summary.email_id == EmailMessage.id)
                .where(EmailMessage.account_id == account_id))
        if keyword.strip():
            pattern = '%' + keyword.strip().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            stmt = stmt.where(or_(EmailMessage.subject.ilike(pattern, escape='\\'),
                                  EmailMessage.sender.ilike(pattern, escape='\\'),
                                  Summary.content.ilike(pattern, escape='\\')))
        rows = db.execute(stmt.order_by(EmailMessage.created_at.desc()).limit(limit)).all()
        return [{'email_id': mail.id, 'subject': mail.subject,
                 'sender': mail.sender, 'state': mail.state,
                 'summary_preview': summary.content[:240] if summary else ''}
                for mail, summary in rows]


def get_email_summary(email_id: str) -> dict:
    """Read a stored summary, refusing emails belonging to any other account."""
    account_id = account_id_or_raise()
    with SessionLocal() as db:
        result = db.execute(select(EmailMessage, Summary)
                            .outerjoin(Summary, Summary.email_id == EmailMessage.id)
                            .where(EmailMessage.id == email_id,
                                   EmailMessage.account_id == account_id)).first()
        if not result:
            raise ValueError('找不到這封郵件或無權限存取')
        mail, summary = result
        return {'email_id': mail.id, 'sender': mail.sender, 'subject': mail.subject,
                'state': mail.state, 'summary': summary.content if summary else '',
                'summary_level': summary.summary_level if summary else None}


def list_email_attachments(email_id: str) -> list[dict]:
    """List stored attachment extraction metadata for an authorized email."""
    account_id = account_id_or_raise()
    with SessionLocal() as db:
        mail = db.scalar(select(EmailMessage.id).where(EmailMessage.id == email_id,
                                                     EmailMessage.account_id == account_id))
        if not mail:
            raise ValueError('找不到這封郵件或無權限存取')
        attachments = db.scalars(select(EmailAttachment)
                                 .where(EmailAttachment.email_id == email_id)
                                 .order_by(EmailAttachment.position)).all()
        return [{'attachment_id': a.id, 'filename': a.filename, 'mime_type': a.mime_type,
                 'status': a.status, 'size_bytes': a.size_bytes, 'note': a.note}
                for a in attachments]


def read_attachment_text(attachment_id: str) -> dict:
    """Read extracted PDF/DOCX/image text, not arbitrary files or binary attachments."""
    account_id = account_id_or_raise()
    with SessionLocal() as db:
        result = db.execute(select(EmailAttachment, EmailMessage)
                            .join(EmailMessage, EmailAttachment.email_id == EmailMessage.id)
                            .where(EmailAttachment.id == attachment_id,
                                   EmailMessage.account_id == account_id)).first()
        if not result:
            raise ValueError('找不到附件或無權限存取')
        a, mail = result
        return {'attachment_id': a.id, 'email_id': mail.id,
                'filename': a.filename, 'mime_type': a.mime_type,
                'status': a.status, 'text': a.extracted_text[:6000], 'note': a.note}
