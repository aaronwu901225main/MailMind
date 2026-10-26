"""v0.4 integration tests: ingestion + document extraction + account isolation."""
import base64
import io
from unittest.mock import patch

import pytest
from docx import Document
from PIL import Image
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.main import app
from app.models import EmailAccount, EmailMessage, EmailAttachment, User
from app.routes.ingest import get_db, hash_token
from app import mcp_tools
from app.services.attachment_parser import extract_attachment


def encoded_docx(text: str) -> str:
    doc = Document()
    doc.add_paragraph(text)
    out = io.BytesIO()
    doc.save(out)
    return base64.b64encode(out.getvalue()).decode()


def setup_db():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    with S() as db:
        a, b = User(), User()
        db.add_all([a, b]); db.flush()
        aa = EmailAccount(user_id=a.id, address='a@example.com', ingest_token_hash=hash_token('ta'))
        ab = EmailAccount(user_id=b.id, address='b@example.com', ingest_token_hash=hash_token('tb'))
        db.add_all([aa, ab]); db.commit()
        aids = (aa.id, ab.id)
    return engine, S, aids


def test_docx_attachments_reach_summary_and_are_scoped():
    engine, S, (a_id, b_id) = setup_db()
    def override_db():
        with S() as db:
            yield db
    app.dependency_overrides[get_db] = override_db
    data = {'account_id': a_id, 'message_id': 'docx-001', 'subject': '測試附件',
            'body': '請參閱附件', 'attachments': [{
                'filename': '../report.docx', 'mime_type': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                'data_base64': encoded_docx('截止日期為 2026/11/01')}]}
    try:
        with patch('app.routes.ingest.summarize_with_gemini', return_value='完成') as summary:
            with TestClient(app) as client:
                result = client.post('/api/v1/ingest/gmail', json=data,
                                     headers={'X-MailMind-Token': 'ta'})
                assert result.status_code == 200, result.text
                assert '截止日期為 2026/11/01' in summary.call_args.args[2]
                assert client.post('/api/v1/ingest/gmail', json=data,
                                   headers={'X-MailMind-Token': 'ta'}).json()['status'] == 'duplicate'
                assert summary.call_count == 1
        with S() as db:
            attachment = db.scalar(select(EmailAttachment))
            mail = db.scalar(select(EmailMessage))
            assert attachment.filename == 'report.docx'
            assert attachment.status == 'extracted'
            assert attachment.extracted_text.startswith('截止日期')
            att_id, mail_id = attachment.id, mail.id
        with patch.object(mcp_tools, 'SessionLocal', S), patch.object(mcp_tools, 'MCP_ACCOUNT_ID', a_id):
            found = mcp_tools.search_emails('測試附件')
            assert found[0]['email_id'] == mail_id
            assert mcp_tools.list_email_attachments(mail_id)[0]['attachment_id'] == att_id
            assert '截止日期' in mcp_tools.read_attachment_text(att_id)['text']
            assert mcp_tools.get_email_summary(mail_id)['summary'] == '完成'
        with patch.object(mcp_tools, 'SessionLocal', S), patch.object(mcp_tools, 'MCP_ACCOUNT_ID', b_id):
            assert mcp_tools.search_emails('測試附件') == []
            with pytest.raises(ValueError):
                mcp_tools.get_email_summary(mail_id)
            with pytest.raises(ValueError):
                mcp_tools.read_attachment_text(att_id)
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_image_is_not_sent_to_gemini_without_opt_in():
    img = Image.new('RGB', (24, 24))
    buffer = io.BytesIO()
    img.save(buffer, format='PNG')
    data = base64.b64encode(buffer.getvalue()).decode()
    with patch('app.services.attachment_parser.ENABLE_CLOUD_IMAGE_ANALYSIS', False):
        result = extract_attachment('sample.png', 'image/png', data)
        assert result.status == 'metadata_only'
        assert '24x24' in result.note
        assert result.text == ''


def test_invalid_pdf_does_not_fail_ingestion_and_bad_base64_is_bounded():
    data = base64.b64encode(b'not a valid pdf').decode()
    parsed = extract_attachment('test.pdf', 'application/pdf', data)
    assert parsed.status == 'error'
    assert not parsed.text
    with pytest.raises(ValueError):
        extract_attachment('x.pdf', 'application/pdf', 'invalid!!')
    with pytest.raises(ValueError):
        extract_attachment('bad.exe', 'application/octet-stream', data)


def test_real_pdf_text_extraction():
    from pathlib import Path
    content = (Path(__file__).parent / 'fixtures' / 'sample.pdf').read_bytes()
    parsed = extract_attachment('agenda.pdf', 'application/pdf', base64.b64encode(content).decode())
    assert parsed.status == 'extracted'
    assert 'Deadline 2026-11-02' in parsed.text
