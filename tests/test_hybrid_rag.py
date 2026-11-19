"""Offline deterministic unit tests; never download models or use LLM APIs."""
import hashlib
from unittest.mock import patch

import numpy as np
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import (EmailAccount, EmailMessage, KnowledgeChunk,
                        KnowledgeEmbedding, User)
from app.services.embedding_store import (delete_orphan_embeddings, encode_query,
                                          encode_passages, pending_chunks,
                                          store_chunk_embeddings)
from app.services.hybrid_search import hybrid_search, vector_search
from app.services.knowledge_search import ensure_knowledge_fts, search_chunks
from app import mcp_tools


class FakeEmbedder:
    def __init__(self):
        self.seen = []

    def encode(self, items, **kwargs):
        self.seen += list(items)
        result = []
        for item in items:
            if any(word in item for word in ['資料集', '合成資料', '資料生成']):
                result.append([1.0, 0.0, 0.0])
            elif any(word in item for word in ['電費', '帳單']):
                result.append([0.0, 1.0, 0.0])
            else:
                result.append([0.0, 0.0, 1.0])
        return np.asarray(result, dtype=np.float32)


@pytest.fixture
def store(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'semantic.db'))
    Base.metadata.create_all(engine)
    assert ensure_knowledge_fts(engine)
    S = sessionmaker(bind=engine)
    with S() as db:
        users = [User(), User()]
        db.add_all(users)
        db.flush()
        accounts = [EmailAccount(user_id=u.id, address=f'user{n}@test.com')
                    for n, u in enumerate(users)]
        db.add_all(accounts)
        db.commit()
        ids = [a.id for a in accounts]
    yield engine, S, ids
    engine.dispose()


def add_chunk(db, account_id, source_text: str, msg_id: str):
    mail = EmailMessage(account_id=account_id, provider_message_id=msg_id,
                        sender='sender@example.com', subject=source_text[:15],
                        state='completed')
    db.add(mail)
    db.flush()
    chunk = KnowledgeChunk(account_id=account_id, email_id=mail.id,
                           source_type='body', source_id=mail.id,
                           chunk_index=0, chunk_text=source_text,
                           content_hash=hashlib.sha256(source_text.encode()).hexdigest())
    db.add(chunk)
    db.flush()
    return chunk.id


def test_e5_prefixes_normalize_and_persistence(store):
    _, S, (a, _) = store
    fake = FakeEmbedder()
    with S() as db:
        chunk_id = add_chunk(db, a, '研究資料集清理專案', 'msg-a')
        chunks = pending_chunks(db, a, 'fake-model')
        assert [c.id for c in chunks] == [chunk_id]
        assert store_chunk_embeddings(db, chunks, fake, 'fake-model') == 1
        db.commit()
    assert fake.seen == ['passage: 研究資料集清理專案']
    with S() as db:
        assert not pending_chunks(db, a, 'fake-model')
        row = db.get(KnowledgeEmbedding, chunk_id)
        assert row.dimension == 3 and len(row.vector) == 12
        found = vector_search(db, a, '合成資料整理', limit=3,
                              embedder=fake, model_name='fake-model')
        assert found[0]['chunk_id'] == chunk_id
        assert found[0]['score'] == pytest.approx(1.0)
        assert fake.seen[-1] == 'query: 合成資料整理'


def test_semantic_recalls_nonmatching_synonym_and_rrf(store):
    _, S, (a, _) = store
    fake = FakeEmbedder()
    with S() as db:
        target = add_chunk(db, a, '資料集清洗流程已完成', 'related')
        add_chunk(db, a, '這是電費繳費通知', 'unrelated')
        store_chunk_embeddings(db, pending_chunks(db, a, 'fake-model'), fake, 'fake-model')
        db.commit()
    with S() as db:
        assert search_chunks(db, a, '合成資料') == []
        hits = hybrid_search(db, a, '合成資料', limit=3,
                             use_semantic=True, embedder=fake, model_name='fake-model')
        assert hits[0]['chunk_id'] == target
        assert hits[0]['retrieval_modes'] == ['vector']
        both = hybrid_search(db, a, '資料集', limit=3,
                             use_semantic=True, embedder=fake, model_name='fake-model')
        assert both[0]['chunk_id'] == target
        assert set(both[0]['retrieval_modes']) == {'fts5', 'vector'}


def test_tenant_isolation_and_mcp_scope(store):
    _, S, (a, b) = store
    fake = FakeEmbedder()
    with S() as db:
        ca = add_chunk(db, a, '資料集來源非常機密', 'private-a')
        cb = add_chunk(db, b, '只有B能看到的資料集', 'private-b')
        store_chunk_embeddings(db, pending_chunks(db, a, 'fake-model'), fake, 'fake-model')
        store_chunk_embeddings(db, pending_chunks(db, b, 'fake-model'), fake, 'fake-model')
        db.commit()
    with S() as db:
        hits = vector_search(db, a, '合成資料', embedder=fake, model_name='fake-model')
        assert [r['chunk_id'] for r in hits] == [ca]
        assert cb not in [r['chunk_id'] for r in hits]
    with patch.object(mcp_tools, 'SessionLocal', S), \
         patch.object(mcp_tools, 'MCP_ACCOUNT_ID', a), \
         patch('app.config.ENABLE_SEMANTIC_QUERY', False):
        rows = mcp_tools.hybrid_search('資料集', limit=10)
        assert rows and all(r['chunk_id'] != cb for r in rows)


def test_content_change_and_orphan_vectors(store):
    _, S, (a, _) = store
    fake = FakeEmbedder()
    with S() as db:
        id1 = add_chunk(db, a, '資料集', 'one')
        store_chunk_embeddings(db, pending_chunks(db, a, 'fake-model'), fake, 'fake-model')
        db.commit()
    with S() as db:
        row = db.get(KnowledgeChunk, id1)
        row.chunk_text = '電費帳單已送達'
        row.content_hash = hashlib.sha256(row.chunk_text.encode()).hexdigest()
        db.commit()
    with S() as db:
        assert vector_search(db, a, '資料集', embedder=fake, model_name='fake-model') == []
        stale = pending_chunks(db, a, 'fake-model')
        assert len(stale) == 1
        store_chunk_embeddings(db, stale, fake, 'fake-model')
        db.commit()
    with S() as db:
        assert vector_search(db, a, '電費', embedder=fake, model_name='fake-model')[0]['chunk_id'] == id1
        db.delete(db.get(KnowledgeChunk, id1))
        db.commit()
    with S() as db:
        assert delete_orphan_embeddings(db) == 1
        db.commit()
        assert db.get(KnowledgeEmbedding, id1) is None


def test_lexical_fallback_with_no_vectors_or_model(store):
    _, S, (a, _) = store
    with S() as db:
        add_chunk(db, a, '關於 RAG-INDEX-5729 的進度', 'fallback')
        db.commit()
    with S() as db:
        hits = hybrid_search(db, a, 'RAG-INDEX-5729', use_semantic=True)
        assert hits and hits[0]['retrieval_modes'] == ['fts5']
        assert hits[0]['rrf_score'] > 0


def test_query_normalization_and_input_guard():
    fake = FakeEmbedder()
    out = encode_query(fake, '資料集')
    assert out.shape == (3,)
    assert np.linalg.norm(out) == pytest.approx(1)
    assert encode_passages(fake, []).shape == (0, 0)
    with pytest.raises(ValueError):
        from app.services.embedding_store import _normalized_vectors
        _normalized_vectors([[float('nan'), 1]], 1)


def test_eval_email_level_dedup_and_metrics():
    from scripts.eval_retrieval import metrics
    questions = [{'relevant_email_ids': ['email-B']},
                 {'relevant_email_ids': ['email-C']}]
    ranked = [['email-A', 'email-A', 'email-B'], ['email-Z']]
    result = metrics(questions, ranked, k=2)
    assert result['recall_at_k'] == pytest.approx(0.5)
    assert result['mrr_at_k'] == pytest.approx(0.25)
