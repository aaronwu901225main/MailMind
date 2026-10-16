import base64
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.main import app
from app.models import DeliveryLog, EmailAccount, EmailMessage, LineLinkCode, Summary, User
from app.routes.ingest import get_db
from app.routes.line_webhook import handle_text
from scripts.send_pending_line import create_pending


def setup():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return engine, Session


def test_linking_skips_old_mail_and_changes_preferences():
    engine, S = setup()
    now = datetime.now(timezone.utc)
    with S() as db:
        u = User()
        db.add(u)
        db.flush()
        acc = EmailAccount(user_id=u.id, address='test@example.com')
        db.add(acc)
        db.flush()
        old = EmailMessage(account_id=acc.id, provider_message_id='old', state='completed', subject='舊信')
        db.add(old)
        db.flush()
        db.add(Summary(email_id=old.id, summary_level='medium', model_name='test', content='old summary'))
        db.add(LineLinkCode(user_id=u.id, code_hash=hashlib.sha256(b'example-code').hexdigest(),
                            expires_at=now+timedelta(minutes=15)))
        db.commit()
        user_id, acc_id, old_id = u.id, acc.id, old.id
    with S() as db:
        assert handle_text(db, 'Utest', '綁定 example-code').startswith('✅')
        assert handle_text(db, 'Utest', '綁定 example-code').startswith('綁定碼無效')
        assert handle_text(db, 'Utest', '極簡').startswith('✅')
        assert db.get(User, user_id).summary_level == 'short'
        assert db.scalar(select(DeliveryLog).where(DeliveryLog.email_id == old_id)).status == 'skipped'
        new = EmailMessage(account_id=acc_id, provider_message_id='new', state='completed', subject='新信')
        db.add(new)
        db.flush()
        db.add(Summary(email_id=new.id, summary_level='short', model_name='test', content='new summary'))
        db.commit()
        new_id = new.id
    with S() as db:
        create_pending(db)
        assert db.scalar(select(DeliveryLog).where(DeliveryLog.email_id == new_id)).status == 'pending'
        create_pending(db)
        assert len(db.scalars(select(DeliveryLog)).all()) == 2
    engine.dispose()


def test_invalid_webhook_signature_denied():
    engine, S = setup()
    def override():
        with S() as db:
            yield db
    app.dependency_overrides[get_db] = override
    try:
        with TestClient(app) as client:
            r = client.post('/line/webhook', json={'events': []})
            assert r.status_code == 401
            with patch('app.routes.line_webhook.LINE_CHANNEL_SECRET', 'line-test-secret'):
                raw = b'{"events":[]}'
                sig = base64.b64encode(hmac.new(b'line-test-secret', raw, hashlib.sha256).digest()).decode()
                r = client.post('/line/webhook', content=raw,
                                headers={'X-Line-Signature': sig, 'Content-Type': 'application/json'})
                assert r.status_code == 200
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_expired_binding_rejected_and_private_commands():
    engine, S = setup()
    with S() as db:
        user = User()
        db.add(user)
        db.flush()
        db.add(LineLinkCode(user_id=user.id, code_hash=hashlib.sha256(b'expired').hexdigest(),
                            expires_at=datetime.now(timezone.utc)-timedelta(minutes=1)))
        db.commit()
    with S() as db:
        assert '過期' in handle_text(db, 'Uanother', '綁定 expired')
        assert '尚未綁定' in handle_text(db, 'Uanother', '我的設定')
    engine.dispose()


def test_worker_push_once_and_no_duplicate():
    engine, S = setup()
    with S() as db:
        user = User(line_user_id='Uline_recipient')
        db.add(user)
        db.flush()
        acc = EmailAccount(user_id=user.id, address='linked@example.com')
        db.add(acc)
        db.flush()
        email = EmailMessage(account_id=acc.id, provider_message_id='test01', state='completed',
                             subject='Meeting', sender='someone@example.com')
        db.add(email)
        db.flush()
        db.add(Summary(email_id=email.id, summary_level='medium', model_name='test', content='Summary'))
        db.commit()
    with patch('scripts.send_pending_line.SessionLocal', S), \
         patch('scripts.send_pending_line.quota_remaining', return_value=180), \
         patch('scripts.send_pending_line.push_summary', return_value=True) as push:
        from scripts.send_pending_line import send_once
        send_once()
        send_once()
        assert push.call_count == 1
        assert push.call_args.args[0] == 'Uline_recipient'
    with S() as db:
        record = db.scalar(select(DeliveryLog))
        assert record.status == 'sent'
        assert record.attempt_count == 1
    engine.dispose()
