"""Inspect account-scoped events/aggregates without exposing SQL to an LLM."""
from __future__ import annotations

import argparse
import json
from app.database import SessionLocal, init_database
from app.models import EmailAccount
from app.services.event_analytics import (search_events, aggregate_events,
                                           get_event_detail, get_extraction_coverage)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--account-id', required=True)
    p.add_argument('--action', choices=['coverage', 'search', 'aggregate', 'detail'], default='coverage')
    p.add_argument('--event-type', choices=['transaction', 'deadline'], default='transaction')
    p.add_argument('--start-date')
    p.add_argument('--end-date')
    p.add_argument('--status', choices=['purchase', 'refund', 'reversal', 'authorization'], default='purchase')
    p.add_argument('--include-review', action='store_true')
    p.add_argument('--limit', type=int, default=10)
    p.add_argument('--event-id')
    args = p.parse_args()
    init_database()
    with SessionLocal() as db:
        if db.get(EmailAccount, args.account_id) is None:
            p.error('找不到指定 Account ID')
        if args.action == 'coverage':
            result = get_extraction_coverage(db, args.account_id)
        elif args.action == 'search':
            result = search_events(db, args.account_id, event_type=args.event_type,
                                   start_date=args.start_date, end_date=args.end_date,
                                   include_review=args.include_review, limit=args.limit)
        elif args.action == 'detail':
            if not args.event_id:
                p.error('detail 需要 --event-id')
            result = get_event_detail(db, args.account_id, args.event_id)
        else:
            if not args.start_date or not args.end_date:
                p.error('aggregate 需要 --start-date 和 --end-date')
            result = aggregate_events(db, args.account_id,
                       event_type=args.event_type, start_date=args.start_date,
                       end_date=args.end_date, status=args.status)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
