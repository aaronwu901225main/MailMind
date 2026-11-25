"""Read-only, zero-API route preview; no database or LINE calls."""
import argparse
from app.services.agent_query_router import route_question


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--query', required=True)
    args = parser.parse_args()
    plan = route_question(args.query)
    print('路由:', plan.kind)
    if plan.kind == 'spending':
        print('查詢事件: transaction')
        print('統計狀態: purchase（含授權型消費通知）')
        print('開始日期:', plan.start_date)
        print('結束日期（不含）:', plan.end_date)
        print('使用工具: MCP aggregate_events + get_extraction_coverage')
        print('不會呼叫 Gemini，也不會對信件內文做 RAG')
    elif plan.kind == 'clarify':
        print('需要補充:', plan.reason)
    else:
        print('沿用原本 search_relevant_emails + Gemini 問答流程')


if __name__ == '__main__':
    main()
