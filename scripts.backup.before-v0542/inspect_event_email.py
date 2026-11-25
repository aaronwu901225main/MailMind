"""Inspect and total verified event occurrences in ONE scoped source email.

Account-scoped SQL; explicitly ignores review and refund/reversal events.
Not a bank statement. Merchant / email subject can be sensitive data.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
from app.database import SessionLocal
from app.models import StructuredEvent
from app.services.event_schema import CURRENCY_DECIMALS
from sqlalchemy import select


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--account-id',required=True)
    p.add_argument('--email-id',required=True)
    args=p.parse_args()
    with SessionLocal() as db:
        rows=db.scalars(select(StructuredEvent).where(
            StructuredEvent.account_id==args.account_id,
            StructuredEvent.email_id==args.email_id,
        ).order_by(StructuredEvent.occurred_on,StructuredEvent.id)).all()
        totals=defaultdict(lambda:[0,0])
        for r in rows:
            print(r.validation_status,r.event_type,r.occurred_on,
                  r.currency,r.amount_minor,r.title,r.event_status)
            if (r.validation_status=='verified' and r.event_type=='transaction'
                and r.event_status in {'purchase','authorization'} and r.currency
                and r.amount_minor is not None):
                totals[r.currency][0]+=1
                totals[r.currency][1]+=r.amount_minor
        for currency,(count,minor) in sorted(totals.items()):
            places=CURRENCY_DECIMALS.get(currency,2)
            print(f'{currency}: {count} verified notified-spend occurrences; '
                  f'{minor/(10**places):,.{places}f} (not settled statement)')
        if not rows:print('沒有該封的事件；確認 ID 和是否已擷取。')

if __name__=='__main__':main()
