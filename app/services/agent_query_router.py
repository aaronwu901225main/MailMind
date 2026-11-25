"""Rule-based, auditable routing of bounded spending totals to MCP analytics.

Unrecognized or mixed intents remain on the existing RAG path.  No user text
is turned into SQL or tool names.  Calendar ranges are Taiwan-local and [a,b).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Taipei')


@dataclass(frozen=True)
class QueryPlan:
    kind: str                 # rag / spending / clarify
    start_date: str = ''
    end_date: str = ''         # exclusive
    label: str = ''
    reason: str = ''


_SPENDING = re.compile(r'刷卡|信用卡|簽帳|消費|花了?多少|花費|支出|刷了?多少', re.I)
_TOTAL = re.compile(r'多少|總共|總額|合計|加總|統計|累計|幾筆|金額|花了?幾|花了?多少', re.I)
_EXPLAIN = re.compile(r'為什麼|為何|怎麼辦|如何|原因|差別|差異|說明|解釋')
_DAY_RE = re.compile(r'(?<!\d)(20\d{2})[-/](\d{1,2})[-/](\d{1,2})(?!\d)')


def _local_day(now: datetime | None) -> date:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        # DB timestamps are stored as UTC without timezone information.
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(TZ).date()


def route_question(question: str, *, now: datetime | None = None) -> QueryPlan:
    q = ' '.join((question or '').split())
    # One user utterance may ask for an explanation as well as totals: preserve
    # the old RAG route rather than silently answer only part of the question.
    if not (_SPENDING.search(q) and _TOTAL.search(q)) or _EXPLAIN.search(q):
        return QueryPlan('rag')
    today = _local_day(now)
    if any(word in q for word in ('這週', '本週', '這周', '本周', '這星期', '本星期')):
        a = today - timedelta(days=today.weekday())
        b = a + timedelta(days=7)
        label = '本週'
    elif any(word in q for word in ('上週', '上周', '上星期', '前一週', '前一周')):
        b = today - timedelta(days=today.weekday())
        a = b - timedelta(days=7)
        label = '上週'
    elif any(word in q for word in ('這個月', '本月', '這月')):
        a = today.replace(day=1)
        b = date(a.year + (a.month == 12), (a.month % 12) + 1, 1)
        label = '本月'
    elif any(word in q for word in ('上個月', '上月', '前一個月')):
        b = today.replace(day=1)
        a = date(b.year - (b.month == 1), ((b.month - 2) % 12) + 1, 1)
        label = '上個月'
    elif any(word in q for word in ('今天', '今日')):
        a, b, label = today, today + timedelta(days=1), '今天'
    elif any(word in q for word in ('昨天', '昨日')):
        a, b, label = today - timedelta(days=1), today, '昨天'
    elif any(word in q for word in ('最近7天', '過去7天', '近7天', '近七天', '最近七天')):
        a, b, label = today - timedelta(days=6), today + timedelta(days=1), '最近7天（含今天）'
    elif any(word in q for word in ('最近30天', '過去30天', '近30天')):
        a, b, label = today - timedelta(days=29), today + timedelta(days=1), '最近30天（含今天）'
    else:
        matches = list(_DAY_RE.finditer(q))
        try:
            if len(matches) == 1:
                a = date(*(int(x) for x in matches[0].groups()))
                b = a + timedelta(days=1)
                label = a.isoformat()
            elif len(matches) == 2 and re.search(r'到|至|~|～|—|–', q[matches[0].end():matches[1].start()]):
                a = date(*(int(x) for x in matches[0].groups()))
                last = date(*(int(x) for x in matches[1].groups()))
                b = last + timedelta(days=1)  # human inclusive end
                label = f'{a.isoformat()} 至 {last.isoformat()}'
                if a > last or (b - a).days > 366:
                    return QueryPlan('clarify', reason='日期區間需由早到晚，且最多一年。')
            else:
                return QueryPlan('clarify', reason='請指定「這週」「上週」「本月」或完整日期，例如 2026-11-19。')
        except ValueError:
            return QueryPlan('clarify', reason='日期格式無效，請使用 YYYY-MM-DD。')
    return QueryPlan('spending', a.isoformat(), b.isoformat(), label)


def format_spending_answer(plan: QueryPlan, aggregate: dict, coverage: dict) -> str:
    """Do precise integer formatting; never ask Gemini to sum transactions."""
    if plan.kind != 'spending':
        raise ValueError('not_spending_query')
    if (not isinstance(aggregate, dict) or aggregate.get('event_type') != 'transaction'
            or aggregate.get('start_date') != plan.start_date
            or aggregate.get('end_date_exclusive') != plan.end_date
            or aggregate.get('is_complete') is not False
            or not isinstance(aggregate.get('totals_by_currency'), list)):
        raise ValueError('mcp_invalid_aggregate')
    import re as _re
    lines = [f'【{plan.label}刷卡消費統計】',
             f'交易日期：{plan.start_date} 至 {plan.end_date}（不含結束日，台灣時間）']
    totals = aggregate['totals_by_currency']
    if not totals:
        lines.append('目前沒有符合日期範圍且已驗證的刷卡消費通知紀錄；這不代表實際消費為 0 元。')
    else:
        for row in totals[:10]:
            if not isinstance(row, dict):
                raise ValueError('mcp_invalid_totals')
            cur, count, amount, digits = (row.get('currency'), row.get('transaction_count'),
                                          row.get('amount_minor'), row.get('minor_unit_digits'))
            if (not isinstance(cur, str) or not _re.fullmatch(r'[A-Z]{3}', cur)
                    or type(count) is not int or count < 0 or count > 1_000_000
                    or type(amount) is not int or amount < 0 or amount > 10**15
                    or type(digits) is not int or digits not in (0, 2, 3)):
                raise ValueError('mcp_invalid_totals')
            unit = 10 ** digits
            whole, minor = divmod(amount, unit)
            money = f'{whole:,}' + (f'.{minor:0{digits}d}' if digits else '')
            prefix = 'NT$' if cur == 'TWD' else f'{cur} '
            lines.append(f'{cur}：{count} 筆，{prefix}{money}')
    if isinstance(coverage, dict):
        done = coverage.get('emails_with_extraction_attempt')
        all_count = coverage.get('account_email_count')
        if (type(done) is int and type(all_count) is int
                and 0 <= done <= all_count <= 10000000):
            lines.append(f'全信箱事件擷取進度：{done}/{all_count} 封（不是此期間交易覆蓋率）。')
    lines.append('提醒：只統計已匯入、已驗證的消費通知（含授權通知），不等於最終帳單；部分郵件可能漏收或尚未解析，跨封重複通知也尚未完成對帳。')
    return '\n'.join(lines)[:1850]
