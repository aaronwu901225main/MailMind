"""MailMind FastAPI 應用入口。"""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.database import init_database

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_database()
    yield

app = FastAPI(title='MailMind', version='0.1.0', lifespan=lifespan)

@app.get('/health')
def health():
    return {'status': 'ok', 'service': 'MailMind', 'version': '0.1.0'}
