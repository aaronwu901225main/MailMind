"""Issue a one-time LINE pairing code for an existing Gmail account."""
import argparse
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from app.database import SessionLocal, init_database
from app.models import EmailAccount, LineLinkCode


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--email', required=True)
    args = parser.parse_args()
    init_database()
    address = args.email.strip().lower()
    with SessionLocal() as db:
        account = db.scalar(select(EmailAccount).where(EmailAccount.address == address))
        if account is None:
            parser.error('Gmail account not registered. Use scripts.create_account first.')
        now = datetime.now(timezone.utc)
        for prior in db.scalars(select(LineLinkCode).where(
                LineLinkCode.user_id == account.user_id, LineLinkCode.consumed_at.is_(None))):
            prior.expires_at = now
        code = secrets.token_urlsafe(9)
        db.add(LineLinkCode(user_id=account.user_id,
                            code_hash=hashlib.sha256(code.encode()).hexdigest(),
                            expires_at=now + timedelta(minutes=15)))
        db.commit()
    print('請在 15 分鐘內私訊 MailMind LINE 官方帳號：')
    print('綁定 ' + code)
    print('請勿把此代碼貼到公開聊天室；此代碼只能使用一次。')


if __name__ == '__main__':
    main()
