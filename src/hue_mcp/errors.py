from mcp.server.mcpserver.exceptions import ToolError


class HueError(ToolError):
    """An anticipated failure; as a ToolError, the MCP server shows its message to the model."""
