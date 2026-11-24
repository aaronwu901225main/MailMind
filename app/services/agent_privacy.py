"""Best-effort minimization before sending candidate excerpts to cloud Gemini.

Not a DLP guarantee: users must explicitly opt in and avoid sensitive inboxes.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

URL = re.compile(r'https?://[^\s<>"\']+', re.I)
EMAIL = re.compile(r'(?<![\w.])[-\w.+]+@[-\w.]+\.[A-Za-z]{2,}', re.I)
BEARER = re.compile(r'\bBearer\s+[A-Za-z0-9_./=+~-]{8,}', re.I)
SECRETS = re.compile(r'(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\s*[:=]\s*[^\s;,]+')
PHONE = re.compile(r'(?<!\d)09\d{8}(?!\d)')


def redact_for_cloud(text: str, max_chars: int = 1200) -> str:
    """Avoid leaking personal email, LINE unsubscribe tokens, URLs with query IDs."""
    text = (text or '')[:max(0, min(int(max_chars), 5000))]
    def clean_url(match):
        raw = match.group(0)
        try:
            parts = urlsplit(raw)
            if parts.query or parts.fragment or any(k in parts.path.lower() for k in ('unsubscribe', 'token', 'verify', 'reset')):
                return '[已隱藏含識別資訊的連結]'
            return f'{parts.scheme}://{parts.netloc}{parts.path[:90]}'
        except ValueError:
            return '[已隱藏連結]'
    text = URL.sub(clean_url, text)
    text = BEARER.sub('[已隱藏憑證]', text)
    text = SECRETS.sub('[已隱藏憑證欄位]', text)
    text = EMAIL.sub('[電子郵件地址]', text)
    text = PHONE.sub('[手機號碼]', text)
    return text
