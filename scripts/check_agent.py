"""Inspect schema/config without printing credentials or email contents."""
from sqlalchemy import func, select

from app.config import (AGENT_ALLOW_CLOUD_EMAIL, AGENT_ENABLED,
                        AGENT_USE_SEMANTIC, AGENT_DAILY_LIMIT, AGENT_MONTHLY_PUSH_LIMIT)
from app.database import SessionLocal, init_database
from app.models import LineAgentJob


def main():
    init_database()
    print('LINE Agent:', '啟用' if AGENT_ENABLED else '停用')
    print('郵件證據送雲端:', '已同意' if AGENT_ALLOW_CLOUD_EMAIL else '未同意')
    print('本地語意搜尋:', '啟用' if AGENT_USE_SEMANTIC else '停用（仍有 FTS5）')
    print('每日問答上限:', AGENT_DAILY_LIMIT)
    print('每月 Agent Push 上限:', AGENT_MONTHLY_PUSH_LIMIT)
    with SessionLocal() as db:
        result = db.execute(select(LineAgentJob.status, func.count(LineAgentJob.id)).group_by(LineAgentJob.status)).all()
    print('工作狀態:', dict(result))


if __name__ == '__main__':
    main()
