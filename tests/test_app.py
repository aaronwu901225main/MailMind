from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session
from app.main import app
from app.database import Base
from app.models import User, EmailAccount, EmailMessage
from app.services.summarizer import build_prompt


def test_health():
    with TestClient(app) as client:
        response = client.get('/health')
    assert response.status_code == 200
    assert response.json()['status'] == 'ok'


def test_prompt_has_three_levels():
    for level in ('short', 'medium', 'full'):
        prompt = build_prompt('test@example.com', '專案通知', '繳交測試報告', level)
        assert '專案通知' in prompt
        assert '繳交測試報告' in prompt
        assert '<email>' in prompt


def test_tables_and_user_separation(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'test.db'))
    Base.metadata.create_all(engine)
    assert {'users', 'email_accounts', 'emails', 'summaries', 'delivery_logs'} <= set(inspect(engine).get_table_names())
    with Session(engine) as session:
        alice = User(line_user_id='Ualice', summary_level='short')
        bob = User(line_user_id='Ubob', summary_level='full')
        session.add_all([alice, bob])
        session.flush()
        account_a = EmailAccount(user_id=alice.id, address='alice@example.com')
        account_b = EmailAccount(user_id=bob.id, address='bob@example.com')
        session.add_all([account_a, account_b])
        session.flush()
        session.add_all([
            EmailMessage(account_id=account_a.id, provider_message_id='same-id', subject='Alice 郵件'),
            EmailMessage(account_id=account_b.id, provider_message_id='same-id', subject='Bob 郵件'),
        ])
        session.commit()
        assert alice.id != bob.id
        assert alice.summary_level == 'short' and bob.summary_level == 'full'
    engine.dispose()
