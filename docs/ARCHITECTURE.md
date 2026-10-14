# MailMind 架構規劃（v0.1）

## 核心原則

- 免費額度優先，超量後停止與提醒，不自動改用收費方案。
- 每封郵件以 `(account_id, provider_message_id)` 去重，不能靠是否已讀判斷。
- 所有郵件、摘要、推播都能追溯到其擁有者；未通過授權不得存取其他使用者資料。
- 初期不儲存郵件全文；需要時保存脫敏預覽及摘要，附件採最短保留期限。
- API key、LINE token、未來的憑證加密金鑰不得入庫或提交 Git。
- LLM 回傳只能作為摘要資料，不得自動執行郵件內的指令。
- Webhook 必須驗證 LINE 簽章；收信端點也要為每個信箱設置隨機 token 驗證。
- 使用者和郵件處理進入背景任務隊列之前，要考慮佇列可靠性與重試上限。

## v0.1 → v1.0 演進

1. v0.1：FastAPI + SQLite + Mock 郵件 + Gemini 摘要測試。
2. v0.2：使用者及信箱綁定、Apps Script 定時讀取新郵件、後端安全接收。
3. v0.3：LINE 推播、摘要等級設定、訊息額度管理。
4. v0.4：PDF、圖片、DOCX 附件解析、結構化摘要與時區處理。
5. v0.5：FastMCP 封裝附件解析工具、使用者互動搜尋。
6. v1.0：可靠性、安全性與適用於少量使用者的部署方式。

## 未來部署

開發期間只在 `127.0.0.1:8000` 執行。當要讓 Apps Script 或 LINE 連線時，
必須配置可信的 HTTPS 公網入口。臨時 Cloudflare Tunnel 僅供開發測試。
Cloud Run 等服務即便有免費額度也可能需要啟用帳單帳戶，並不保證費用永遠為 0。

## OAuth 補充

若轉向 Gmail API，`gmail.readonly` 為受限 OAuth scope，公開應用涉及額外驗證。
Google OAuth 外部 Testing 狀態下，使用非基本登入 scopes 的 refresh token 通常七天到期；
不要直接把測試狀態當成長期多使用者服務的部署方案。
