"""Local CLI: create a user/mailbox and a per-mailbox ingest secret.

Print the secret once. Only its SHA-256 hash is stored in the database.
"""
import argparse
import secrets
from sqlalchemy import select
from app.database import SessionLocal, init_database
from app.models import EmailAccount, User
from app.routes.ingest import hash_token


def main():
    parser = argparse.ArgumentParser(description='Register a MailMind email account')
    parser.add_argument('--email', required=True, help='Gmail address belonging to this account')
    parser.add_argument('--level', default='medium', choices=['short', 'medium', 'full'])
    parser.add_argument('--rotate', action='store_true', help='Rotate token for existing account')
    args = parser.parse_args()
    addr = args.email.strip().lower()
    if '@' not in addr or ' ' in addr:
        parser.error('Please enter a valid email address')

    init_database()
    with SessionLocal() as session:
        account = session.scalar(select(EmailAccount).where(EmailAccount.address == addr))
        if account and not args.rotate:
            print('This email already exists. Use --rotate to issue a NEW token (old token stops working).')
            print('ACCOUNT_ID=' + account.id)
            return

        if account:
            user = session.get(User, account.user_id)
        else:
            user = User(summary_level=args.level)
            session.add(user)
            session.flush()
            account = EmailAccount(user_id=user.id, address=addr)
            session.add(account)
            session.flush()
        user.summary_level = args.level
        token = secrets.token_urlsafe(32)
        account.ingest_token_hash = hash_token(token)
        account_id = account.id
        session.commit()

    print('Registered successfully. Copy token to Apps Script Properties, not chat or GitHub.')
    print('ACCOUNT_ID=' + account_id)
    print('INGEST_TOKEN=' + token)
    print('SUMMARY_LEVEL=' + args.level)


if __name__ == '__main__':
    main()
