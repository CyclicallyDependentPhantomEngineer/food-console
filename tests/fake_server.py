"""Tiny stand-in MCP server used by the smoke test."""
from mcp.server.mcpserver import MCPServer as FastMCP
from mcp.types import ToolAnnotations

app = FastMCP("fake-zomato")

@app.tool(annotations=ToolAnnotations(read_only_hint=True))
def get_order_history(limit: int = 3) -> str:
    """Return past orders."""
    return '[{"restaurant": "Biryani House", "total": 450}]'[:1000]

@app.tool(annotations=ToolAnnotations(read_only_hint=True))
def search_menu(keyword: str) -> str:
    """Search dishes."""
    return '{"items": [{"variant_id": "v_1", "name": "Tikka", "nutrition": {"calories": 300, "protein": 30}}]}'

_polls = {"n": 0}

@app.tool(annotations=ToolAnnotations(read_only_hint=True))
def payment_status() -> str:
    """Payment status; changes between calls."""
    _polls["n"] += 1
    return "paid" if _polls["n"] > 1 else "pending"

@app.tool()
def place_order(cart_id: str) -> str:
    """Place an order for a cart."""
    return f"placed {cart_id}"

if __name__ == "__main__":
    app.run()
