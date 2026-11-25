"""Offline event-engine correctness/security tests, no Gemini/LINE calls."""
import hashlib
from datetime import date

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import (EmailAccount, EmailMessage, EventExtractionRun,
                        KnowledgeChunk, StructuredEvent, User)
from app.services.event_schema import money_to_minor, validate_event
from app.services.event_extraction import extract_local
from app.services.event_store import index_one_email
from app.services.event_cloud import extract_gemini
from app.services.event_analytics import (aggregate_events, search_events,
    get_event_detail, get_extraction_coverage)


@pytest.fixture
def store(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'events.db'))
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    with S() as db:
        a, b = User(), User()
        db.add_all([a, b]); db.flush()
        x, y = EmailAccount(user_id=a.id, address='a@localhost'), EmailAccount(user_id=b.id, address='b@localhost')
        db.add_all([x, y]); db.commit()
        accounts = x.id, y.id
    yield S, accounts
    engine.dispose()


def add(db, account_id, text=None, *, name='email', source='body'):
    mail = EmailMessage(account_id=account_id, provider_message_id=name,
                        sender='sender@localhost', subject=name, state='completed')
    db.add(mail); db.flush()
    chunk = None
    if text:
        chunk = KnowledgeChunk(account_id=account_id, email_id=mail.id,
                               source_type=source, source_id=mail.id,
                               chunk_index=0, chunk_text=text,
                               content_hash=hashlib.sha256(text.encode()).hexdigest())
        db.add(chunk); db.flush()
    db.commit()
    return mail, chunk


def test_decimal_money_no_floats():
    assert money_to_minor('1,200.50', 'TWD') == 120050
    assert money_to_minor('150', 'TWD') == 15000
    assert money_to_minor('999', 'JPY') == 999
    with pytest.raises(ValueError):
        money_to_minor('1.234', 'TWD')
    with pytest.raises(ValueError):
        money_to_minor('-100', 'USD')


def test_schema_registry_is_strict():
    with pytest.raises(ValueError):
        validate_event({'event_type': 'invented'})
    with pytest.raises(ValueError):
        validate_event({'event_type': 'transaction', 'source_chunk_id': 1,
                        'evidence_quote': '2026-11-24 消費 TWD 100', 'extraction_method': 'regex',
                        'occurred_on': '2026-11-24', 'amount_minor': 10000,
                        'currency': 'TWD', 'event_status': 'purchase', 'arbitrary_sql': 'drop table'})


def test_local_bank_templates_and_deadline(store):
    S, (a, _) = store
    with S() as db:
        mail, chunk = add(db, a, '您的信用卡末四碼 1234 於 2026/11/24 消費新臺幣 150 元。', name='bank')
        records = extract_local(chunk)
        assert len([r for r in records if r['event_type'] == 'transaction']) == 1
        assert records[0]['validation_status'] == 'verified'
        assert records[0]['fields']['amount_minor'] == 15000
        assert records[0]['fields']['card_last4'] == '1234'
        deadline_email, dead = add(db, a, '申請文件繳交截止日期為 2026-11-30，請於期限內完成。', name='deadline')
        parsed = extract_local(dead)
        assert any(r['event_type'] == 'deadline' and r['validation_status'] == 'verified' for r in parsed)


def test_ambiguous_without_year_is_review_not_verified(store):
    S, (a, _) = store
    with S() as db:
        _, chunk = add(db, a, '信用卡消費通知：11/24 便利商店消費 TWD 120 元', name='no-year')
        found = extract_local(chunk)
        assert found and found[0]['validation_status'] == 'review'
        assert found[0]['fields']['occurred_on'] is None


def test_no_summary_as_source(store):
    S, (a, _) = store
    with S() as db:
        mail, chunk = add(db, a, '2026/11/24 信用卡消費 TWD 500', name='summary', source='summary')
        res = index_one_email(db, a, mail.id)
        assert res['status'] == 'no_source'
        assert not db.scalars(select(StructuredEvent)).all()


def test_index_idempotent_and_account_scoped(store):
    S, (a, b) = store
    with S() as db:
        mail, chunk = add(db, a, '2026/11/24 信用卡消費 TWD 100', name='a')
        other, _ = add(db, b, '2026/11/24 信用卡消費 TWD 9000', name='b')
        with pytest.raises(ValueError, match='forbidden'):
            index_one_email(db, a, other.id)
        result = index_one_email(db, a, mail.id)
        assert result['verified'] == 1
        assert index_one_email(db, a, mail.id)['status'] == 'skipped'
        assert len(db.scalars(select(StructuredEvent)).all()) == 1
        assert search_events(db, b) == []
        assert aggregate_events(db, a, event_type='transaction',
                 start_date='2026-11-24', end_date='2026-11-25')['totals_by_currency'][0]['amount_minor'] == 10000


def test_two_identical_purchases_same_email_are_separate(store):
    S, (a, _) = store
    with S() as db:
        text = '2026/11/24 信用卡消費 TWD 100\n2026/11/24 信用卡消費 TWD 100'
        mail, chunk = add(db, a, text, name='two')
        res = index_one_email(db, a, mail.id)
        assert res['verified'] == 2
        total = aggregate_events(db, a, event_type='transaction',
                                 start_date='2026-11-24', end_date='2026-11-25')
        assert total['totals_by_currency'][0]['amount_minor'] == 20000


def test_currencies_and_refunds_not_summed_together(store):
    S, (a, _) = store
    with S() as db:
        for i, text in enumerate(('2026/11/24 信用卡消費 TWD 100',
                                  '2026/11/24 card charged USD 10',
                                  '2026/11/24 信用卡退款 TWD 50')):
            mail, _ = add(db, a, text, name=f'bank-{i}')
            index_one_email(db, a, mail.id)
        result = aggregate_events(db, a, event_type='transaction',
                                  start_date='2026-11-24', end_date='2026-11-25')
        amounts = {e['currency']: e['amount_minor'] for e in result['totals_by_currency']}
        assert amounts == {'TWD': 10000, 'USD': 1000}
        refund = aggregate_events(db, a, event_type='transaction',
                                  start_date='2026-11-24', end_date='2026-11-25', status='refund')
        assert refund['totals_by_currency'][0]['amount_minor'] == 5000
        assert result['is_complete'] is False


def test_half_open_date_boundaries(store):
    S, (a, _) = store
    with S() as db:
        for i, day in enumerate(('2026/11/23', '2026/11/24')):
            mail, _ = add(db, a, f'{day} 信用卡消費 TWD 100', name=f'day-{i}')
            index_one_email(db, a, mail.id)
        a1 = aggregate_events(db, a, event_type='transaction',
                              start_date='2026-11-23', end_date='2026-11-24')
        assert a1['totals_by_currency'][0]['transaction_count'] == 1
        with pytest.raises(ValueError):
            aggregate_events(db, a, event_type='transaction', start_date='2026-11-25', end_date='2026-11-24')


def test_source_update_replaces_stale_amount_not_duplicate(store):
    S, (a, _) = store
    with S() as db:
        mail, chunk = add(db, a, '2026/11/24 信用卡消費 TWD 100', name='change')
        index_one_email(db, a, mail.id)
        chunk.chunk_text = '2026/11/24 信用卡消費 TWD 200'
        chunk.content_hash = hashlib.sha256(chunk.chunk_text.encode()).hexdigest()
        db.commit()
        result = index_one_email(db, a, mail.id)
        assert result['status'] == 'processed'
        events = db.scalars(select(StructuredEvent)).all()
        assert len(events) == 1 and events[0].amount_minor == 20000


def test_coverage_does_not_claim_complete_spending(store):
    S, (a, _) = store
    with S() as db:
        good, _ = add(db, a, '2026/11/24 信用卡消費 TWD 100', name='good')
        empty, _ = add(db, a, name='old')
        index_one_email(db, a, good.id)
        index_one_email(db, a, empty.id)
        coverage = get_extraction_coverage(db, a)
        assert coverage['account_email_count'] == 2
        assert coverage['no_original_source_count'] == 1
        assert '不是信用卡交易的完整率' in coverage['coverage_warning']


def test_detail_has_direct_evidence_and_cannot_read_other_account(store):
    S, (a, b) = store
    with S() as db:
        mail, _ = add(db, a, '2026/11/24 信用卡消費 TWD 100', name='evidence')
        index_one_email(db, a, mail.id)
        event = db.scalars(select(StructuredEvent)).first()
        assert 'TWD 100' in get_event_detail(db, a, event.id)['evidence_quote']
        with pytest.raises(ValueError, match='forbidden'):
            get_event_detail(db, b, event.id)


def test_cloud_optin_and_hallucination_rejected(store):
    S, (a, _) = store
    with S() as db:
        mail, chunk = add(db, a, '信用卡交易於 2026/11/24 消費 NT$250，卡片末四碼 1234', name='cloud')
        with pytest.raises(ValueError, match='explicit_consent'):
            extract_gemini([chunk], allow_cloud=False)
        def bad(docs):
            return {'events': [{'chunk_id': chunk.id, 'event_type': 'transaction',
                'evidence_quote': '虛構的消費金額 9999', 'occurred_on': '2026-11-24',
                'amount_text': '9999', 'currency': 'TWD', 'event_status': 'purchase'}]}
        assert extract_gemini([chunk], allow_cloud=True, generate=bad) == []
        def good(docs):
            return {'events': [{'chunk_id': chunk.id, 'event_type': 'transaction',
                'evidence_quote': chunk.chunk_text, 'occurred_on': '2026-11-24',
                'amount_text': '250', 'currency': 'TWD', 'event_status': 'purchase'}]}
        parsed = extract_gemini([chunk], allow_cloud=True, generate=good)
        assert parsed and parsed[0]['validation_status'] == 'verified'


def test_chunk_overlap_does_not_double_count_purchase(store):
    S, (a, _) = store
    with S() as db:
        email = EmailMessage(account_id=a, provider_message_id='overlap',
                             subject='overlap', state='completed')
        db.add(email); db.flush()
        phrase = '2026/11/24 信用卡消費 TWD 100'
        left = ('x' * 650) + phrase + ('y' * 70)
        right = left[-120:] + ('z' * 300)
        for i, body in enumerate((left, right)):
            db.add(KnowledgeChunk(account_id=a, email_id=email.id,
                  source_type='body', source_id=email.id,
                  chunk_index=i, chunk_text=body,
                  content_hash=hashlib.sha256(body.encode()).hexdigest()))
        db.commit()
        res = index_one_email(db, a, email.id)
        assert res['verified'] == 1
        total = aggregate_events(db, a, event_type='transaction',
                                 start_date='2026-11-24', end_date='2026-11-25')
        assert total['totals_by_currency'][0]['transaction_count'] == 1


def test_bill_due_date_is_not_transaction_date(store):
    S, (a, _) = store
    with S() as db:
        _, chunk = add(db, a, '本期信用卡消費 TWD 300，請於 2026/12/15 繳費', name='bill-date')
        transactions = [r for r in extract_local(chunk) if r['event_type'] == 'transaction']
        assert transactions and all(r['validation_status'] == 'review' for r in transactions)


def test_cloud_cannot_relabel_refund_as_purchase(store):
    S, (a, _) = store
    with S() as db:
        mail, chunk = add(db, a, '2026/11/24 信用卡消費退款 TWD 250', name='refund-spoof')
        def dishonest(docs):
            return {'events': [{'chunk_id': chunk.id, 'event_type': 'transaction',
                'evidence_quote': chunk.chunk_text, 'occurred_on': '2026-11-24',
                'amount_text': '250', 'currency': 'TWD', 'event_status': 'purchase'}]}
        assert extract_gemini([chunk], allow_cloud=True, generate=dishonest) == []


def test_mcp_events_are_bound_to_fixed_account(store):
    from unittest.mock import patch
    from app import mcp_tools
    S, (a, b) = store
    with S() as db:
        mymail, _ = add(db, a, '2026/11/24 信用卡消費 TWD 200', name='mcp-a')
        other, _ = add(db, b, '2026/11/24 信用卡消費 TWD 9000', name='mcp-b')
        index_one_email(db, a, mymail.id)
        index_one_email(db, b, other.id)
    with patch.object(mcp_tools, 'SessionLocal', S), patch.object(mcp_tools, 'MCP_ACCOUNT_ID', a):
        totals = mcp_tools.aggregate_events('transaction', '2026-11-24', '2026-11-25')
        assert totals['totals_by_currency'][0]['amount_minor'] == 20000
        results = mcp_tools.search_events('transaction', '2026-11-24', '2026-11-25')
        assert len(results) == 1
        foreign_id = None
        with S() as db:
            foreign_id = db.scalar(select(StructuredEvent.id).where(StructuredEvent.account_id == b))
        with pytest.raises(ValueError, match='forbidden'):
            mcp_tools.get_event_detail(foreign_id)
