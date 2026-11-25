"""LINE agent gateway: verify signature, enqueue explicit commands, delegate rest.

The existing LINE webhook is intentionally untouched. To activate, change only
Flask reverse proxy's *internal* destination from /line/webhook to
/line/agent/webhook; external LINE webhook URL remains unchanged.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import (AGENT_ENABLED, AGENT_ALLOW_CLOUD_EMAIL,
                        LINE_CHANNEL_SECRET, LINE_CHANNEL_ACCESS_TOKEN)
from app.database import SessionLocal
from app.services.line_agent_queue import enqueue_question, extract_question

logger = logging.getLogger(__name__)
router = APIRouter(tags=['line-agent'])

LEGACY_URL = 'http://127.0.0.1:8000/line/webhook'
LINE_REPLY_URL = 'https://api.line.me/v2/bot/message/reply'


def sign_payload(body: bytes, secret: str) -> str:
    return base64.b64encode(hmac.new(secret.encode('utf-8'), body, hashlib.sha256).digest()).decode('ascii')


def verify_payload(body: bytes, signature: str, secret: str) -> bool:
    return bool(secret and signature and hmac.compare_digest(sign_payload(body, secret), signature))


def event_id_for(event: dict) -> str:
    """LINE IDs are stable on webhook redelivery. Refuse unidentifiable events."""
    value = event.get('webhookEventId')
    if isinstance(value, str) and value:
        return 'event:' + value[:200]
    message = event.get('message') or {}
    value = message.get('id') if isinstance(message, dict) else None
    if isinstance(value, str) and value:
        return 'message:' + value[:200]
    return ''


async def reply_line(reply_token: str | None, text: str) -> None:
    if not reply_token or not LINE_CHANNEL_ACCESS_TOKEN:
        logger.warning('Agent reply skipped: missing LINE reply token or credential')
        return
    async with httpx.AsyncClient(timeout=4) as client:
        try:
            response = await client.post(
                LINE_REPLY_URL,
                headers={'Authorization': f'Bearer {LINE_CHANNEL_ACCESS_TOKEN}'},
                json={'replyToken': reply_token,
                      'messages': [{'type': 'text', 'text': text[:1000]}]},
            )
            if response.status_code >= 400:
                logger.warning('Agent acknowledgement failed: LINE status %d', response.status_code)
        except httpx.HTTPError:
            logger.warning('Agent acknowledgement network error', exc_info=False)


async def forward_legacy(events: list[dict], destination: str = LEGACY_URL, envelope: dict | None = None) -> None:
    """Forward only non-agent events; re-sign internal payload with channel secret."""
    if not events:
        return
    forwarded = {k: v for k, v in (envelope or {}).items() if k != 'events'}
    forwarded['events'] = events
    body = json.dumps(forwarded, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    signature = sign_payload(body, LINE_CHANNEL_SECRET)
    async with httpx.AsyncClient(timeout=12) as client:
        response = await client.post(
            destination,
            content=body,
            headers={'Content-Type': 'application/json', 'X-Line-Signature': signature},
        )
        response.raise_for_status()


@router.post('/line/agent/webhook')
async def agent_webhook(request: Request):
    raw = await request.body()
    if len(raw) > 256_000:
        raise HTTPException(status_code=413, detail='payload too large')
    if not verify_payload(raw, request.headers.get('X-Line-Signature', ''), LINE_CHANNEL_SECRET):
        raise HTTPException(status_code=401, detail='invalid LINE signature')
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail='invalid JSON')
    if not isinstance(data, dict) or not isinstance(data.get('events'), list):
        raise HTTPException(status_code=400, detail='invalid events')
    if len(data['events']) > 30:
        raise HTTPException(status_code=413, detail='too many events')

    passthrough = []
    acknowledgements = []
    for event in data['events']:
        if not isinstance(event, dict):
            continue
        message = event.get('message') or {}
        source = event.get('source') or {}
        if (event.get('type') != 'message' or not isinstance(message, dict)
                or message.get('type') != 'text' or not isinstance(source, dict)
                or source.get('type') != 'user'):
            passthrough.append(event)
            continue
        question = extract_question(message.get('text', ''))
        if question is None:
            passthrough.append(event)
            continue

        if not AGENT_ENABLED or not AGENT_ALLOW_CLOUD_EMAIL:
            acknowledgement = '郵件問答尚未啟用，請先由管理員確認雲端資料處理設定。'
        elif not question:
            acknowledgement = '請輸入「查郵件 你的問題」，例如：查郵件 PDF 解析為何失敗？'
        elif len(question) > 400:
            acknowledgement = '問題太長，請縮短至 400 字以內。'
        elif not event_id_for(event) or not source.get('userId'):
            acknowledgement = '無法辨識訊息來源，請重新發送一次。'
        else:
            with SessionLocal() as db:
                status, _ = enqueue_question(
                    db, line_user_id=source['userId'],
                    event_id=event_id_for(event), question=question,
                )
            acknowledgement = {
                'queued': '已收到查詢，整理郵件後會在這個聊天室回覆。',
                'duplicate': '',  # LINE redelivery: do not reply twice.
                'unbound': '尚未綁定 Gmail 帳號，請先輸入「綁定 <代碼>」。',
                'ambiguous': '目前綁定多個 Gmail 帳號，這個版本暫不支援跨帳號查詢。',
                'limited': '已達每日問答或本月推播額度，請稍後再試。',
                'busy': '目前查詢排隊已滿，請稍後再試。',
                'invalid': '請提供有效的郵件問題。',
            }.get(status, '查詢暫時無法建立，請稍後再試。')
        if acknowledgement:
            acknowledgements.append((event.get('replyToken'), acknowledgement))

    # Preserve all other existing LINE commands through the ORIGINAL endpoint.
    # If it fails, let LINE retry. Agent jobs will remain idempotent.
    if passthrough:
        try:
            await forward_legacy(passthrough, envelope=data)
        except httpx.HTTPError:
            logger.exception('Legacy LINE webhook forwarding failed')
            raise HTTPException(status_code=502, detail='legacy webhook unavailable')
    semaphore = asyncio.Semaphore(4)
    async def acknowledged(token, text):
        async with semaphore:
            await reply_line(token, text)
    await asyncio.gather(*(acknowledged(token, text) for token, text in acknowledgements))
    return JSONResponse({'status': 'ok'})
