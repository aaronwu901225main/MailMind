"""v0.5.4.2 offline router/analytics tests, never contact Gemini or LINE."""
import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import EmailAccount, LineAgentJob, User
from app.services.agent_query_router import format_spending_answer, route_question
from app.services.line_agent_queue import enqueue_question
from app.services import line_agent_worker as worker

TIME = datetime(2026, 11, 24, 8, 0, tzinfo=timezone.utc)  # 2026-11-24 16:00 Taipei


def test_this_week_is_taipei_monday_monday():
    r = route_question('我這週總共刷了多少錢？', now=TIME)
    assert (r.kind, r.start_date, r.end_date) == ('spending', '2026-11-23', '2026-11-30')


def test_last_week_includes_nov_19():
    r = route_question('我上週信用卡消費合計多少？', now=TIME)
    assert (r.start_date, r.end_date) == ('2026-11-16', '2026-11-23')


def test_month_boundary_and_year_turnover():
    r = route_question('上個月信用卡花多少', now=datetime(2027, 1, 4, tzinfo=timezone.utc))
    assert (r.start_date, r.end_date) == ('2026-12-01', '2027-01-01')
    r = route_question('本月刷卡總額', now=TIME)
    assert (r.start_date, r.end_date) == ('2026-11-01', '2026-12-01')


def test_day_and_explicit_inclusive_range():
    r = route_question('2026-11-19 刷卡消費多少', now=TIME)
    assert (r.start_date, r.end_date) == ('2026-11-19', '2026-11-20')
    r = route_question('2026/11/19 至 2026/11/22 信用卡總額', now=TIME)
    assert (r.start_date, r.end_date) == ('2026-11-19', '2026-11-23')


def test_other_intents_remain_rag():
    assert route_question('PDF 文字辨識失敗的原因是什麼？', now=TIME).kind == 'rag'
    assert route_question('信用卡授權跟請款的差別是什麼？', now=TIME).kind == 'rag'
    assert route_question('這週收到什麼重要郵件？', now=TIME).kind == 'rag'


def test_ambiguous_analytics_requires_dates():
    r = route_question('幫我計算信用卡總共刷了多少錢', now=TIME)
    assert r.kind == 'clarify'
    assert route_question('2026-11-30 到 2026-11-01 刷卡多少', now=TIME).kind == 'clarify'


def _result(start='2026-11-23', end='2026-11-30', totals=None):
    return {'event_type': 'transaction', 'status': 'purchase', 'start_date': start,
            'end_date_exclusive': end, 'is_complete': False,
            'totals_by_currency': totals if totals is not None else []}


def test_zero_is_not_claimed_when_no_records():
    plan = route_question('這週刷卡多少', now=TIME)
    result = format_spending_answer(plan, _result(), {
        'emails_with_extraction_attempt': 20, 'account_email_count': 146})
    assert '不代表實際消費為 0 元' in result
    assert '20/146' in result


def test_exact_integer_money_and_currency_separation():
    plan = route_question('本週信用卡消費總額', now=TIME)
    result = format_spending_answer(plan, _result(totals=[
        {'currency': 'TWD', 'transaction_count': 3, 'amount_minor': 54700, 'minor_unit_digits': 2},
        {'currency': 'JPY', 'transaction_count': 1, 'amount_minor': 500, 'minor_unit_digits': 0},
    ]), {})
    assert 'NT$547.00' in result and '3 筆' in result
    assert 'JPY 500' in result
    assert '最終帳單' in result


def test_mcp_response_validation_rejects_wrong_scope_period_and_values():
    plan = route_question('這週刷卡多少', now=TIME)
    with pytest.raises(ValueError):
        format_spending_answer(plan, _result('2026-01-01'), {})
    with pytest.raises(ValueError):
        format_spending_answer(plan, _result(totals=[{'currency': 'TWD',
            'transaction_count': 2, 'amount_minor': '200.0', 'minor_unit_digits': 2}]), {})


@pytest.fixture
def store(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'agent_route.db'))
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        user = User(line_user_id='LINE-ROUTER')
        db.add(user); db.flush()
        account = EmailAccount(user_id=user.id, address='router@example.invalid')
        db.add(account); db.commit()
        aid = account.id
    yield Session, aid
    engine.dispose()


def test_worker_routes_analytics_without_gemini_or_rag(store):
    Session, aid = store
    with Session() as db:
        status, job_id = enqueue_question(db, line_user_id='LINE-ROUTER',
                                          event_id='spending-evt', question='這週總共刷了多少錢', now=TIME)
        assert status == 'queued'
    calls = []
    async def fake_analytics(account_id, plan):
        calls.append(('aggregate', account_id, plan.start_date))
        return _result(totals=[{'currency': 'TWD', 'transaction_count': 1,
                                'amount_minor': 12000, 'minor_unit_digits': 2}]), {}
    async def no_rag(*args):
        pytest.fail('Analytics path must not use RAG or Gemini')
    def no_answer(*args):
        pytest.fail('Analytics path must not ask Gemini to sum')
    async def fake_send(line_user_id, job, answer):
        calls.append(('LINE', answer))
    with patch.object(worker, 'SessionLocal', Session), \
         patch.object(worker, 'AGENT_ENABLED', True), \
         patch.object(worker, 'AGENT_ALLOW_CLOUD_EMAIL', True):
        assert asyncio.run(worker.process_one_job(retriever=no_rag,
            answerer=no_answer, sender=fake_send, analytics_retriever=fake_analytics))
    assert calls[0][1] == aid and calls[0][2] == '2026-11-23'
    assert 'NT$120.00' in calls[1][1]
    with Session() as db:
        row = db.get(LineAgentJob, job_id)
        assert row.status == 'sent' and row.result_text is None


def test_worker_clarifies_without_any_tool_calls(store):
    Session, aid = store
    with Session() as db:
        status, job_id = enqueue_question(db, line_user_id='LINE-ROUTER',
                                          event_id='clarify-evt', question='刷卡總額多少', now=TIME)
        assert status == 'queued'
    got = []
    async def no_call(*args):
        pytest.fail('Clarification should not invoke MCP')
    async def fake_send(uid, job, text):
        got.append(text)
    with patch.object(worker, 'SessionLocal', Session), \
         patch.object(worker, 'AGENT_ENABLED', True), \
         patch.object(worker, 'AGENT_ALLOW_CLOUD_EMAIL', True):
        asyncio.run(worker.process_one_job(retriever=no_call,
            sender=fake_send, analytics_retriever=no_call))
    assert len(got) == 1 and '請指定' in got[0]
