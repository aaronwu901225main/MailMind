"""v0.5.2.1 email-grouped retrieval; cloud reranking requires --rerank.

Usage:
 python -m scripts.search_relevant_emails --account-id ID --query 'PDF 文字辨識失敗' --semantic
 python -m scripts.search_relevant_emails --account-id ID --query 'PDF 文字辨識失敗' --semantic --rerank
"""
import argparse

from app.database import SessionLocal, init_database
from app.models import EmailAccount
from app.services.relevance_search import search_relevant_emails


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--account-id', required=True)
    parser.add_argument('--query', required=True)
    parser.add_argument('--limit', type=int, default=5)
    parser.add_argument('--semantic', action='store_true', help='Use cached CPU embedding model')
    parser.add_argument('--rerank', action='store_true', help='Explicitly send retrieved snippets to Gemini (privacy/usage)')
    parser.add_argument('--include-partial', action='store_true', help='Include partially relevant emails if --rerank')
    args = parser.parse_args()
    init_database()
    with SessionLocal() as session:
        if not session.get(EmailAccount, args.account_id):
            parser.error('Unknown account')
        try:
            results = search_relevant_emails(
                session, args.account_id, args.query, limit=args.limit,
                use_semantic=args.semantic, rerank=args.rerank,
                include_partial=args.include_partial)
        except Exception as error:
            parser.exit(2, f'檢索或 rerank 失敗：{type(error).__name__}：{error}\n'
                            '沒有回傳任何未經驗證的「相關」結果。\n')

    if not args.rerank:
        print('⚠️ 以下是候選結果，尚未進行相關性判斷；不代表每封都相關。')
    for n, result in enumerate(results, 1):
        print(f'\n{n}. {result["subject"]}  [relevance={result["relevance"]}]')
        print('email_id=' + result['email_id'])
        supporting = set(result.get('supporting_chunk_ids', []))
        for ev in result['evidence']:
            if args.rerank and ev['chunk_id'] not in supporting:
                continue
            print(f'  [{ev["source_type"]}] chunk={ev["chunk_id"]} '
                  f'{(ev["filename"] or "").replace(chr(10), " ")}')
            print('  ' + ev['text'][:240].replace('\n', ' '))
    print(f'\n共 {len(results)} 封郵件（已按 Email ID 合併）')
    if args.rerank and not results:
        print('找不到可由目前候選片段直接支持的相關郵件；可試著調整問題或增加索引。')


if __name__ == '__main__':
    main()
