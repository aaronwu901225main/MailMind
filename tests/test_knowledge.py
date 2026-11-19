"""v0.5.1 FTS integration, incremental indexing, and multi-account isolation."""
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import EmailAccount, EmailAttachment, EmailMessage, KnowledgeChunk, Summary, User
from app.services.knowledge_index import index_email, split_text
from app.services.knowledge_search import ensure_knowledge_fts, search_chunks
from app import mcp_tools


@pytest.fixture
def store(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'test.db'))
    Base.metadata.create_all(engine)
    assert ensure_knowledge_fts(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        users = [User(), User()]
        db.add_all(users)
        db.flush()
        accounts = [EmailAccount(user_id=u.id, address=f'{i}@example.com')
                    for i, u in enumerate(users)]
        db.add_all(accounts)
        db.commit()
        aids = [a.id for a in accounts]
    yield engine, Session, aids
    engine.dispose()


def create_mail(db, account_id, msg_id, subject, summary):
    mail = EmailMessage(account_id=account_id, provider_message_id=msg_id,
                        subject=subject, sender='test@example.com', state='completed')
    db.add(mail)
    db.flush()
    db.add(Summary(email_id=mail.id, summary_level='short', model_name='test', content=summary))
    db.flush()
    return mail


def test_split_bounded_and_overlap():
    chunks = split_text('繁體中文' * 700)
    assert len(chunks) >= 3
    assert all(0 < len(c) <= 900 for c in chunks)
    assert chunks[0][-100:] in chunks[1]


def test_index_search_and_backfill_idempotency(store):
    _, S, (a, _) = store
    with S() as db:
        mail = create_mail(db, a, 'a1', '專案進度', '附件成功率為96.0%，低於目標98%')
        attach = EmailAttachment(email_id=mail.id, position=0, filename='測試.pdf',
                                 mime_type='application/pdf', size_bytes=100,
                                 status='extracted', extracted_text='ATTACH-VERIFY-8294 PDF 報告')
        db.add(attach)
        db.flush()
        mid = mail.id
        num = index_email(db, mail, body='本週研究進度：資料清洗已完成')
        db.commit()
        assert num >= 4
    with S() as db:
        assert 'ATTACH-VERIFY-8294' in search_chunks(db, a, 'ATTACH-VERIFY-8294')[0]['snippet']
        assert '資料清洗' in search_chunks(db, a, '資料清洗')[0]['snippet']
        assert '成功率' in search_chunks(db, a, '成功率')[0]['snippet']
        before = db.scalar(select(func.count()).select_from(KnowledgeChunk))
        assert index_email(db, db.get(EmailMessage, mid), body=None) == 0
        db.commit()
        assert db.scalar(select(func.count()).select_from(KnowledgeChunk)) == before
        assert any(r['source_type']=='body' for r in search_chunks(db,a,'資料清洗'))


def test_short_queries_special_chars_and_account_isolation(store):
    _, S, (a, b) = store
    with S() as db:
        ma = create_mail(db, a, '1', 'AI 模型', '機密：參數為 {{RAG}}')
        mb = create_mail(db, b, '2', '私密評測', 'ATTACH-VERIFY-8294')
        index_email(db, ma, body='AI 專案週報')
        index_email(db, mb, body='跨帳號極機密：資料未公開')
        db.commit()
    with S() as db:
        assert search_chunks(db, a, 'AI')  # trigram short-query LIKE fallback
        assert search_chunks(db, a, '{{RAG}}')
        assert not search_chunks(db, a, 'ATTACH-VERIFY-8294')
        assert not search_chunks(db, b, 'AI 專案週報')
        assert not search_chunks(db, a, '" OR NOT 1 --')
    with patch.object(mcp_tools, 'SessionLocal', S), patch.object(mcp_tools, 'MCP_ACCOUNT_ID', a):
        assert mcp_tools.search_knowledge('機密')
        assert mcp_tools.search_knowledge('跨帳號極機密') == []


def test_fts_triggers_update_and_delete(store):
    _, S, (a, _) = store
    with S() as db:
        mail = create_mail(db, a, '1', '附件', '原始內容 ALPHAONLY')
        index_email(db, mail)
        mail_id = mail.id
        db.commit()
    with S() as db:
        assert search_chunks(db, a, 'ALPHAONLY')
        summary = db.scalar(select(Summary).where(Summary.email_id == mail_id))
        summary.content = '更新內容 BRAVOONLY'
        db.flush()
        index_email(db, db.get(EmailMessage, mail_id))
        db.commit()
    with S() as db:
        assert not search_chunks(db, a, 'ALPHAONLY')
        assert search_chunks(db, a, 'BRAVOONLY')


def test_real_ingest_indexes_body_and_keeps_duplicate_guard(store):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.routes.ingest import get_db, hash_token

    _, S, (a, _) = store
    def override_db():
        with S() as db:
            yield db
    app.dependency_overrides[get_db] = override_db
    try:
        with S() as db:
            account = db.get(EmailAccount, a)
            account.ingest_token_hash = hash_token('secret-test-token')
            db.commit()
        data = {
            'account_id': a, 'message_id': 'live-index-001',
            'subject': '版本整合', 'sender': 'qa@example.com',
            'body': '這是新郵件的原始內文：獨有驗收字串 BODY-7319，未包含於摘要。',
        }
        with patch('app.routes.ingest.summarize_with_gemini', return_value='完成摘要') as model:
            with TestClient(app) as c:
                headers = {'X-MailMind-Token': 'secret-test-token'}
                first = c.post('/api/v1/ingest/gmail', json=data, headers=headers)
                assert first.status_code == 200, first.text
                assert first.json()['status'] == 'completed'
                repeated = c.post('/api/v1/ingest/gmail', json=data, headers=headers)
                assert repeated.status_code == 200
                assert repeated.json()['status'] == 'duplicate'
                assert model.call_count == 1
        with S() as db:
            results = search_chunks(db, a, 'BODY-7319')
            assert results
            assert results[0]['source_type'] == 'body'
    finally:
        app.dependency_overrides.clear()


def test_fts_missing_gracefully_falls_back_to_like(store):
    engine, S, (a, _) = store
    with S() as db:
        mail = create_mail(db, a, 'fallback-1', '全文搜尋', '三字中文詞彙 備援處理')
        index_email(db, mail)
        db.commit()
    with engine.begin() as conn:
        for name in ('knowledge_chunks_ai','knowledge_chunks_ad','knowledge_chunks_au'):
            conn.exec_driver_sql(f'DROP TRIGGER {name}')
        conn.exec_driver_sql('DROP TABLE knowledge_fts')
    with S() as db:
        result = search_chunks(db, a, '備援處理')
        assert result and '備援處理' in result[0]['snippet']
