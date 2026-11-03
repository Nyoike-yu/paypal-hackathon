"""stand-in for `npx @paypal/mcp --tools=orders.get`, for tests. speaks the real MCP protocol over stdio and offers get_order(id)"""
import json, sys
try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    from mcp.server.mcpserver import MCPServer as FastMCP

app = FastMCP("fake-paypal")

@app.tool()
def get_order(id: str) -> str:
    if id == "BAD-AUTH": raise RuntimeError("401 AUTHENTICATION_FAILURE")
    if id == "NOT-JSON": return "something went wrong"
    if id == "OTHER-ORDER": return json.dumps({"id": "SOMEONE-ELSE", "status": "COMPLETED"})
    return "Order details: " + json.dumps({
        "id": id, "status": "COMPLETED", "create_time": "2026-10-30T10:00:00Z",
        "payer": {"email_address": "private@example.com", "name": {"given_name": "Secret"}},
        "purchase_units": [{"amount": {"currency_code": "USD", "value": "20.00"},
                            "shipping": {"address": {"address_line_1": "1 Private Street"}},
                            "payments": {"captures": [{"id": "CAP1", "status": "COMPLETED", "amount": {"currency_code": "USD", "value": "20.00"}}]}}]})

if __name__ == "__main__":
    app.run()
