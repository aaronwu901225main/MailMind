"""Find account-scoped mail IDs for targeted event extraction checks.

Read-only; subjects can contain private data, so run locally and redact before
sharing any printed output.
"""
from __future__ import annotations
import argparse
from sqlalchemy import select, func
from app.database import SessionLocal
from app.models import EmailMessage, KnowledgeChunk


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--account-id',required=True)
    p.add_argument('--subject-contains',required=True)
    p.add_argument('--limit',type=int,default=10)
    args=p.parse_args()
    if not args.subject_contains or len(args.subject_contains)>100:
        p.error('請提供 1～100 字的主旨關鍵字')
    if not (1<=args.limit<=50):p.error('limit 介於 1～50')
    with SessionLocal() as db:
        emails=db.scalars(select(EmailMessage).where(
            EmailMessage.account_id==args.account_id,
            EmailMessage.subject.contains(args.subject_contains),
        ).order_by(EmailMessage.created_at.desc()).limit(args.limit)).all()
        for mail in emails:
            count=db.scalar(select(func.count(KnowledgeChunk.id)).where(
                KnowledgeChunk.account_id==args.account_id,
                KnowledgeChunk.email_id==mail.id,
                KnowledgeChunk.source_type.in_(['body','attachment']),
            )) or 0
            print(f'{mail.id}  chunks={count}  {mail.subject}')
        print(f'符合郵件數：{len(emails)}')

if __name__=='__main__':main()
