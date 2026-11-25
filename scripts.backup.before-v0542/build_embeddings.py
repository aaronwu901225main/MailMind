"""Optional offline embedding batch job; never start on an HPC login node unless permitted."""
import argparse

from app.config import EMBEDDING_MODEL
from app.database import SessionLocal, init_database
from app.models import EmailAccount
from app.services.embedding_store import (
    delete_orphan_embeddings, load_embedder, pending_chunks, store_chunk_embeddings,
)


def main():
    p = argparse.ArgumentParser(description='Offline, restartable local CPU embedding index')
    p.add_argument('--account-id', required=True)
    p.add_argument('--batch-size', type=int, default=8)
    p.add_argument('--max-chunks', type=int, default=0,
                   help='0=all pending, otherwise at most N for smoke tests')
    p.add_argument('--allow-download', action='store_true',
                   help='Allow the first Hugging Face model download in this batch job only')
    args = p.parse_args()
    if not 1 <= args.batch_size <= 64 or args.max_chunks < 0:
        p.error('batch-size must be 1..64; max-chunks must be >= 0')
    init_database()
    with SessionLocal() as db:
        if db.get(EmailAccount, args.account_id) is None:
            raise SystemExit('找不到指定帳號')
        initial = pending_chunks(db, args.account_id, EMBEDDING_MODEL, limit=1)
        delete_orphan_embeddings(db)
        db.commit()
    if not initial:
        print('✅ 沒有待建立或更新的向量')
        return
    print('模型：', EMBEDDING_MODEL)
    print('準備載入 CPU 模型；首次下載需加 --allow-download。')
    embedder = load_embedder(EMBEDDING_MODEL, allow_download=args.allow_download)
    count = 0
    while True:
        cap = args.batch_size if not args.max_chunks else min(args.batch_size, args.max_chunks - count)
        if cap <= 0:
            break
        with SessionLocal() as db:
            chunks = pending_chunks(db, args.account_id, EMBEDDING_MODEL, limit=cap)
            if not chunks:
                break
            changed = store_chunk_embeddings(db, chunks, embedder, EMBEDDING_MODEL)
            db.commit()
        count += changed
        print(f'已完成向量：{count} 個 Chunks')
    print(f'✅ 本次處理 {count} 個 Chunks；可安全重複執行')


if __name__ == '__main__':
    main()
