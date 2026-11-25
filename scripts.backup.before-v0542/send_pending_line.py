"""Push newly summarized mail to bound LINE users, honoring free-tier budget.

Run manually (--once) or at intervals (--loop). Run only one worker per database.
"""
import argparse
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import MAX_LINE_PUSHES_PER_MONTH
from app.database import SessionLocal, init_database
from app.models import DeliveryLog, EmailAccount, EmailMessage, Summary, User
from app.services.line_messaging import push_summary, quota_remaining, render_summary

log = logging.getLogger('mailmind.delivery')


def create_pending(db: Session):
    # Existing mail was explicitly marked 'skipped' during binding.
    candidates = db.execute(
        select(EmailMessage.id, User.id)
        .join(EmailAccount, EmailMessage.account_id == EmailAccount.id)
        .join(User, EmailAccount.user_id == User.id)
        .join(Summary, Summary.email_id == EmailMessage.id)
        .where(EmailMessage.state == 'completed',
               User.line_user_id.is_not(None),
               User.notifications_enabled.is_(True))
    ).all()
    existing = set(db.execute(select(DeliveryLog.email_id, DeliveryLog.user_id)
                              .where(DeliveryLog.channel == 'line')).all())
    for email_id, user_id in candidates:
        if (email_id, user_id) not in existing:
            db.add(DeliveryLog(email_id=email_id, user_id=user_id,
                               channel='line', status='pending'))
    db.commit()


def count_current_month(db: Session) -> int:
    now = datetime.now(timezone.utc)
    month = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    return db.scalar(select(func.count(DeliveryLog.id)).where(
        DeliveryLog.channel == 'line',
        DeliveryLog.status == 'sent',
        DeliveryLog.created_at >= month)) or 0


def send_once(limit: int = 10, dry_run: bool = False):
    with SessionLocal() as db:
        if not dry_run:
            create_pending(db)
        # Dry run reports what will be sent without mutating delivery logs.
        if dry_run:
            q = (select(EmailMessage.subject)
                 .join(EmailAccount, EmailMessage.account_id == EmailAccount.id)
                 .join(User, EmailAccount.user_id == User.id)
                 .join(Summary, Summary.email_id == EmailMessage.id)
                 .where(User.line_user_id.is_not(None), User.notifications_enabled.is_(True),
                        ~select(DeliveryLog.id).where(
                            DeliveryLog.email_id == EmailMessage.id,
                            DeliveryLog.user_id == User.id,
                            DeliveryLog.channel == 'line').exists())
                 .limit(limit))
            for subject in db.scalars(q):
                print('PENDING:', subject[:80])
            return

        ids = db.scalars(select(DeliveryLog.id).where(
            DeliveryLog.channel == 'line',
            DeliveryLog.status.in_(['pending', 'failed', 'sending']),
            DeliveryLog.attempt_count < 3).order_by(DeliveryLog.created_at).limit(limit)).all()
        for log_id in ids:
            record = db.get(DeliveryLog, log_id)
            user = db.get(User, record.user_id)
            email = db.get(EmailMessage, record.email_id)
            summary = db.scalar(select(Summary).where(Summary.email_id == record.email_id))
            if not user or not user.line_user_id or not user.notifications_enabled or not email or not summary:
                record.status = 'skipped'
                db.commit()
                continue
            if count_current_month(db) >= MAX_LINE_PUSHES_PER_MONTH:
                log.warning('Local LINE monthly cap reached. Not sending.')
                return
            try:
                left = quota_remaining()
                if left is not None and left <= 0:
                    log.warning('LINE official-account monthly quota exhausted. Not sending.')
                    return
                text = render_summary(email.sender, email.subject, summary.content, summary.summary_level)
                record.status = 'sending'
                record.attempt_count += 1
                db.commit()
                push_summary(user.line_user_id, text, record.id)
                record.status = 'sent'
                db.commit()
                log.info('LINE push accepted: email=%s', record.email_id)
            except Exception:
                # Do not print sender/subject or API tokens in logs.
                record.status = 'failed'
                db.commit()
                log.exception('LINE push failed (delivery=%s), retry on next cycle', log_id)
                # A credential/quota outage is unlikely to resolve within this run.
                return


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--once', action='store_true', help='Run one cycle (default)')
    p.add_argument('--loop', action='store_true', help='Run forever at interval')
    p.add_argument('--interval', type=int, default=300)
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()
    if args.interval < 60:
        p.error('interval must be >= 60 seconds')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    init_database()
    while True:
        send_once(dry_run=args.dry_run)
        if not args.loop or args.dry_run:
            return
        time.sleep(args.interval)


if __name__ == '__main__':
    main()
