"""Evaluate hand-labeled email search questions with Recall@K and MRR@K.

JSONL example:
{"query":"哪封信有PDF解析失敗的原因", "relevant_email_ids":["<uuid>"]}
Do not publish real mailbox queries, email IDs or result sets.
"""
import argparse
import json
from pathlib import Path

from app.database import SessionLocal, init_database
from app.models import EmailAccount
from app.services.hybrid_search import hybrid_search


def metrics(questions: list[dict], ranked_ids: list[list[str]], k: int) -> dict:
    # Rankings are chunk-level. Evaluate unique-email order to avoid penalizing
    # emails having multiple body/summary/attachment chunks.
    recalls, reciprocals = [], []
    for question, ids in zip(questions, ranked_ids):
        gold = set(question['relevant_email_ids'])
        if not gold:
            raise ValueError('Every question must have at least one relevant_email_id')
        unique = list(dict.fromkeys(ids))[:k]
        recalls.append(len(set(unique) & gold) / len(gold))
        reciprocals.append(next((1 / i for i, item in enumerate(unique, 1) if item in gold), 0))
    return {'recall_at_k': sum(recalls) / len(recalls),
            'mrr_at_k': sum(reciprocals) / len(reciprocals)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--account-id', required=True)
    parser.add_argument('--questions', required=True, help='Local JSONL with query + relevant_email_ids')
    parser.add_argument('--k', type=int, default=5)
    parser.add_argument('--semantic', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.k <= 10:
        parser.error('--k must be 1..10')
    data = []
    for line in Path(args.questions).read_text(encoding='utf-8').splitlines():
        if line.strip():
            item = json.loads(line)
            if not isinstance(item.get('query'), str) or not isinstance(item.get('relevant_email_ids'), list):
                parser.error('Bad JSONL: each line needs query and relevant_email_ids')
            data.append(item)
    if not data:
        parser.error('No questions found')
    init_database()
    with SessionLocal() as db:
        if not db.get(EmailAccount, args.account_id):
            parser.error('Unknown account')
        # Request more chunk results than k to allow multiple hits from one email.
        results = [hybrid_search(db, args.account_id, q['query'],
                                 limit=min(20, args.k * 4), use_semantic=args.semantic)
                   for q in data]
    score = metrics(data, [[row['email_id'] for row in hits] for hits in results], args.k)
    mode = 'hybrid' if args.semantic else 'FTS-only'
    print(f'{mode}: {len(data)} questions; Recall@{args.k}={score["recall_at_k"]:.3f}, '
          f'MRR@{args.k}={score["mrr_at_k"]:.3f}')


if __name__ == '__main__':
    main()
