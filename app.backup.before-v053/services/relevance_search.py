"""v0.5.2.1: Conservative account-scoped candidate retrieval and email grouping.

No network or LLM calls. Optional semantic retrieval still needs the CPU model.
The existing hybrid_search() API is deliberately left intact.
"""
from __future__ import annotations

import re
from collections import OrderedDict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import KnowledgeChunk
from app.services.hybrid_search import hybrid_search
from app.services.knowledge_search import search_chunks

# Expand *keywords*, not user intent. Bounded and deterministic; the reranker
# decides whether weak keyword matches provide real evidence.
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9_./-]*|[0-9][A-Za-z0-9_.-]+")
_HAN = re.compile(r"[\u3400-\u9fff]{3,}")
_SOURCE_ORDER = {'attachment': 0, 'body': 1, 'summary': 2, 'metadata': 3}


def lexical_variants(query: str, max_variants: int = 6) -> list[str]:
    """Generate at most six subqueries, preserving exact original first.

    Trigram FTS5 cannot find separated/synonymous phrases. Smaller phrases
    improve recall but must be treated as weaker evidence.
    """
    query = ' '.join((query or '').split())[:200]
    if not query:
        return []
    variants = [query]

    def add(part):
        part = part.strip()
        if len(part) >= 3 and part.casefold() not in {s.casefold() for s in variants}:
            variants.append(part)

    for part in _LATIN.findall(query):
        add(part)
    for part in _HAN.findall(query):
        add(part)
        if len(part) > 4:
            add(part[:4])
            add(part[-4:])
        if len(part) > 6:
            add(part[:3])
            add(part[-3:])
    return variants[:max(1, min(int(max_variants), 8))]


def _collect_candidates(session: Session, account_id: str, query: str, *,
                        use_semantic: bool, embedder=None, model_name=None,
                        pool_size: int = 20) -> list[dict]:
    """FTS + semantic RRF, then small, weakly-weighted lexical expansions."""
    kwargs = dict(limit=20, use_semantic=use_semantic, embedder=embedder)
    if model_name is not None:
        kwargs['model_name'] = model_name
    base = hybrid_search(session, account_id, query, **kwargs)
    combined: dict[int, dict] = {}
    for rank, row in enumerate(base, 1):
        key = row['chunk_id']
        combined[key] = {**row, 'candidate_score': float(row['rrf_score']),
                         'query_variants': ['original']}
    for variant in lexical_variants(query)[1:]:
        for rank, row in enumerate(search_chunks(session, account_id, variant, 8), 1):
            cid = row['chunk_id']
            # A weak recall signal, not an excuse to override exact/semantic hits.
            supplement = 0.20 / (60 + rank)
            if cid in combined:
                combined[cid]['candidate_score'] += supplement
                if 'lexical-expand' not in combined[cid]['retrieval_modes']:
                    combined[cid]['retrieval_modes'].append('lexical-expand')
                combined[cid]['query_variants'].append(variant)
            else:
                combined[cid] = {**row, 'candidate_score': supplement,
                                 'rrf_score': 0.0, 'retrieval_modes': ['lexical-expand'],
                                 'query_variants': [variant]}
    return sorted(combined.values(),
                  key=lambda row: (-row['candidate_score'], row['chunk_id']))[:pool_size]


def group_by_email(session: Session, account_id: str, hits: list[dict], *,
                   limit: int = 8, chunks_per_email: int = 3,
                   text_chars: int = 1200) -> list[dict]:
    """Group multiple body/summary/attachment hits as one email, preserving evidence.

    Read content only after re-checking account_id, even though upstream tools
    already use account-scoped SQL. The evidence remains direct source text.
    """
    limit = max(1, min(int(limit), 12))
    chunks_per_email = max(1, min(int(chunks_per_email), 3))
    text_chars = max(100, min(int(text_chars), 2000))
    grouped: OrderedDict[str, dict] = OrderedDict()
    seen_chunks = set()
    for hit in hits:
        eid, cid = hit['email_id'], hit['chunk_id']
        if cid in seen_chunks:
            continue
        seen_chunks.add(cid)
        group = grouped.get(eid)
        if group is None:
            if len(grouped) >= limit:
                continue
            group = {'email_id': eid, 'subject': hit['subject'],
                     'sender': hit['sender'], 'candidate_score': hit['candidate_score'],
                     'evidence': []}
            grouped[eid] = group
        if len(group['evidence']) >= chunks_per_email:
            continue
        # A second account filter prevents accidental cross-tenant content reads.
        chunk = session.scalar(select(KnowledgeChunk).where(
            KnowledgeChunk.id == cid, KnowledgeChunk.account_id == account_id,
            KnowledgeChunk.email_id == eid))
        if chunk is None:
            continue
        group['evidence'].append({
            'chunk_id': cid, 'source_type': chunk.source_type,
            'source_id': chunk.source_id, 'chunk_index': chunk.chunk_index,
            'filename': hit.get('filename'),
            'text': chunk.chunk_text[:text_chars],
            'retrieval_modes': list(hit.get('retrieval_modes', [])),
            'vector_cosine': hit.get('vector_cosine'),
        })
    # Remove any empty email groups. Do not force a fixed number of results.
    return [g for g in grouped.values() if g['evidence']]


def search_relevant_emails(session: Session, account_id: str, query: str, *,
                           limit: int = 5, use_semantic: bool = False,
                           rerank: bool = False, include_partial: bool = False,
                           embedder=None, model_name=None, reranker=None) -> list[dict]:
    """Local grouping by default, optional explicit relevance judgement.

    No single cosine threshold: multilingual-E5 scores can be tightly bunched.
    Returning unreranked candidates is *not* a claim of relevance.
    """
    query = (query or '').strip()[:500]
    if not account_id or not query:
        return []
    limit = max(1, min(int(limit), 10))
    hits = _collect_candidates(session, account_id, query, use_semantic=use_semantic,
                               embedder=embedder, model_name=model_name, pool_size=20)
    # Reserve headroom for removing irrelevant candidates.
    groups = group_by_email(session, account_id, hits, limit=min(12, max(limit * 2, 8)))
    if not rerank:
        return [{**g, 'relevance': 'unjudged'} for g in groups[:limit]]

    if reranker is None:
        from app.services.gemini_relevance import gemini_rerank
        reranker = gemini_rerank
    decisions = reranker(query, groups)
    allowed = {'relevant'} | ({'partial'} if include_partial else set())
    groups_by_id = {g['email_id']: g for g in groups}
    ranked: list[dict] = []
    seen_ids: set[str] = set()
    for decision in decisions:
        eid = decision.get('email_id')
        group = groups_by_id.get(eid)
        if group is None or eid in seen_ids or decision.get('relevance') not in allowed:
            continue
        valid_ids = {e['chunk_id'] for e in group['evidence']}
        ids = decision.get('supporting_chunk_ids', [])
        if not isinstance(ids, list):
            continue
        supported = [v for v in ids if isinstance(v, int) and not isinstance(v, bool) and v in valid_ids]
        if not supported:
            continue   # no cited evidence -> no relevance claim
        seen_ids.add(eid)
        ranked.append({**group, 'relevance': decision['relevance'],
                       'supporting_chunk_ids': supported})
        if len(ranked) == limit:
            break
    return ranked
