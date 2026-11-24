"""Offline v0.5.2.1 tests: no real Gemini / downloads / personal mailbox."""
from __future__ import annotations

import hashlib
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import EmailAccount, EmailMessage, KnowledgeChunk, User
from app.services.knowledge_search import ensure_knowledge_fts
from app.services.relevance_search import (
    lexical_variants, search_relevant_emails, group_by_email,
)
from app import mcp_tools


@pytest.fixture
def store(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'relevance.db'))
    Base.metadata.create_all(engine)
    assert ensure_knowledge_fts(engine)
    S = sessionmaker(bind=engine)
    with S() as db:
        users = [User(), User()]
        db.add_all(users)
        db.flush()
        accounts = [EmailAccount(user_id=u.id, address=f'case{i}@example.com')
                    for i, u in enumerate(users)]
        db.add_all(accounts)
        db.commit()
        account_ids = [a.id for a in accounts]
    yield S, account_ids
    engine.dispose()


def add_chunks(db, account_id, msgid, subject, parts):
    mail = EmailMessage(account_id=account_id, provider_message_id=msgid,
                        sender='qa@example.com', subject=subject, state='completed')
    db.add(mail)
    db.flush()
    ids = []
    for source, content in parts:
        chunk = KnowledgeChunk(
            account_id=account_id, email_id=mail.id, source_type=source,
            source_id=mail.id, chunk_index=0, chunk_text=content,
            content_hash=hashlib.sha256(content.encode()).hexdigest())
        db.add(chunk)
        db.flush()
        ids.append(chunk.id)
    db.commit()
    return mail.id, ids


def test_phrase_expansion_is_bounded_and_not_sql(store):
    vals = lexical_variants('PDF 文字辨識失敗')
    assert vals[0] == 'PDF 文字辨識失敗'
    assert 'PDF' in vals
    assert any('辨識' in v for v in vals[1:])
    assert len(vals) <= 6
    assert lexical_variants('  ') == []
    assert lexical_variants('" OR 1=1 --')


def test_without_llm_groups_chinks_but_never_claims_relevance(store):
    S, (a, _) = store
    with S() as db:
        e1, c1 = add_chunks(db, a, 'report', 'PDF 測試', [
            ('attachment', '五份掃描式 PDF 因為沒有 OCR 而無法擷取文字'),
            ('summary', 'PDF 測試失敗，待修復'),
        ])
        e2, c2 = add_chunks(db, a, 'other', '雜項', [
            ('body', '歡迎使用 PDF 編輯器與年度電子報')])
    with S() as db:
        out = search_relevant_emails(db, a, 'PDF', limit=10)
    ids = [r['email_id'] for r in out]
    assert ids.count(e1) == 1
    assert e2 in ids
    assert all(row['relevance'] == 'unjudged' for row in out)
    evidence = next(r for r in out if r['email_id'] == e1)['evidence']
    assert {x['source_type'] for x in evidence} == {'attachment', 'summary'}


def test_rerank_filters_unrelated_no_forced_top_n(store):
    S, (a, _) = store
    with S() as db:
        target, tc = add_chunks(db, a, 'report', 'PDF 測試', [
            ('body', '掃描式 PDF 的文字辨識失敗因為未安裝 OCR 模組'),
            ('summary', '掃描式 PDF 需要 OCR'),
        ])
        other, oc = add_chunks(db, a, 'notice', '官方通知', [
            ('body', '歡迎使用 PDF 的新活動')])

    observed = {}
    def fake_reranker(query, groups):
        observed['query'] = query
        observed['groups'] = groups
        return [
            {'email_id': other, 'relevance': 'irrelevant', 'supporting_chunk_ids': []},
            {'email_id': target, 'relevance': 'relevant', 'supporting_chunk_ids': [tc[0]]},
        ]

    with S() as db:
        hits = search_relevant_emails(db, a, 'PDF', limit=5, rerank=True,
                                      reranker=fake_reranker)
    assert len(hits) == 1
    assert hits[0]['email_id'] == target
    assert hits[0]['relevance'] == 'relevant'
    assert hits[0]['supporting_chunk_ids'] == [tc[0]]
    assert 'PDF' in observed['query']


def test_llm_cannot_invent_account_or_supporting_chunk(store):
    S, (a, b) = store
    with S() as db:
        own, own_chunks = add_chunks(db, a, 'own', '月度報告', [('body', '附件處理測試：驗收完成')])
        secret, secret_chunks = add_chunks(db, b, 'secret', '機密郵件', [('body', '附件處理測試：不可外洩')])

    def malicious(query, groups):
        return [
            {'email_id': secret, 'relevance': 'relevant', 'supporting_chunk_ids': secret_chunks},
            {'email_id': own, 'relevance': 'relevant', 'supporting_chunk_ids': [999999]},
            {'email_id': own, 'relevance': 'relevant', 'supporting_chunk_ids': own_chunks},
        ]
    with S() as db:
        out = search_relevant_emails(db, a, '附件處理測試', rerank=True,
                                     reranker=malicious)
    assert [r['email_id'] for r in out] == [own]
    assert all('機密' not in str(v) for v in out)


def test_partial_is_off_by_default(store):
    S, (a, _) = store
    with S() as db:
        mid, ids = add_chunks(db, a, 'partial', '解析結果', [('body', 'PDF 存檔成功')])
    judge = lambda q, groups: [{'email_id': mid, 'relevance': 'partial',
                                 'supporting_chunk_ids': ids}]
    with S() as db:
        assert search_relevant_emails(db, a, 'PDF', rerank=True, reranker=judge) == []
        result = search_relevant_emails(db, a, 'PDF', rerank=True,
                                        include_partial=True, reranker=judge)
        assert result[0]['relevance'] == 'partial'


def test_empty_query_does_not_load_model_or_call_api(store):
    S, (a, _) = store
    with S() as db:
        assert search_relevant_emails(db, a, '  ', use_semantic=True, rerank=True,
                                      reranker=lambda *args: 1 / 0) == []


def test_new_mcp_tool_uses_fixed_account_and_no_api_by_default(store):
    S, (a, b) = store
    with S() as db:
        add_chunks(db, a, 'a', '目標郵件', [('body', 'PDF 掃描失敗 OCR')])
        add_chunks(db, b, 'b', '別人的信', [('body', 'PDF 掃描失敗 OCR 私密')])
    with patch.object(mcp_tools, 'SessionLocal', S), \
         patch.object(mcp_tools, 'MCP_ACCOUNT_ID', a), \
         patch('app.config.ENABLE_GEMINI_RERANK', False), \
         patch('app.config.ENABLE_SEMANTIC_QUERY', False):
        result = mcp_tools.search_relevant_emails('PDF')
        assert len(result) == 1
        assert result[0]['subject'] == '目標郵件'
        assert result[0]['relevance'] == 'unjudged'


from contextlib import contextmanager

@contextmanager
def _mock_genai(client):
    import google
    from types import ModuleType, SimpleNamespace
    mock = ModuleType('google.genai')
    mock.Client = lambda **kwargs: client
    mock.types = SimpleNamespace(GenerateContentConfig=lambda **kwargs: kwargs)
    with patch.dict('sys.modules', {'google.genai': mock}), \
         patch.object(google, 'genai', mock, create=True):
        yield


def test_gemini_rerank_sends_only_bounded_candidates_and_parses_json():
    from types import SimpleNamespace
    from app.services.gemini_relevance import gemini_rerank

    groups = [{'email_id': 'email-1', 'subject': 'PDF 報告',
               'evidence': [{'chunk_id': 17, 'source_type': 'attachment',
                             'text': 'OCR 失敗' * 500}]}]
    called = []
    def respond(**kwargs):
        called.append(kwargs)
        return SimpleNamespace(text='{"judgements":[{"email_id":"email-1",'
                                    '"relevance":"relevant",'
                                    '"supporting_chunk_ids":[17]}]}')
    fake_client = SimpleNamespace(models=SimpleNamespace(generate_content=respond),
                                  close=lambda: None)
    with patch('app.services.gemini_relevance.GEMINI_API_KEY', 'dummy-key'), _mock_genai(fake_client):
        decisions = gemini_rerank('掃描 PDF 為何失敗', groups)
    assert decisions[0]['supporting_chunk_ids'] == [17]
    assert len(called) == 1
    sent = called[0]['contents']
    assert len(sent) < 2000
    assert 'dummy-key' not in sent


def test_gemini_rerank_rejects_broken_json():
    from types import SimpleNamespace
    from app.services.gemini_relevance import gemini_rerank
    client = SimpleNamespace(models=SimpleNamespace(
        generate_content=lambda **kwargs: SimpleNamespace(text='not json')),
        close=lambda: None)
    with patch('app.services.gemini_relevance.GEMINI_API_KEY', 'dummy-key'), _mock_genai(client):
        with pytest.raises(RuntimeError, match='JSON'):
            gemini_rerank('test', [{'email_id': 'mail-1', 'subject': 'q',
                                     'evidence': [{'chunk_id': 2, 'source_type': 'body',
                                                   'text': 'sample'}]}])
