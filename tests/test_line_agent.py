"""v0.5.3 offline smoke & security tests: no outbound LINE/Gemini/MCP traffic."""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import EmailAccount, EmailMessage, KnowledgeChunk, LineAgentJob, User
from app.routes import line_agent
from app.services.agent_privacy import redact_for_cloud
from app.services.line_agent_queue import enqueue_question, extract_question, quota_ok
from app.services import line_agent_worker as worker


@pytest.fixture
def store(tmp_path):
    e = create_engine('sqlite:///' + str(tmp_path / 'agent.db'), connect_args={'check_same_thread': False})
    Base.metadata.create_all(e)
    S = sessionmaker(bind=e)
    with S() as db:
        users = [User(line_user_id='LINE-1'), User(line_user_id='LINE-2')]
        db.add_all(users)
        db.flush()
        accounts = [EmailAccount(user_id=u.id, address=f'a{i}@example.com') for i, u in enumerate(users)]
        db.add_all(accounts)
        db.commit()
        ids = [acc.id for acc in accounts]
    yield S, ids
    e.dispose()


def test_command_requires_prefix():
    assert extract_question('查郵件 PDF 辨識失敗') == 'PDF 辨識失敗'
    assert extract_question('問郵件：上周的報告') == '上周的報告'
    assert extract_question('查郵件') == ''
    assert extract_question('查郵件　PDF') == 'PDF'
    assert extract_question('完整') is None
    assert extract_question('我的設定') is None
    assert extract_question('查郵件是不是改了') is None


def test_queue_binds_only_owner_and_dedupes(store):
    S, (a, b) = store
    with patch('app.services.line_agent_queue.AGENT_MONTHLY_PUSH_LIMIT', 20):
        with S() as db:
            status, job_id = enqueue_question(db, line_user_id='LINE-1', event_id='evt1', question='PDF 資料')
            assert status == 'queued' and job_id
            again, _ = enqueue_question(db, line_user_id='LINE-1', event_id='evt1', question='不一樣')
            assert again == 'duplicate'
            row = db.get(LineAgentJob, job_id)
            assert row.account_id == a
            assert row.account_id != b
            assert row.question == 'PDF 資料'
            assert enqueue_question(db, line_user_id='UNKNOWN', event_id='evt2', question='PDF')[0] == 'unbound'


def test_queue_rejects_ambiguous_accounts(store):
    S, (a, b) = store
    with S() as db:
        owner = db.get(EmailAccount, a).user_id
        db.add(EmailAccount(user_id=owner, address='other@example.com'))
        db.commit()
        assert enqueue_question(db, line_user_id='LINE-1', event_id='evt3', question='PDF')[0] == 'ambiguous'


def test_queue_limits_and_local_quota(store):
    S, _ = store
    with patch('app.services.line_agent_queue.AGENT_DAILY_LIMIT', 1):
        with S() as db:
            assert enqueue_question(db, line_user_id='LINE-1', event_id='ev4', question='X')[0] == 'queued'
            assert enqueue_question(db, line_user_id='LINE-1', event_id='ev5', question='Y')[0] == 'limited'
    with patch('app.services.line_agent_queue.AGENT_MONTHLY_PUSH_LIMIT', 0):
        with S() as db:
            assert not quota_ok(db)


def _event(text='查郵件 PDF 報告', msgid='message-1', user='LINE-1'):
    return {'type': 'message', 'webhookEventId': 'event-' + msgid,
            'replyToken': 'REPLY-' + msgid,
            'source': {'type': 'user', 'userId': user},
            'message': {'type': 'text', 'id': msgid, 'text': text}}


def test_webhook_rejects_bad_signature(store):
    from app.main import app
    with patch.object(line_agent, 'LINE_CHANNEL_SECRET', 'secret'):
        response = TestClient(app).post('/line/agent/webhook', content=b'{"events":[]}',
                                        headers={'X-Line-Signature': 'wrong'})
    assert response.status_code == 401


def test_webhook_enqueues_and_preserves_old_commands(store):
    from app.main import app
    S, _ = store
    acknowledgements, forwarded = [], []
    async def fake_reply(token, text):
        acknowledgements.append((token, text))
    async def fake_forward(events, **kwargs):
        forwarded.extend(events)
    payload = json.dumps({'events': [_event(), _event('完整', 'message-2')]}, ensure_ascii=False).encode()
    with patch.object(line_agent, 'SessionLocal', S), \
         patch.object(line_agent, 'LINE_CHANNEL_SECRET', 'secret'), \
         patch.object(line_agent, 'AGENT_ENABLED', True), \
         patch.object(line_agent, 'AGENT_ALLOW_CLOUD_EMAIL', True), \
         patch.object(line_agent, 'reply_line', fake_reply), \
         patch.object(line_agent, 'forward_legacy', fake_forward):
        sig = line_agent.sign_payload(payload, 'secret')
        response = TestClient(app).post('/line/agent/webhook', content=payload,
                                        headers={'X-Line-Signature': sig})
        # Second post simulates LINE's at-least-once delivery.
        again = TestClient(app).post('/line/agent/webhook', content=payload,
                                     headers={'X-Line-Signature': sig})
    assert response.status_code == again.status_code == 200
    assert len(forwarded) == 2 and all(e['message']['text'] == '完整' for e in forwarded)
    assert len(acknowledgements) == 1 and '已收到' in acknowledgements[0][1]
    with S() as db:
        assert len(db.scalars(select(LineAgentJob)).all()) == 1


def test_webhook_does_not_queue_without_explicit_privacy_consent(store):
    from app.main import app
    S, _ = store
    replies = []
    async def fake_reply(token, text):
        replies.append(text)
    payload = json.dumps({'events': [_event()]}, ensure_ascii=False).encode()
    with patch.object(line_agent, 'SessionLocal', S), \
         patch.object(line_agent, 'LINE_CHANNEL_SECRET', 'secret'), \
         patch.object(line_agent, 'AGENT_ENABLED', True), \
         patch.object(line_agent, 'AGENT_ALLOW_CLOUD_EMAIL', False), \
         patch.object(line_agent, 'reply_line', fake_reply):
        sig = line_agent.sign_payload(payload, 'secret')
        assert TestClient(app).post('/line/agent/webhook', content=payload,
                                    headers={'X-Line-Signature': sig}).status_code == 200
    assert '尚未啟用' in replies[0]
    with S() as db:
        assert db.scalars(select(LineAgentJob)).all() == []


def test_mcp_decode_and_error_rejection():
    good = SimpleNamespace(is_error=False, content=[
        SimpleNamespace(type='text', text='{"email_id":"x","relevance":"relevant"}'),
        SimpleNamespace(type='text', text='[{"email_id":"y"}]'),
    ])
    assert [r['email_id'] for r in worker.decode_tool_result(good)] == ['x', 'y']
    with pytest.raises(RuntimeError, match='mcp_tool_error'):
        worker.decode_tool_result(SimpleNamespace(is_error=True, content=[]))


def test_worker_refetches_account_scoped_chunks(store):
    S, (a, b) = store
    def insert(db, account, msgid, content):
        mail = EmailMessage(account_id=account, provider_message_id=msgid,
                            subject=msgid, state='completed')
        db.add(mail); db.flush()
        chunk = KnowledgeChunk(account_id=account, email_id=mail.id,
                               source_type='body', source_id=mail.id,
                               chunk_index=0, chunk_text=content,
                               content_hash=hashlib.sha256(content.encode()).hexdigest())
        db.add(chunk); db.commit()
        return mail.id, chunk.id
    with S() as db:
        my_eid, my_chunk = insert(db, a, 'PDF Test', '掃描 PDF 因無 OCR 無法讀取')
        other_eid, other_chunk = insert(db, b, 'Private', '別人的機密資訊')
    spoof = [
        {'email_id': other_eid, 'relevance': 'relevant', 'supporting_chunk_ids': [other_chunk]},
        {'email_id': my_eid, 'relevance': 'unjudged', 'supporting_chunk_ids': [my_chunk]},
        {'email_id': my_eid, 'relevance': 'relevant', 'supporting_chunk_ids': [999]},
        {'email_id': my_eid, 'relevance': 'relevant', 'supporting_chunk_ids': [my_chunk],
         'evidence': [{'text': '模型偽造的內容'}]},
    ]
    with patch.object(worker, 'SessionLocal', S):
        result = worker.validate_evidence(a, spoof)
    assert len(result) == 1
    assert 'OCR' in result[0]['evidence'][0]['text']
    assert '模型偽造' not in str(result)
    assert '機密' not in str(result)


def test_privacy_redacts_unsubscribe_tokens_and_credentials():
    sample = 'https://mail.test/unsubscribe?token=VERYSECRET api_key=ABC1234567 user@abc.com 0912345678'
    cleaned = redact_for_cloud(sample)
    assert 'VERYSECRET' not in cleaned
    assert 'ABC1234567' not in cleaned
    assert 'user@abc.com' not in cleaned
    assert '0912345678' not in cleaned


def test_worker_end_to_end_without_external_calls(store):
    S, (a, _) = store
    with S() as db:
        status, job_id = enqueue_question(db, line_user_id='LINE-1', event_id='worker-job', question='有 OCR 嗎')
        assert status == 'queued'
        email = EmailMessage(account_id=a, provider_message_id='ocr', subject='測試', state='completed')
        db.add(email); db.flush()
        chunk = KnowledgeChunk(account_id=a, email_id=email.id, source_type='body',
                               source_id=email.id, chunk_index=0,
                               chunk_text='掃描 PDF 因 OCR 未安裝而失敗', content_hash='abc')
        db.add(chunk); db.commit()
        eid, cid = email.id, chunk.id
    calls = []
    async def fake_retrieve(account_id, question):
        calls.append(('mcp', account_id))
        return [{'email_id': eid, 'relevance': 'relevant', 'supporting_chunk_ids': [cid]}]
    def fake_answer(question, trusted):
        assert 'OCR' in trusted[0]['evidence'][0]['text']
        return '掃描文件缺少 OCR。[1]'
    async def fake_send(line_user_id, id, answer):
        calls.append(('line', answer))
    with patch.object(worker, 'SessionLocal', S), \
         patch.object(worker, 'AGENT_ENABLED', True), \
         patch.object(worker, 'AGENT_ALLOW_CLOUD_EMAIL', True):
        assert asyncio.run(worker.process_one_job(retriever=fake_retrieve,
                                                   answerer=fake_answer,
                                                   sender=fake_send))
    assert len(calls) == 2
    with S() as db:
        job = db.get(LineAgentJob, job_id)
        assert job.status == 'sent'
        assert job.result_text is None  # minimize retention after delivery
        assert '清除' in job.question


def test_worker_failure_is_pending_with_backoff(store):
    S, _ = store
    with S() as db:
        status, job_id = enqueue_question(db, line_user_id='LINE-1', event_id='error-job', question='test')
        assert status == 'queued'
    async def err(*args):
        raise RuntimeError('mcp_tool_error')
    with patch.object(worker, 'SessionLocal', S), \
         patch.object(worker, 'AGENT_ENABLED', True), \
         patch.object(worker, 'AGENT_ALLOW_CLOUD_EMAIL', True):
        assert asyncio.run(worker.process_one_job(retriever=err))
    with S() as db:
        job = db.get(LineAgentJob, job_id)
        assert job.status == 'pending'
        assert job.attempts == 1
        assert job.next_attempt_at > datetime.now(timezone.utc).replace(tzinfo=None)


def test_legacy_forward_signs_modified_event_body():
    captured = {}
    class FakeAsyncClient:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, content, headers):
            captured.update(url=url, content=content, headers=headers)
            return SimpleNamespace(raise_for_status=lambda: None)
    event = _event('完整')
    with patch.object(line_agent, 'LINE_CHANNEL_SECRET', 'secret'), \
         patch('app.routes.line_agent.httpx.AsyncClient', FakeAsyncClient):
        asyncio.run(line_agent.forward_legacy([event]))
    assert line_agent.verify_payload(captured['content'],
                                     captured['headers']['X-Line-Signature'], 'secret')
    assert json.loads(captured['content'])['events'] == [event]
