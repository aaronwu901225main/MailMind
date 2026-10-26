"""MailMind stdio MCP Server. Must never be exposed via the public Flask tunnel."""
from mcp.server import MCPServer
from app.mcp_tools import (search_emails, get_email_summary,
                           list_email_attachments, read_attachment_text)

mcp = MCPServer('MailMind MCP')

mcp.tool()(search_emails)
mcp.tool()(get_email_summary)
mcp.tool()(list_email_attachments)
mcp.tool()(read_attachment_text)


def main():
    from app.database import init_database
    from app.mcp_tools import account_id_or_raise
    init_database()
    account_id_or_raise()  # fail closed: do not launch with unrestricted account access
    mcp.run()  # MCP SDK 2.x defaults to stdio


if __name__ == '__main__':
    main()
