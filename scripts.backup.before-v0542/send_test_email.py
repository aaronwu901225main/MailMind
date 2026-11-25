"""Send a fake email into the authenticated API without exposing real Gmail."""
import argparse
import os
import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8000')
    parser.add_argument('--account-id', default=os.environ.get('MAILMIND_ACCOUNT_ID'))
    parser.add_argument('--token', default=os.environ.get('MAILMIND_INGEST_TOKEN'))
    parser.add_argument('--message-id', default='local-demo-v02')
    args = parser.parse_args()
    if not args.account_id or not args.token:
        parser.error('Set MAILMIND_ACCOUNT_ID and MAILMIND_INGEST_TOKEN environment variables first.')
    body = {
        'account_id': args.account_id,
        'message_id': args.message_id,
        'sender': 'demo@example.com',
        'subject': 'MailMind 測試信件',
        'body': '這是虛構測試信。請於 2026/10/18 18:00 前提交專案報告，10/19 10:00 開會。',
    }
    response = httpx.post(args.url.rstrip('/') + '/api/v1/ingest/gmail',
                          json=body, headers={'X-MailMind-Token': args.token}, timeout=90)
    print('HTTP', response.status_code)
    print(response.text)
    response.raise_for_status()


if __name__ == '__main__':
    main()
