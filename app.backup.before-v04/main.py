"""MailMind FastAPI application entry point."""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.database import init_database
from app.routes.ingest import router as ingest_router
from app.routes.line_webhook import router as line_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_database()
    yield


app = FastAPI(title='MailMind', version='0.3.0', lifespan=lifespan)
app.include_router(ingest_router)
app.include_router(line_router)


@app.get('/health')
def health():
    return {'status': 'ok', 'service': 'MailMind', 'version': '0.3.0'}
