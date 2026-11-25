"""Offline structured-event extraction from already indexed source chunks.

Default is fully local and no network calls. For unknown templates, hybrid
mode requires both --allow-cloud and MAILMIND_EVENTS_ALLOW_CLOUD=true.
Never run bulk AI model work on a restricted HPC login node.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import select

from app.database import SessionLocal, init_database
from app.models import EmailAccount, EmailMessage
from app.services.event_store import index_one_email


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--account-id', required=True)
    parser.add_argument('--max-emails', type=int, default=20)
    parser.add_argument('--mode', choices=['local', 'hybrid'], default='local')
    parser.add_argument('--allow-cloud', action='store_true')
    args = parser.parse_args()
    load_dotenv(Path(__file__).resolve().parents[1] / '.env')
    if not 1 <= args.max_emails <= 1000:
        parser.error('--max-emails 必須介於 1 到 1000')
    cloud_enabled = (args.mode == 'hybrid' and args.allow_cloud and
                     os.getenv('MAILMIND_EVENTS_ALLOW_CLOUD', '').lower() == 'true')
    if args.mode == 'hybrid' and not cloud_enabled:
        parser.error('Hybrid 雲端擷取須同時加 --allow-cloud 與 MAILMIND_EVENTS_ALLOW_CLOUD=true')
    if args.mode == 'hybrid' and args.max_emails > 5:
        parser.error('Hybrid 模式每次最多 5 封，避免意外消耗 Gemini 額度')
    init_database()
    with SessionLocal() as db:
        if db.get(EmailAccount, args.account_id) is None:
            parser.error('找不到指定 Account ID')
        ids = db.scalars(select(EmailMessage.id).where(
            EmailMessage.account_id == args.account_id,
            EmailMessage.state == 'completed',
        ).order_by(EmailMessage.created_at.desc())).all()
        scanned = changed = verified = review = no_source = 0
        for email_id in ids:
            try:
                result = index_one_email(db, args.account_id, email_id,
                                         mode=args.mode, allow_cloud=cloud_enabled)
            except Exception as exc:
                db.rollback()
                # Avoid logging raw provider responses, private emails or model outputs.
                print(f'⚠️ {email_id}: {type(exc).__name__}；未變更該封資料')
                continue
            scanned += 1
            if result['status'] == 'skipped':
                continue
            changed += 1
            verified += result['verified']
            review += result['review']
            no_source += result['status'] == 'no_source'
            if changed % 5 == 0:
                print(f'已處理 {changed} 封：verified={verified} review={review} 無原文={no_source}')
            if changed >= args.max_emails:
                break
    print(f'✅ 掃描 {scanned} 封；新處理 {changed} 封；verified={verified}，'
          f'review={review}，缺少原文={no_source}')
    print('提醒：已驗證事件不代表已涵蓋所有實際交易或全部銀行通知。')


if __name__ == '__main__':
    main()
