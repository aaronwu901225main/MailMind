"""CLI to compare FTS-only and opt-in local semantic / RRF search."""
import argparse

from app.database import SessionLocal, init_database
from app.models import EmailAccount
from app.services.hybrid_search import hybrid_search


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--account-id', required=True)
    p.add_argument('--query', required=True)
    p.add_argument('--limit', type=int, default=5)
    p.add_argument('--semantic', action='store_true', help='Load cached CPU model for query')
    args = p.parse_args()
    init_database()
    with SessionLocal() as db:
        if not db.get(EmailAccount, args.account_id):
            raise SystemExit('找不到指定帳號')
        hits = hybrid_search(db, args.account_id, args.query, limit=args.limit,
                             use_semantic=args.semantic)
    for n, hit in enumerate(hits, 1):
        print(f"\n{n}. {hit['subject']} [{hit['source_type']}] ")
        print(f"email_id={hit['email_id']} chunk_id={hit['chunk_id']} rrf={hit['rrf_score']:.5f} via={','.join(hit['retrieval_modes'])}")
        print(hit['snippet'][:300].replace('\n', ' '))
    print(f'\n共 {len(hits)} 筆片段')
    if not args.semantic:
        print('（目前為 FTS-only；向量搜尋請另加 --semantic）')


if __name__ == '__main__':
    main()
