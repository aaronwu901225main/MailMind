"""Run separately from existing email-summary LINE worker.

python -m scripts.agent_worker --once --dry-run
python -m scripts.agent_worker --loop --interval 15
"""
import argparse
import asyncio
import logging

from sqlalchemy import func, select
from app.database import SessionLocal, init_database
from app.models import LineAgentJob
from app.services.line_agent_worker import process_one_job


def main():
    ap = argparse.ArgumentParser()
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument('--once', action='store_true')
    group.add_argument('--loop', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--interval', type=int, default=15)
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    init_database()
    if args.dry_run:
        with SessionLocal() as db:
            count = db.scalar(select(func.count(LineAgentJob.id)).where(LineAgentJob.status == 'pending')) or 0
        print(f'✅ LINE Agent 佇列可讀；pending={count}；沒有呼叫 Gemini/LINE')
        return
    if args.once:
        print('已處理一個工作' if asyncio.run(process_one_job()) else '目前沒有待處理工作')
        return
    while True:
        did_work = asyncio.run(process_one_job())
        if not did_work:
            import time
            time.sleep(max(5, min(args.interval, 300)))


if __name__ == '__main__':
    main()
