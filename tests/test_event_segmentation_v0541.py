"""Anonymized multi-event regression cases; contains no user's real email/PAN.

Reproduces the layout of a notification with repeating transaction tables.
Uses synthetic merchant names/card digits and unrelated fee boilerplate.
"""
import hashlib
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import User, EmailAccount, EmailMessage, KnowledgeChunk, StructuredEvent
from app.services.event_extraction import extract_document, extract_local, validate_source_anchor
from app.services.event_schema import validate_event
from app.services.event_segmentation import reconstruct
from app.services.event_store import index_one_email
from app.services.event_analytics import aggregate_events

SYNTHETIC_BULK = '''
通知日期：2026/11/20
親愛的客戶您好。您最新的消費授權紀錄如下：
卡號後4碼： 9876
卡別                行動卡號後4碼            授權日期             授權時間
正卡                1122                   2026/11/19          19:21
消費金額                        商店名稱                     消費類別
NT$83                           測試文具行                   超市∕量販
卡別                行動卡號後4碼            授權日期             授權時間
正卡                                       2026/11/19          11:36
消費金額                        商店名稱                     消費類別                 備註
NT$200                                                        餐飲                     註一
卡別                行動卡號後4碼            授權日期             授權時間
正卡                                       2026/11/19          18:29
消費金額                        商店名稱                     消費類別                 備註
NT$264                                                        餐飲                     註一
註一：商店尚未提供名稱，請稍後查詢。
若對消費有疑異請聯絡客服。
謹慎理財，信用無價。預借現金手續費：預借現金金額乘以 3％加上新臺幣150元或美元5元。
'''


def chunk(text, index=0, source_type='body', cid=1, source_id='source-a'):
    return SimpleNamespace(chunk_text=text, source_type=source_type,
                           source_id=source_id, chunk_index=index, id=cid,
                           content_hash=hashlib.sha256(text.encode()).hexdigest())


def t_events(chunks):
    return [r for r in extract_document(chunks) if r['event_type']=='transaction']


def test_synthetic_three_distinct_transactions_and_no_footer_fee():
    rows=t_events([chunk(SYNTHETIC_BULK)])
    assert len(rows)==3
    assert [r['fields']['amount_minor'] for r in rows]==[8300,20000,26400]
    assert [str(r['fields']['occurred_on']) for r in rows]==['2026-11-19']*3
    assert [r['fields']['occurred_time'] for r in rows]==['19:21','11:36','18:29']
    assert [r['fields']['merchant'] for r in rows]==['測試文具行',None,None]
    assert all(r['fields']['event_status']=='purchase' for r in rows)
    assert all(r['validation_status']=='verified' for r in rows)
    assert all(r['fields']['source_stage']=='authorization' for r in rows[:1])
    doc=reconstruct([chunk(SYNTHETIC_BULK)])
    assert all(validate_source_anchor(doc.pieces[0].chunk,r,doc) for r in rows)


def test_reconstruct_fts_overlap_does_not_duplicate_three_occurrences():
    parts=[]
    begin=0
    for idx,stop in enumerate((630,1170,len(SYNTHETIC_BULK))):
        if begin>=len(SYNTHETIC_BULK):break
        stop=min(stop,len(SYNTHETIC_BULK))
        parts.append(chunk(SYNTHETIC_BULK[begin:stop],idx,cid=idx+2))
        begin=stop-120
        if stop==len(SYNTHETIC_BULK):break
    assert len(parts)>1
    doc=reconstruct(parts)
    assert doc.text==SYNTHETIC_BULK
    records=t_events(parts)
    assert len(records)==3
    assert {r['fields']['amount_minor'] for r in records}=={8300,20000,26400}
    assert all(validate_source_anchor(doc.containing(r['document_span_start'])[0],r,doc)
               for r in records)


def test_repeated_same_day_same_amount_not_deduplicated():
    text='2026/11/19 10:01 信用卡消費 TWD 100\n2026/11/19 12:45 信用卡消費 TWD 100'
    rows=t_events([chunk(text)])
    assert len(rows)==2
    assert rows[0]['fields']['occurred_time']=='10:01'
    assert rows[1]['fields']['occurred_time']=='12:45'


def test_dates_in_disclaimers_and_payment_deadlines_not_transaction_dates():
    rows=t_events([chunk('信用卡消費 TWD 300，請於 2026/12/15 繳費。手續費新臺幣150元。')])
    assert rows and rows[0]['validation_status']=='review'
    assert all(r['fields']['amount_minor']!=15000 for r in rows)


def test_no_phantom_merchant_from_url_or_unknown_category():
    text=('服務網址：https://example.test/cathaybk/abc\n'
          '2026/11/22 信用卡消費 NT$442 優步 餐飲\n')
    events=t_events([chunk(text)])
    assert len(events)==1 and events[0]['fields']['merchant']=='優步'


def test_source_metadata_does_not_become_transactions():
    rows=extract_local(chunk('2026/11/19 信用卡消費 TWD 100',source_type='summary'))
    assert rows==[]


def test_overall_db_three_items_sum_and_repeat_is_idempotent(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'multi.db'))
    Base.metadata.create_all(engine)
    Session=sessionmaker(bind=engine)
    with Session() as db:
        user=User();db.add(user);db.flush()
        account=EmailAccount(user_id=user.id,address='user@example.test')
        db.add(account);db.flush()
        email=EmailMessage(account_id=account.id,provider_message_id='bulk-1',
                           subject='bulk spending',state='completed')
        db.add(email);db.flush()
        c=KnowledgeChunk(account_id=account.id,email_id=email.id,
                         source_type='body',source_id=email.id,chunk_index=0,
                         chunk_text=SYNTHETIC_BULK,
                         content_hash=hashlib.sha256(SYNTHETIC_BULK.encode()).hexdigest())
        db.add(c);db.commit()
        result=index_one_email(db,account.id,email.id)
        assert result['verified']==3 and result['review']==0
        response=aggregate_events(db,account.id,event_type='transaction',
                     start_date='2026-11-19',end_date='2026-11-20')
        assert response['totals_by_currency'][0]['amount_minor']==54700
        assert response['totals_by_currency'][0]['transaction_count']==3
        assert index_one_email(db,account.id,email.id)['status']=='skipped'
        assert len(db.scalars(select(StructuredEvent)).all())==3
        # New extractor keeps original email and unique source events on rerun.
    engine.dispose()


def test_authorization_only_spending_legacy_aggregates_as_notified_spend(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'legacy.db'))
    Base.metadata.create_all(engine)
    Session=sessionmaker(bind=engine)
    with Session() as db:
        u=User();db.add(u);db.flush()
        a=EmailAccount(user_id=u.id,address='t@example.test');db.add(a);db.flush()
        email=EmailMessage(account_id=a.id,provider_message_id='auth-test',
                           subject='auth',state='completed');db.add(email);db.flush()
        c=KnowledgeChunk(account_id=a.id,email_id=email.id,source_type='body',
                         source_id=email.id,chunk_index=0,
                         chunk_text='2026/11/19 信用卡消費 TWD 200',content_hash='hash')
        db.add(c);db.flush()
        from app.models import StructuredEvent
        import json
        db.add(StructuredEvent(account_id=a.id,email_id=email.id,source_chunk_id=c.id,
                               source_type='body',event_type='transaction',
                               event_fingerprint='legacy',occurred_on='2026-11-19',
                               amount_minor=20000,currency='TWD',event_status='authorization',
                               payload_json=json.dumps({'event_status':'authorization'}),
                               evidence_quote=c.chunk_text,extraction_method='regex',
                               validation_status='verified',schema_version='1'))
        db.commit()
        out=aggregate_events(db,a.id,event_type='transaction',
                             start_date='2026-11-19',end_date='2026-11-20',status='purchase')
        assert out['totals_by_currency'][0]['amount_minor']==20000
    engine.dispose()


def test_multiple_deadlines_are_independent_events():
    body='申請截止日期 2026/11/30，請按期辦理。\n繳交文件截止日期 2026/12/15，請於期限內完成。'
    rows=extract_document([chunk(body)])
    due=[r for r in rows if r['event_type']=='deadline']
    assert len(due)==2
    assert [str(r['fields']['occurred_on']) for r in due]==['2026-11-30','2026-12-15']


def test_missing_explicit_transaction_date_after_amount_is_review():
    body='2026/11/20 消費通知。NT$120，另外提及 2026/11/21 的說明。'
    rows=t_events([chunk(body)])
    assert rows and rows[0]['validation_status']=='review'


def test_truncated_original_source_does_not_use_summary_to_fill_missing_transactions():
    body='2026/11/19 信用卡消費 NT$120\n'
    rows=t_events([chunk(body)])
    assert len(rows)==1
    assert rows[0]['fields']['amount_minor']==12000
