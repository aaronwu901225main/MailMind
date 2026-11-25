"""One account-scoped end-to-end demonstration without sending LINE messages."""
import argparse
import asyncio

from app.config import AGENT_ALLOW_CLOUD_EMAIL
from app.services.line_agent_worker import (retrieve_mcp, validate_evidence, build_answer,
                                            retrieve_analytics_mcp)
from app.services.agent_query_router import route_question, format_spending_answer


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--account-id', required=True)
    ap.add_argument('--query', required=True)
    args = ap.parse_args()
    if not AGENT_ALLOW_CLOUD_EMAIL:
        raise SystemExit('請先明確設定 MAILMIND_AGENT_ALLOW_CLOUD_EMAIL=true')
    query = args.query[:400]
    plan = route_question(query)
    print('查詢路由：', plan.kind)
    if plan.kind == 'spending':
        aggregate, coverage = await retrieve_analytics_mcp(args.account_id, plan)
        print(format_spending_answer(plan, aggregate, coverage))
    elif plan.kind == 'clarify':
        print('無法安全統計：' + plan.reason)
    else:
        result = await retrieve_mcp(args.account_id, query)
        trusted = validate_evidence(args.account_id, result)
        print('已通過來源驗證的郵件數：', len(trusted))
        print(build_answer(query, trusted))


if __name__ == '__main__':
    asyncio.run(main())
