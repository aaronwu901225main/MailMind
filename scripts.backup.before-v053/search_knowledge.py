"""CLI search for an already-authorized account. Does not call LLM."""
import argparse
from app.database import SessionLocal, init_database
from app.models import EmailAccount
from app.services.knowledge_search import ensure_knowledge_fts, search_chunks


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--account-id', required=True)
    p.add_argument('--keyword', required=True)
    p.add_argument('--limit', type=int, default=10)
    args = p.parse_args()
    init_database()
    with SessionLocal() as db:
        if db.get(EmailAccount, args.account_id) is None:
            raise SystemExit('找不到這個帳號 ID')
        ensure_knowledge_fts(db.get_bind())
        results = search_chunks(db, args.account_id, args.keyword, args.limit)
    if not results:
        print('沒有符合的索引內容')
    for hit in results:
        print(f"\n[{hit['source_type']}] {hit['subject']} | {hit['email_id']} | chunk={hit['chunk_index']}")
        if hit['filename']:
            print(f"附件：{hit['filename']}")
        print(hit['snippet'][:350].replace('\n', ' '))
    print(f'\n共 {len(results)} 筆片段')


if __name__ == '__main__':
    main()
