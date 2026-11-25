"""Event taxonomy and validators. To add a new event kind, register its schema.

Schema versioning is separate from email templates: each incoming format can
map to the same typed semantic event, with evidence anchored to source chunks.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = '2'
EXTRACTOR_VERSION = 'v0.5.4.1-2'
CURRENCY_DECIMALS = {'TWD': 2, 'USD': 2, 'EUR': 2, 'JPY': 0}


class _EventBase(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    source_chunk_id: int = Field(gt=0)
    evidence_quote: str = Field(min_length=5, max_length=500)
    extraction_method: Literal['regex', 'gemini']


class TransactionEvent(_EventBase):
    event_type: Literal['transaction'] = 'transaction'
    occurred_on: date
    amount_minor: int = Field(gt=0)
    currency: Literal['TWD', 'USD', 'EUR', 'JPY']
    merchant: str | None = Field(default=None, max_length=120)
    card_last4: str | None = Field(default=None, pattern=r'^\d{4}$')
    transaction_reference: str | None = Field(default=None, max_length=100)
    event_status: Literal['purchase', 'refund', 'reversal', 'authorization', 'unknown']
    source_stage: Literal['authorization', 'posted', 'unknown'] = 'unknown'
    occurred_time: str | None = Field(default=None, pattern=r'^(?:[01]\d|2[0-3]):[0-5]\d$')


class DeadlineEvent(_EventBase):
    event_type: Literal['deadline'] = 'deadline'
    occurred_on: date
    title: str = Field(min_length=2, max_length=160)
    action: str = Field(min_length=2, max_length=160)
    event_status: Literal['due'] = 'due'


EVENT_SCHEMAS = {'transaction': TransactionEvent, 'deadline': DeadlineEvent}


def validate_event(event: dict):
    """Reject unsupported types and silently invented fields."""
    kind = event.get('event_type') if isinstance(event, dict) else None
    schema = EVENT_SCHEMAS.get(kind)
    if schema is None:
        raise ValueError('unsupported_event_type')
    return schema.model_validate(event)


def money_to_minor(text: str, currency: str) -> int:
    """Exact decimal conversion. No floats, no implicit FX conversion."""
    places = CURRENCY_DECIMALS[currency]
    number = text.replace(',', '').replace('，', '').strip()
    try:
        value = Decimal(number)
    except InvalidOperation:
        raise ValueError('invalid_amount') from None
    if not value.is_finite() or value <= 0:
        raise ValueError('invalid_amount')
    multiplier = Decimal(10) ** places
    minor = value * multiplier
    if minor != minor.to_integral_value(rounding=ROUND_HALF_UP):
        raise ValueError('unsupported_fractional_minor_unit')
    if minor >= 2**63:  # SQLite signed integer bounds
        raise ValueError('amount_out_of_range')
    return int(minor)
