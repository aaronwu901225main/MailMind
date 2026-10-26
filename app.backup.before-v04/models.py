"""第一版的多使用者資料模型；沒有公開註冊／登入 API。"""
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


class User(Base):
    __tablename__ = 'users'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    line_user_id: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    summary_level: Mapped[str] = mapped_column(String(12), nullable=False, default='medium')
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    accounts: Mapped[list['EmailAccount']] = relationship(back_populates='user')


class EmailAccount(Base):
    __tablename__ = 'email_accounts'
    __table_args__ = (UniqueConstraint('user_id', 'address', name='uq_user_email_address'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), nullable=False, index=True)
    address: Mapped[str] = mapped_column(String(320), nullable=False)
    provider: Mapped[str] = mapped_column(String(30), default='gmail', nullable=False)
    ingest_token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    user: Mapped[User] = relationship(back_populates='accounts')


class EmailMessage(Base):
    __tablename__ = 'emails'
    __table_args__ = (UniqueConstraint('account_id', 'provider_message_id', name='uq_account_message'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(ForeignKey('email_accounts.id'), nullable=False, index=True)
    provider_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    sender: Mapped[str] = mapped_column(String(320), default='', nullable=False)
    subject: Mapped[str] = mapped_column(Text, default='', nullable=False)
    state: Mapped[str] = mapped_column(String(30), default='new', nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Summary(Base):
    __tablename__ = 'summaries'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email_id: Mapped[str] = mapped_column(ForeignKey('emails.id'), unique=True, nullable=False)
    summary_level: Mapped[str] = mapped_column(String(12), nullable=False)
    model_name: Mapped[str] = mapped_column(String(120), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DeliveryLog(Base):
    __tablename__ = 'delivery_logs'
    __table_args__ = (UniqueConstraint('email_id', 'user_id', 'channel', name='uq_email_user_channel'),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email_id: Mapped[str] = mapped_column(ForeignKey('emails.id'), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(30), default='line', nullable=False)
    status: Mapped[str] = mapped_column(String(30), default='pending', nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LineLinkCode(Base):
    """Short-lived, single-use account-to-LINE binding challenge."""
    __tablename__ = 'line_link_codes'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), nullable=False, index=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
