"""Safely preview/reprocess selected emails under the new segmenting extractor.

--preview reads existing body/attachment chunks and shows parsed event fields
without database writes or network calls. Reprocessing atomically replaces
only the selected email's structured events; existing emails remain intact.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import select

from app.database import SessionLocal, init_database
from app.models import EmailAccount, KnowledgeChunk
from app.services.event_extraction import extract_document
from app.services.event_store import index_one_email
from app.services.event_schema import EXTRACTOR_VERSION


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--account-id',required=True)
    p.add_argument('--email-id',action='append',required=True,
                   help='May be repeated, maximum 20 per run')
    p.add_argument('--preview',action='store_true',help='Read-only preview; no mutations')
    args=p.parse_args()
    ids=list(dict.fromkeys(args.email_id))
    if len(ids)>20:
        p.error('每次最多指定 20 封')
    load_dotenv(Path(__file__).resolve().parents[1]/'.env')
    if not args.preview:
        init_database()
    with SessionLocal() as db:
        if db.get(EmailAccount,args.account_id) is None:
            p.error('找不到指定 Account ID')
        print(f'Extractor: {EXTRACTOR_VERSION}; preview={args.preview}')
        for email_id in ids:
            if args.preview:
                chunks=db.scalars(select(KnowledgeChunk).where(
                    KnowledgeChunk.account_id==args.account_id,
                    KnowledgeChunk.email_id==email_id,
                    KnowledgeChunk.source_type.in_(['body','attachment']),
                ).order_by(KnowledgeChunk.source_type,KnowledgeChunk.source_id,
                           KnowledgeChunk.chunk_index)).all()
                if not chunks:
                    print(f'{email_id}: no source chunks')
                    continue
                grouped=defaultdict(list)
                for c in chunks:grouped[(c.source_type,c.source_id)].append(c)
                rows=[]
                for group in grouped.values():rows.extend(extract_document(group))
                print(f'{email_id}: {len(rows)} candidate events (NOT yet DB verified)')
                for r in rows:
                    f=r['fields']
                    print(' ',f['event_type'],f.get('occurred_on'),f.get('currency'),
                          f.get('amount_minor'),f.get('merchant'),r['validation_status'],
                          r['reason'])
                continue
            try:
                outcome=index_one_email(db,args.account_id,email_id,mode='local')
            except Exception as exc:
                db.rollback()
                print(f'⚠️ {email_id}: {type(exc).__name__}；該封沒有更新')
                continue
            print(f"{email_id}: {outcome['status']} "
                  f"verified={outcome['verified']} review={outcome['review']}")
    print('完成。reprocess 會更換事件 ID；請重新查詢 aggregation。')

if __name__=='__main__':
    main()
