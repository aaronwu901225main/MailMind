"""集中管理設定；絕不把 API key 寫在程式碼中。"""
import os
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / '.env')

GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '').strip()
GEMINI_MODEL = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash-lite').strip()
DATABASE_URL = os.getenv('DATABASE_URL', '').strip() or (
    f"sqlite:///{PROJECT_ROOT / 'data' / 'mailmind.db'}"
)

# 第一期只有本機健康檢查，LLM 測試透過 CLI 執行。
# 尚未提供會公開消耗 API 額度的匿名 HTTP 端點。
