# 將這段加入既有電費機器人 app.py，與既有 MailMind Proxy 路由放在一起。
# app / requests / request / Response 已在原程式中 import，不需再加 import。
# MAILMIND_URL 已定義為 http://127.0.0.1:8000。

@app.route('/mailmind/line/webhook', methods=['POST'])
def mailmind_line_webhook_proxy():
    try:
        upstream = requests.post(
            f'{MAILMIND_URL}/line/webhook',
            data=request.get_data(),  # 必須保留原始 bytes，LINE HMAC 驗證依賴此內容
            headers={
                'Content-Type': request.headers.get('Content-Type', 'application/json'),
                'X-Line-Signature': request.headers.get('X-Line-Signature', ''),
            },
            timeout=25,
        )
        return Response(upstream.content, status=upstream.status_code,
                        content_type='application/json')
    except requests.RequestException:
        return {'error': 'MailMind unavailable'}, 502
