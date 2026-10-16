from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.main import app
from app.database import Base
from app.models import EmailAccount, EmailMessage, Summary, User
from app.routes.ingest import get_db, hash_token


def test_ingest_auth_dedup_and_user_preference():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    testing_session = sessionmaker(bind=engine)
    with testing_session() as db:
        a = User(summary_level='short')
        b = User(summary_level='full')
        db.add_all([a, b])
        db.flush()
        acc_a = EmailAccount(user_id=a.id, address='alice@example.com', ingest_token_hash=hash_token('secret-a'))
        acc_b = EmailAccount(user_id=b.id, address='bob@example.com', ingest_token_hash=hash_token('secret-b'))
        db.add_all([acc_a, acc_b])
        db.commit()
        account_a_id = acc_a.id
        account_b_id = acc_b.id

    def override_db():
        with testing_session() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    body = dict(account_id=account_a_id, message_id='same-gmail-msg', sender='p@example.com',
                subject='Hello', body='Some text')
    try:
        with patch('app.routes.ingest.summarize_with_gemini', return_value='摘要完成') as mock_summary:
            with TestClient(app) as client:
                assert client.post('/api/v1/ingest/gmail', json=body).status_code == 401
                assert client.post('/api/v1/ingest/gmail', json=body,
                                   headers={'X-MailMind-Token': 'wrong'}).status_code == 401
                first = client.post('/api/v1/ingest/gmail', json=body,
                                    headers={'X-MailMind-Token': 'secret-a'})
                assert first.status_code == 200, first.text
                assert first.json()['status'] == 'completed'
                assert mock_summary.call_args.args[-1] == 'short'
                second = client.post('/api/v1/ingest/gmail', json=body,
                                     headers={'X-MailMind-Token': 'secret-a'})
                assert second.json()['status'] == 'duplicate'
                assert mock_summary.call_count == 1
                body['account_id'] = account_b_id
                assert client.post('/api/v1/ingest/gmail', json=body,
                                   headers={'X-MailMind-Token': 'secret-a'}).status_code == 401
                second_user = client.post('/api/v1/ingest/gmail', json=body,
                                          headers={'X-MailMind-Token': 'secret-b'})
                assert second_user.status_code == 200
                assert mock_summary.call_count == 2
                assert mock_summary.call_args.args[-1] == 'full'
        with testing_session() as db:
            assert len(db.scalars(select(EmailMessage)).all()) == 2
            assert len(db.scalars(select(Summary)).all()) == 2
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_ingest_retries_after_provider_error():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    testing_session = sessionmaker(bind=engine)
    with testing_session() as db:
        user = User()
        db.add(user)
        db.flush()
        account = EmailAccount(user_id=user.id, address='a@example.com', ingest_token_hash=hash_token('token'))
        db.add(account)
        db.commit()
        account_id = account.id
    def override_db():
        with testing_session() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    data = {'account_id': account_id, 'message_id': 'retry', 'body': 'demo'}
    try:
        with TestClient(app) as client:
            with patch('app.routes.ingest.summarize_with_gemini', side_effect=RuntimeError('offline')):
                failed = client.post('/api/v1/ingest/gmail', json=data, headers={'X-MailMind-Token': 'token'})
                assert failed.status_code == 502
            with patch('app.routes.ingest.summarize_with_gemini', return_value='Recovered'):
                ok = client.post('/api/v1/ingest/gmail', json=data, headers={'X-MailMind-Token': 'token'})
                assert ok.status_code == 200
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
