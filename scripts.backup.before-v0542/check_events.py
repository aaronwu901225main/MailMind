"""Safe v0.5.4 schema and event count check; no private values printed."""
from sqlalchemy import func, inspect, select
from app.database import SessionLocal, init_database
from app.models import StructuredEvent, EventExtractionRun


def main():
    init_database()
    with SessionLocal() as db:
        tables = inspect(db.get_bind()).get_table_names()
        for table in ('structured_events', 'event_extraction_runs'):
            if table not in tables:
                raise RuntimeError(f'資料表不存在: {table}')
        total = db.scalar(select(func.count(StructuredEvent.id))) or 0
        verified = db.scalar(select(func.count(StructuredEvent.id)).where(
            StructuredEvent.validation_status == 'verified')) or 0
        pending = db.scalar(select(func.count(StructuredEvent.id)).where(
            StructuredEvent.validation_status == 'review')) or 0
        runs = db.scalar(select(func.count(EventExtractionRun.id))) or 0
    print('✅ MailMind v0.5.4 事件 Schema 初始化成功')
    print(f'抽取紀錄：{runs}，事件：{total}，verified：{verified}，review：{pending}')
    print('本指令不會呼叫 Gemini、LINE 或處理任何郵件原文。')


if __name__ == '__main__':
    main()
