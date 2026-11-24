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

LINE_CHANNEL_SECRET = os.getenv('MAILMIND_LINE_CHANNEL_SECRET', '').strip()
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('MAILMIND_LINE_CHANNEL_ACCESS_TOKEN', '').strip()
MAX_LINE_PUSHES_PER_MONTH = int(os.getenv('MAX_LINE_PUSHES_PER_MONTH', '150'))
if MAX_LINE_PUSHES_PER_MONTH < 0:
    raise ValueError('MAX_LINE_PUSHES_PER_MONTH must be >= 0')

# v0.4: Sensitive images are never sent to Gemini unless explicitly enabled.
ENABLE_CLOUD_IMAGE_ANALYSIS = os.getenv('MAILMIND_ENABLE_IMAGE_ANALYSIS', 'false').lower() == 'true'
# Each MCP stdio process can only access one pre-authorized email account.
MCP_ACCOUNT_ID = os.getenv('MAILMIND_MCP_ACCOUNT_ID', '').strip()

# v0.5.2: Local-only vector search. No model download or inference at API startup.
EMBEDDING_MODEL = os.getenv('MAILMIND_EMBEDDING_MODEL', 'intfloat/multilingual-e5-small').strip()
ENABLE_SEMANTIC_QUERY = os.getenv('MAILMIND_ENABLE_SEMANTIC_QUERY', 'false').strip().lower() == 'true'
VECTOR_SEARCH_CANDIDATES = min(5000, max(10, int(os.getenv('MAILMIND_VECTOR_CANDIDATES', '2000'))))

# v0.5.2.1: Explicit opt-in. MCP never sends private email snippets to Gemini
# unless this is enabled; the local CLI requires --rerank separately.
ENABLE_GEMINI_RERANK = os.getenv('MAILMIND_ENABLE_GEMINI_RERANK', 'false').strip().lower() == 'true'
