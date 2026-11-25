"""Backfill v0.4 stored email summaries / attachment extraction into v0.5.1.

Safe to re-run. No LLM, LINE push or Gmail API calls. The historical email body
cannot be recovered if it wasn't saved by v0.4; it will be indexed for new mail.
"""
import argparse
import logging
from sqlalchemy import select

from app.database import SessionLocal, init_database
from app.models import EmailAccount, EmailMessage
from app.services.knowledge_index import index_email
from app.services.knowledge_search import ensure_knowledge_fts

logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--account-id', required=True)
    parser.add_argument('--batch-size', type=int, default=50)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 500:
        parser.error('--batch-size must be 1..500')
    init_database()
    with SessionLocal() as db:
        ensure_knowledge_fts(db.get_bind())
        account = db.get(EmailAccount, args.account_id)
        if account is None:
            raise SystemExit('找不到這個帳號 ID')
        ids = db.scalars(select(EmailMessage.id)
                         .where(EmailMessage.account_id == args.account_id)
                         .order_by(EmailMessage.created_at, EmailMessage.id)).all()
    changes = 0
    processed = 0
    for start in range(0, len(ids), args.batch_size):
        with SessionLocal() as db:
            for email_id in ids[start:start + args.batch_size]:
                mail = db.get(EmailMessage, email_id)
                if mail is not None:
                    changes += index_email(db, mail, body=None)
                    processed += 1
            db.commit()
        print(f'已掃描 {processed}/{len(ids)} 封信，新增或重建 {changes} 個 Chunks')
    print(f'索引回填完成：{processed} 封；異動 Chunks = {changes}。')
    print('備註：v0.4 舊信未儲存的原始內文無法由資料庫還原。')


if __name__ == '__main__':
    main()
