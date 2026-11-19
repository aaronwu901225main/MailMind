"""資料庫連線與初始化。"""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from app.config import DATABASE_URL

class Base(DeclarativeBase):
    pass

engine = create_engine(DATABASE_URL, connect_args={'check_same_thread': False}
                       if DATABASE_URL.startswith('sqlite:') else {})
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

def init_database() -> None:
    # 讓 SQLAlchemy 掃描 model metadata
    import app.models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    # Existing v0.2 SQLite tables are not changed by create_all().
    # Install the delivery deduplication index as a small, backward-compatible migration.
    with engine.begin() as connection:
        connection.execute(text(
            'CREATE UNIQUE INDEX IF NOT EXISTS uq_delivery_email_user_channel '
            'ON delivery_logs (email_id, user_id, channel)'
        ))
