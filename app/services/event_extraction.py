"""Conservative multi-event extraction with document-level event segmentation.

No bank-specific parsers. Multiple transaction rows in one email are separate
occurrences; statement boilerplate and discount/fee numbers never become
verified spending. Source dates always come from original body/attachment text.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from app.services.event_schema import money_to_minor, validate_event
from app.services.event_segmentation import SourceDocument, reconstruct

DATE_RE = re.compile(r'(?<!\d)((?:19|20)\d{2})[./\-年](\d{1,2})[./\-月](\d{1,2})(?:日)?(?!\d)')
AMOUNT_RE = re.compile(
    r'(?<![A-Za-z0-9])(?P<unit>NT\s*\$|NTD|TWD|新臺幣|新台幣|台幣|USD|US\s*\$|EUR|JPY|日圓)'
    r'\s*(?P<amount>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)', re.I)
AMOUNT_SUFFIX_RE = re.compile(
    r'(?<!\d)(?P<amount>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)'
    r'\s*(?P<unit>元(?:整)?|TWD|NTD|USD|EUR|JPY)(?![A-Za-z])', re.I)
CARD_RE = re.compile(r'(?:卡號\s*(?:後|末)\s*[4４四]碼|末四碼|末4碼|尾號|ending in)\s*[:：#]?\s*(\d{4})(?!\d)', re.I)
# Keep legacy names for optional Gemini extractor compatibility, but NEVER
# match "at" inside a domain: those tokens are not merchant evidence.
MERCHANT_RE = re.compile(r'(?:商店名稱|特約商店|消費商家|交易商店|店家|merchant)\s*[:：]\s*([^\n\r]{2,80})', re.I)
TRANSACTION_WORDS = re.compile(r'信用卡|簽帳|消費|刷卡|交易|付款|支付|扣款|purchase|charged|payment|refund|退款|授權|消費金額', re.I)
PURCHASE_WORDS = re.compile(r'消費|刷卡|交易成功|扣款|支付|付款|charged|purchase|spent|payment', re.I)
REFUND_WORDS = re.compile(r'退款|退刷|退貨退款|refund', re.I)
REVERSE_WORDS = re.compile(r'取消交易|沖正|撤銷交易|reversal|voided', re.I)
AUTH_WORDS = re.compile(r'預授權|授權通知|授權紀錄|授權記錄|授權日期|authorization|pre-auth', re.I)
EXCLUDE_WORDS = re.compile(r'可用額度|信用額度|最低應繳|應繳總金額|帳單總額|統計總計|累計消費', re.I)
DEADLINE_WORDS = re.compile(r'截止|期限|繳交|提交|前完成|請於|due date|deadline|by\s+(?:20\d{2})', re.I)
FEE_WORDS = re.compile(r'手續費|年利率|循環利率|現金回饋|回饋金|活動回饋|優惠價|折抵|優惠金|最低應繳|最低付款|信用額度|可用額度|回饋\s*[:：]|fee\s*[:：]', re.I)
NON_MERCHANT = re.compile(r'^(?:餐飲|超市|量販|百貨|購物|一般消費|其他|註[一二三四五六七八九十0-9]|備註|國內|國外|TW|TWD|消費類別|類別|交易結果|手續費|回饋)(?:\b|\s|$)', re.I)
DATE_NOT_TX = re.compile(r'通知日期|寄送日期|寄件日期|發送日期|報告日期|請於|截止日期|繳費期限|繳款期限|due date|deadline', re.I)
DATE_TX_LABEL = re.compile(r'消費日期|交易日期|授權日期|刷卡日期|付款日期|交易時間|transaction date', re.I)


def _date_matches(text: str):
    for match in DATE_RE.finditer(text):
        try:
            value = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            continue
        yield match, value


def _currency(unit: str) -> str:
    code = re.sub(r'\s+', '', unit).upper()
    if code in ('USD', 'US$'):
        return 'USD'
    if code == 'EUR':
        return 'EUR'
    if code in ('JPY', '日圓'):
        return 'JPY'
    return 'TWD'


def _quote(text: str, start: int, end: int, margin: int = 95) -> str:
    return text[max(0, start-margin):min(len(text), end+margin)].strip()[:500]


def _closest_date(text: str, offset: int, span: int = 180):
    values = [(min(abs(m.start()-offset), abs(m.end()-offset)), d)
              for m, d in _date_matches(text)
              if m.start() <= offset+span and m.end() >= offset-span]
    values.sort(key=lambda item: item[0])
    if not values:
        return None, False
    return values[0][1], (len(values)>1 and values[1][0]-values[0][0]<12
                           and values[1][1] != values[0][1])


def _raw_record(*, kind: str, chunk_id: int, source_id: str,
                source_type: str, start: int, end: int, quote: str,
                method: str, fields: dict, valid: bool, reason: str = '') -> dict:
    return {'event_type':kind, 'source_chunk_id':chunk_id,
            'source_id':source_id, 'source_type':source_type,
            'span_start':start, 'span_end':end,
            'evidence_quote':quote, 'extraction_method':method,
            'fields':fields, 'validation_status':'verified' if valid else 'review',
            'reason':reason}


def _amounts(text: str):
    found = sorted([*AMOUNT_RE.finditer(text), *AMOUNT_SUFFIX_RE.finditer(text)],
                   key=lambda m: (m.start(), -m.end()))
    nonoverlap = []
    for m in found:
        if not nonoverlap or m.start() >= nonoverlap[-1].end():
            nonoverlap.append(m)
    return nonoverlap


def _get_date(text: str, amount, previous_amount_end: int, next_amount_start: int):
    """Find a transaction date within this *occurrence*'s interval.

    Date between the previous amount and this one belongs to this row;
    never reuse the date of a previous transaction just because it is close.
    """
    start = max(previous_amount_end, amount.start()-520)
    candidates = []
    for m, value in _date_matches(text[start:amount.start()]):
        global_start = start + m.start()
        preceding = text[max(start, global_start-24):global_start]
        following = text[start+m.end():min(len(text),start+m.end()+16)]
        if DATE_NOT_TX.search(preceding) or re.match(r'\s*(?:消費|帳單|交易)?通知', following):
            continue
        labeled = bool(DATE_TX_LABEL.search(preceding))
        candidates.append((2 if labeled else 1, global_start, start+m.end(), value))
    # Date after amount: only if in this occurrence, explicitly labeled or
    # directly next to the amount. Never treat payment due date as tx date.
    after_end = min(next_amount_start, amount.end()+140)
    for m, value in _date_matches(text[amount.end():after_end]):
        global_start = amount.end()+m.start()
        preceding = text[max(amount.end(),global_start-35):global_start]
        if DATE_NOT_TX.search(preceding):
            continue
        labeled = bool(DATE_TX_LABEL.search(preceding))
        if labeled:
            candidates.append((2, global_start, amount.end()+m.end(), value))
    if not candidates:
        return None, None, False
    candidates.sort(key=lambda r: (r[0], r[1] if r[1] < amount.start() else -r[1]), reverse=True)
    chosen = candidates[0]
    ambiguous = bool(len(candidates)>1 and candidates[1][0]==chosen[0]
                     and candidates[1][3] != chosen[3]
                     and abs(candidates[1][1]-chosen[1])<30)
    return chosen[3], (chosen[1], chosen[2]), ambiguous


def _find_merchant(text: str, amount, segment_start: int, segment_end: int) -> str | None:
    """Extract from this amount's row; never search email URLs or other rows."""
    before = text[max(segment_start, amount.start()-120):amount.start()]
    after = text[amount.end():min(segment_end,amount.end()+130)]
    for hit in MERCHANT_RE.finditer(before + after):
        raw = re.split(r'\s{2,}|[;；，,。\n]', hit.group(1).strip(), 1)[0].strip()
        if raw and not NON_MERCHANT.search(raw):
            return raw[:120]
    at = re.search(r'\bat\s+([A-Za-z][A-Za-z\d \-&\'\.]{1,50})', after)
    if at:
        candidate = re.split(r'\s{2,}|[.,;\n]',at.group(1),1)[0].strip()
        if candidate:
            return candidate[:120]
    # An adjacent alphanumeric suffix is part of the amount's surrounding
    # prose, not evidence of a merchant column.
    if after and not re.match(r'^[ \t]', after):
        return None
    line = after.splitlines()[0] if after else ''
    # Columns separated by large spacing in a flattened HTML table or PDF.
    columns = [c.strip() for c in re.split(r'\s{2,}|\t+', line) if c.strip()]
    if not columns and line.strip():
        columns = [line.strip()]
    if not columns:
        before_merchant = re.search(r'(?:在|於)\s*([^\s，。、]{2,45})\s*(?:消費|刷卡|付款)', before)
        return before_merchant.group(1) if before_merchant else None
    head = columns[0]
    # If the text is separated by only one space, take the first word unless
    # it is a category. Unknown/hidden merchant stays null, never guessed.
    if len(columns)==1 and ' ' in head:
        head = head.split()[0]
    head = head.strip('：:，,。;；*')
    if not head or len(head)>55 or NON_MERCHANT.search(head) or re.fullmatch(r'[\d\W]+',head):
        return None
    if re.match(r'^(?:https?://|www\.|取消訂閱|請勿回覆)',head,re.I):
        return None
    return head[:120]


def _card_near(text: str, offset: int) -> str | None:
    close = list(CARD_RE.finditer(text[max(0,offset-380):min(len(text),offset+100)]))
    if close:
        return close[-1].group(1)
    whole = {m.group(1) for m in CARD_RE.finditer(text)}
    return next(iter(whole)) if len(whole)==1 else None


def extract_document(chunks: list) -> list[dict]:
    """Extract every independent occurrence across a source's chunk boundaries."""
    if not chunks or chunks[0].source_type not in {'body','attachment'}:
        return []
    doc = reconstruct(chunks)
    text = doc.text
    amounts = _amounts(text)
    results=[]
    for i, m in enumerate(amounts[:250]):
        previous_end = amounts[i-1].end() if i else 0
        next_start = amounts[i+1].start() if i+1<len(amounts) else len(text)
        # Finance disclaimer amounts, fee illustrations and reward amounts
        # are not card transactions, even if a transaction is nearby.
        before = text[max(previous_end,m.start()-75):m.start()]
        line = text[max(0,text.rfind('\n',0,m.start())+1):min(len(text),text.find('\n',m.end()) if '\n' in text[m.end():] else len(text))]
        if FEE_WORDS.search(before[-55:]) or FEE_WORDS.search(line[:max(0,m.start()-max(0,text.rfind('\n',0,m.start())+1))]):
            continue
        context = text[max(previous_end,m.start()-190):min(next_start,m.end()+55)]
        if EXCLUDE_WORDS.search(before[-70:]) or not TRANSACTION_WORDS.search(context):
            continue
        try:
            currency = _currency(m.group('unit'))
            amount = money_to_minor(m.group('amount'),currency)
        except ValueError:
            continue
        occurred, date_span, ambiguous = _get_date(text,m,previous_end,next_start)
        # Time is attached to the selected transaction row/date, never to
        # another amount or the email's delivery timestamp.
        occurred_time = None
        if date_span:
            time_fragment = text[date_span[1]:min(len(text), date_span[1]+45)]
            time_match = re.search(r'(?<!\d)((?:[01]\d|2[0-3]):[0-5]\d)(?!\d)', time_fragment)
            if time_match:
                occurred_time = time_match.group(1)
        near = text[max(previous_end,m.start()-130):min(next_start,m.end()+45)]
        if REVERSE_WORDS.search(near):
            status = 'reversal'
        elif REFUND_WORDS.search(near):
            status = 'refund'
        elif PURCHASE_WORDS.search(near) or AUTH_WORDS.search(near):
            # A spending notification is useful to users even when the bank
            # sends authorizations but not a later posting notification.
            status = 'purchase'
        else:
            status = 'unknown'
        # This is *provenance*, not an exclusion from spending totals.
        auth_scope = text[max(0,m.start()-650):m.start()]
        stage = 'authorization' if AUTH_WORDS.search(auth_scope) else 'unknown'
        q_start = min(m.start(),date_span[0]) if date_span else m.start()
        q_end = max(m.end(),date_span[1]) if date_span else m.end()
        q_start = max(0,q_start-25)
        q_end = min(len(text),q_end+80)
        quote = text[q_start:q_end]
        # Do not construct an unverifiable citation by joining far apart spans.
        if len(quote)>500:
            quote = text[max(0,m.start()-150):min(len(text),m.end()+100)]
        quote = quote.strip()
        anchor, local = doc.containing(m.start())
        if anchor is None:
            continue
        merchant = _find_merchant(text,m,previous_end,next_start)
        fields = {'event_type':'transaction','source_chunk_id':anchor.id,
                  'evidence_quote':quote,'extraction_method':'regex',
                  'occurred_on':occurred,'amount_minor':amount,'currency':currency,
                  'merchant':merchant,'card_last4':_card_near(text,m.start()),
                  'transaction_reference':None,'event_status':status,
                  'source_stage':stage,'occurred_time':occurred_time}
        verified=bool(occurred and not ambiguous and status!='unknown'
                      and any(d==occurred for _,d in _date_matches(quote)))
        if verified:
            try: validate_event(fields)
            except ValueError: verified=False
        reason = '' if verified else ('date_missing_or_ambiguous' if not occurred or ambiguous else 'missing_event_evidence')
        rec=_raw_record(kind='transaction',chunk_id=anchor.id,
                source_id=doc.source_id,source_type=doc.source_type,
                start=local,end=local+len(m.group()),quote=quote,method='regex',
                fields=fields,valid=verified,reason=reason)
        rec.update({'document_span_start':m.start(),'document_span_end':m.end(),
                    'document_date_span':date_span})
        results.append(rec)
    # Distinct deadline occurrences, not nearby transaction dates.
    for m,due in list(_date_matches(text))[:120]:
        preceding=text[max(0,m.start()-80):m.start()]
        following=text[m.end():min(len(text),m.end()+65)]
        if not (DEADLINE_WORDS.search(preceding[-50:]) or
                re.search(r'截止|期限|繳交|提交|請於|deadline|due date',following[:35],re.I)):
            continue
        if TRANSACTION_WORDS.search(preceding[-65:]) and not re.search(r'截止|期限|繳交|提交|due date|deadline|請於',preceding[-50:]+following[:30],re.I):
            continue
        q_start=max(0,m.start()-80);q_end=min(len(text),m.end()+65)
        quote=text[q_start:q_end].strip()
        anchor,local=doc.containing(m.start())
        if anchor is None:
            continue
        title=quote[:130] or '截止事項'
        fields={'event_type':'deadline','source_chunk_id':anchor.id,
                'evidence_quote':quote,'extraction_method':'regex',
                'occurred_on':due,'title':title,'action':title[:130],
                'event_status':'due'}
        try: validate_event(fields); valid=True
        except ValueError: valid=False
        rec=_raw_record(kind='deadline',chunk_id=anchor.id,
                source_id=doc.source_id,source_type=doc.source_type,
                start=local,end=local+len(m.group()),quote=quote,method='regex',
                fields=fields,valid=valid,reason='' if valid else 'schema_validation')
        rec.update({'document_span_start':m.start(),'document_span_end':m.end()})
        results.append(rec)
    return results


def extract_local(chunk) -> list[dict]:
    """One-chunk compatibility entrypoint for existing callers/tests."""
    return extract_document([chunk])


def validate_source_anchor(chunk, rec: dict, source_document: SourceDocument | None = None) -> bool:
    """Check numeric and date evidence against the original scoped source."""
    if chunk.id!=rec.get('source_chunk_id') or chunk.source_type not in {'body','attachment'}:
        return False
    if source_document is None:
        text=chunk.chunk_text or ''
        if rec.get('document_span_start') is not None:
            source_document=reconstruct([chunk])
    else:
        if (source_document.source_id,source_document.source_type)!=(chunk.source_id,chunk.source_type):
            return False
        text=source_document.text
    if source_document is not None:
        pos=rec.get('document_span_start')
        end=rec.get('document_span_end')
        if not isinstance(pos,int) or not isinstance(end,int) or not 0<=pos<end<=len(text):
            return False
        matched,off=source_document.containing(pos)
        if matched is None or matched.id!=chunk.id:
            return False
        if text[pos:end] not in [m.group() for m in _amounts(text[pos:end])] and rec.get('event_type')=='transaction':
            return False
    quote=rec.get('evidence_quote','')
    if not isinstance(quote,str) or not quote or quote not in text:
        return False
    f=rec.get('fields',{})
    dates={d.isoformat() for _,d in _date_matches(quote)}
    if str(f.get('occurred_on')) not in dates:
        return False
    if f.get('event_type')=='transaction':
        values=set()
        for m in _amounts(quote):
            try:
                code=_currency(m.group('unit'))
                values.add((code,money_to_minor(m.group('amount'),code)))
            except ValueError: pass
        if (f.get('currency'),f.get('amount_minor')) not in values:
            return False
        merchant=f.get('merchant')
        if merchant is not None and merchant not in quote:
            return False
        card=f.get('card_last4')
        if card and not any(m.group(1)==card for m in CARD_RE.finditer(text)):
            return False
    elif f.get('event_type')!='deadline':
        return False
    return True
