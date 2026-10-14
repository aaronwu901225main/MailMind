# MailMind v0.1 — AI Email Assistant（專案啟動版）

這是第一階段的可執行專案骨架，**不是已完成的收信推播系統**。目標是先確認 Python、FastAPI、SQLite、Gemini API 的環境正常，並從第一天就預留多人資料結構。

## 需求

- Python 3.10 以上（建議 3.11 或 3.12）
- macOS / Linux / Windows (WSL)
- Gemini API key：只有「真實模型摘要」步驟需要

## 1. 建立虛擬環境

```bash
cd mailmind-starter
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```

> Windows PowerShell 可使用 `.venv\\Scripts\\Activate.ps1`。不要上傳 `.env`。

## 2. 第一個本地測試：資料庫與設定

```bash
python -m scripts.check_setup
pytest -q
```

預期看到 SQLite 初始化成功、5 張資料表以及測試通過。`data/mailmind.db` 會自動建立。

## 3. 啟動 FastAPI

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

另開一個 Terminal：

```bash
curl http://127.0.0.1:8000/health
```

預期：`{"status":"ok","service":"MailMind","version":"0.1.0"}`。

這個版本僅有 `/health`，刻意**不**開放無需登入的 LLM 摘要端點，避免公開測試時被濫用免費額度。

## 4. 選做：使用真實 Gemini API 摘要假郵件

1. 前往 https://aistudio.google.com/apikey 建立可用的 Gemini API key。
2. 把 key 填進自己的 `.env`：`GEMINI_API_KEY=你的密鑰`。
3. 執行：

```bash
python -m scripts.demo_summary --level short
python -m scripts.demo_summary --level medium
python -m scripts.demo_summary --level full
```

程式只會傳送 `scripts/demo_summary.py` 中的**虛構郵件**到 Gemini。
如果出現 429、quota、region 等錯誤，先在 Google AI Studio 查可用模型及免費額度；不要新增帳單資訊來「修好」這些錯誤。

## 注意事項

- 本版尚未整合 Gmail、Apps Script、LINE、附件或 MCP。
- 不要把 Gmail 郵件、API key、OAuth refresh token 或 LINE secret 貼到聊天紀錄／GitHub。
- 免費 Gemini 方案可能將傳入資料用於改善產品，暫時只用假郵件。
- 架構詳見 `docs/ARCHITECTURE.md`。

## 完成 v0.1 的驗收

- [ ] `python -m scripts.check_setup` 正常
- [ ] `pytest -q` 全數通過
- [ ] `/health` 回傳 `status=ok`
- [ ] 選做：Gemini 成功輸出繁體中文摘要

完成後我們再開始 **v0.2：多使用者帳號綁定 + Google Apps Script 自動收信**。
