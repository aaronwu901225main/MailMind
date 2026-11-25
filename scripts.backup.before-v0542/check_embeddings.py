"""Cheap SQLite check. Does not import torch or download models."""
import argparse
from sqlalchemy import func, select

from app.config import EMBEDDING_MODEL
from app.database import SessionLocal, init_database
from app.models import EmailAccount, KnowledgeChunk, KnowledgeEmbedding


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--account-id', required=True)
    args = p.parse_args()
    init_database()
    with SessionLocal() as db:
        if db.get(EmailAccount, args.account_id) is None:
            raise SystemExit('找不到指定帳號')
        chunks = db.scalar(select(func.count()).select_from(KnowledgeChunk).where(
            KnowledgeChunk.account_id == args.account_id)) or 0
        vectors = db.scalar(select(func.count()).select_from(KnowledgeEmbedding)
                            .join(KnowledgeChunk, KnowledgeEmbedding.chunk_id == KnowledgeChunk.id)
                            .where(KnowledgeChunk.account_id == args.account_id,
                                   KnowledgeEmbedding.model_name == EMBEDDING_MODEL,
                                   KnowledgeEmbedding.content_hash == KnowledgeChunk.content_hash)) or 0
    print(f'模型：{EMBEDDING_MODEL}')
    print(f'Chunks：{chunks}')
    print(f'有效向量：{vectors}')
    print(f'待補向量：{chunks-vectors}')
    if vectors == 0:
        print('尚無有效向量，目前只能使用 FTS5 搜尋。')


if __name__ == '__main__':
    main()
