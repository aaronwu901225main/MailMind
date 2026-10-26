"""Run the same functions exposed through MCP without configuring an MCP client."""
import argparse
from app import mcp_tools
from app.database import init_database


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--account-id', required=True)
    parser.add_argument('--keyword', default='')
    args = parser.parse_args()
    init_database()
    # Local debug only; never expose this helper as a public API.
    mcp_tools.MCP_ACCOUNT_ID = args.account_id
    for mail in mcp_tools.search_emails(args.keyword, 5):
        print(f"{mail['email_id']} | {mail['subject'][:80]} | {mail['state']}")
        for attachment in mcp_tools.list_email_attachments(mail['email_id']):
            print(f"  ↳ {attachment['filename']} [{attachment['status']}]")


if __name__ == '__main__':
    main()
