"""MailMind FastAPI application entry point."""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.database import init_database
from app.routes.ingest import router as ingest_router
from app.routes.line_webhook import router as line_router
from app.routes.line_agent import router as line_agent_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_database()
    from app.database import SessionLocal
    from app.services.knowledge_search import ensure_knowledge_fts
    with SessionLocal() as db:
        ensure_knowledge_fts(db.get_bind())
    yield


app = FastAPI(title='MailMind', version='0.5.4.1', lifespan=lifespan)
app.include_router(ingest_router)
app.include_router(line_router)
app.include_router(line_agent_router)


@app.get('/health')
def health():
    return {'status': 'ok', 'service': 'MailMind', 'version': app.version}
