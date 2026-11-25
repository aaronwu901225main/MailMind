"""不需要 API key 的環境檢查。"""
from sqlalchemy import inspect
from app.config import GEMINI_API_KEY, GEMINI_MODEL
from app.database import engine, init_database


def main():
    init_database()
    tables = inspect(engine).get_table_names()
    print('✅ SQLite 初始化成功')
    print('📁 資料表：' + ', '.join(tables))
    print('🤖 預設模型：' + GEMINI_MODEL)
    print('🔑 Gemini API Key：' + ('已設定（不顯示內容）' if GEMINI_API_KEY else '尚未設定'))
    print('✅ Step 1 環境檢查完成')


if __name__ == '__main__':
    main()
