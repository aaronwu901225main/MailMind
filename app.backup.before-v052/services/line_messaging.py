"""Minimal LINE Messaging API client. Uses dedicated MailMind channel credentials."""
import httpx
from app.config import LINE_CHANNEL_ACCESS_TOKEN

API_BASE = 'https://api.line.me/v2/bot'


def _headers():
    if not LINE_CHANNEL_ACCESS_TOKEN:
        raise RuntimeError('MAILMIND_LINE_CHANNEL_ACCESS_TOKEN not configured')
    return {'Authorization': f'Bearer {LINE_CHANNEL_ACCESS_TOKEN}'}


def reply_message(reply_token: str, text: str) -> None:
    with httpx.Client(timeout=10) as client:
        response = client.post(
            f'{API_BASE}/message/reply',
            headers=_headers(),
            json={'replyToken': reply_token, 'messages': [{'type': 'text', 'text': text[:4900]}]},
        )
    response.raise_for_status()


def quota_remaining() -> int | None:
    """Return remaining messages in official account's monthly plan.

    Returns None only for an unlimited plan; errors intentionally fail closed.
    """
    with httpx.Client(timeout=10) as client:
        limit = client.get(f'{API_BASE}/message/quota', headers=_headers())
        used = client.get(f'{API_BASE}/message/quota/consumption', headers=_headers())
    limit.raise_for_status()
    used.raise_for_status()
    quota = limit.json()
    if quota.get('type') == 'none':
        return None
    return max(0, int(quota['value']) - int(used.json()['totalUsage']))


def push_summary(line_user_id: str, content: str, retry_key: str) -> bool:
    """Return True when accepted; 409 with accepted-request-id is deduplicated success."""
    headers = {**_headers(), 'X-Line-Retry-Key': retry_key}
    with httpx.Client(timeout=15) as client:
        r = client.post(
            f'{API_BASE}/message/push',
            headers=headers,
            json={'to': line_user_id, 'messages': [{'type': 'text', 'text': content[:4900]}]},
        )
    if r.status_code == 409 and r.headers.get('x-line-accepted-request-id'):
        return True
    r.raise_for_status()
    return True


def render_summary(sender: str, subject: str, summary: str, level: str) -> str:
    names = {'short': '極簡', 'medium': '中等', 'full': '完整'}
    header = (f'📨 MailMind 新郵件\n'
              f'主旨：{subject[:160]}\n'
              f'寄件者：{sender[:130]}\n'
              f'摘要模式：{names.get(level, level)}\n\n')
    budget = max(0, 4900 - len(header) - 20)
    summary = summary if len(summary) <= budget else summary[:budget] + '\n…（內容已截短）'
    return header + summary
