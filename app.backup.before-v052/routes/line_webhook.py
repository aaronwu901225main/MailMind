"""LINE webhook: verified signature, one-time account linking and user preferences."""
import base64
import hashlib
import hmac
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import LINE_CHANNEL_SECRET
from app.models import DeliveryLog, EmailAccount, EmailMessage, LineLinkCode, User
from app.routes.ingest import get_db
from app.services.line_messaging import reply_message

router = APIRouter(prefix='/line', tags=['line'])
log = logging.getLogger(__name__)


def _as_utc(dt):
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def verify_signature(data: bytes, signature: str | None) -> bool:
    if not LINE_CHANNEL_SECRET or not signature:
        return False
    digest = hmac.new(LINE_CHANNEL_SECRET.encode(), data, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature)


def skip_existing_mail(session: Session, user: User) -> None:
    """Don't push historical mail when a user links or re-enables notifications."""
    ids = session.scalars(select(EmailMessage.id)
        .join(EmailAccount, EmailMessage.account_id == EmailAccount.id)
        .where(EmailAccount.user_id == user.id)).all()
    already = set(session.scalars(select(DeliveryLog.email_id).where(
        DeliveryLog.user_id == user.id, DeliveryLog.channel == 'line')).all())
    for email_id in ids:
        if email_id not in already:
            session.add(DeliveryLog(
                email_id=email_id, user_id=user.id, channel='line', status='skipped'))


def handle_text(session: Session, line_id: str, value: str) -> str:
    value = value.strip()
    if value.startswith('綁定 '):
        code = value.removeprefix('綁定 ').strip()
        digest = hashlib.sha256(code.encode()).hexdigest()
        link = session.scalar(select(LineLinkCode).where(LineLinkCode.code_hash == digest))
        now = datetime.now(timezone.utc)
        if not link or link.consumed_at or _as_utc(link.expires_at) <= now:
            return '綁定碼無效或已過期，請重新產生。'
        user = session.get(User, link.user_id)
        if user is None:
            return '找不到指定帳號。'
        others = session.scalar(select(User).where(
            User.line_user_id == line_id, User.id != user.id))
        if others:
            return '這個 LINE 帳號已綁定其他 MailMind 使用者。'
        # Ensure no older emails are sent in a burst after linking.
        skip_existing_mail(session, user)
        user.line_user_id = line_id
        link.consumed_at = now
        session.commit()
        return '✅ 綁定完成！往後收到的新郵件將依照設定推送摘要。輸入「我的設定」查看狀態。'

    user = session.scalar(select(User).where(User.line_user_id == line_id))
    if value in ('說明', '幫助', 'help', '/help'):
        return 'MailMind 指令：\n綁定 <一次性代碼>\n極簡／中等／完整\n開啟通知／關閉通知\n我的設定'
    if not user:
        return '尚未綁定 MailMind。請先在後端產生綁定碼，再輸入「綁定 代碼」。'
    level_map = {'極簡': 'short', '中等': 'medium', '完整': 'full'}
    if value in level_map:
        user.summary_level = level_map[value]
        session.commit()
        return f'✅ 摘要模式已改為「{value}」，從下一封新郵件起生效。'
    if value == '關閉通知':
        user.notifications_enabled = False
        session.commit()
        return '🔕 已關閉 MailMind 新郵件推播。'
    if value == '開啟通知':
        skip_existing_mail(session, user)
        user.notifications_enabled = True
        session.commit()
        return '🔔 已開啟推播，僅通知現在起的新郵件。'
    if value == '我的設定':
        level = {'short': '極簡', 'medium': '中等', 'full': '完整'}.get(user.summary_level, '中等')
        enabled = '開啟' if user.notifications_enabled else '關閉'
        return f'⚙️ MailMind\n摘要模式：{level}\n新郵件通知：{enabled}'
    return '輸入「說明」查看 MailMind 支援的指令。'


@router.post('/webhook')
async def line_webhook(request: Request,
                       x_line_signature: str | None = Header(default=None),
                       session: Session = Depends(get_db)):
    body = await request.body()
    if not verify_signature(body, x_line_signature):
        raise HTTPException(status_code=401, detail='invalid signature')
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail='invalid JSON')
    events = payload.get('events', []) if isinstance(payload, dict) else []
    for event in events:
        if not isinstance(event, dict):
            continue
        source = event.get('source') or {}
        if source.get('type') != 'user' or not source.get('userId'):
            continue  # No account linking from group chats.
        if event.get('type') != 'message' or (event.get('message') or {}).get('type') != 'text':
            continue
        token = event.get('replyToken')
        if not token:
            continue
        try:
            answer = handle_text(session, source['userId'], event['message'].get('text', ''))
            reply_message(token, answer)
        except Exception:
            # Webhook returns 200 to avoid repeated events; log technical failures only.
            log.exception('LINE message processing error')
            session.rollback()
    return {'status': 'ok'}
