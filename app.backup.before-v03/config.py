"""Central configuration. Keep secrets in local .env, never in source control."""
import os
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / '.env')

GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '').strip()
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-3.5-flash-lite').strip()
DATABASE_URL = os.getenv('DATABASE_URL', '').strip() or (
    f"sqlite:///{PROJECT_ROOT / 'data' / 'mailmind.db'}"
)
MAX_DAILY_SUMMARIES = int(os.getenv('MAX_DAILY_SUMMARIES', '10'))
if MAX_DAILY_SUMMARIES < 1:
    raise ValueError('MAX_DAILY_SUMMARIES must be >= 1')
