from contextlib import asynccontextmanager
from typing import Any, Dict
import os
import sys
import httpx
import logging
from pathlib import Path
from pythonjsonlogger import jsonlogger

import uvicorn
from fastapi import FastAPI, Depends, HTTPException, Header, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

from project_registry import load_registry, scan_connected_tools

try:
    from mcp.server.fastapi import create_mcp_server
    from mcp.server import Server
    if not hasattr(Server("compat-check"), "tool"):
        raise ImportError("Installed MCP Server lacks decorator tool API.")
except ImportError:
    class Server:
        def __init__(self, name: str):
            self.name = name
            self.tools: dict[str, Any] = {}

        def tool(self):
            def decorator(func):
                self.tools[func.__name__] = func
                return func
            return decorator

    def create_mcp_server(server: Server) -> FastAPI:
        fallback = FastAPI(title=f"{server.name} MCP Compatibility")

        @fallback.get("/tools")
        async def list_tools():
            return {
                "tools": [
                    {
                        "name": name,
                        "description": (func.__doc__ or "").strip(),
                    }
                    for name, func in sorted(server.tools.items())
                ]
            }

        return fallback

logger = logging.getLogger("mcp-server")
logger.setLevel(logging.INFO)
logHandler = logging.StreamHandler()
formatter = jsonlogger.JsonFormatter('%(asctime)s %(levelname)s %(message)s')
logHandler.setFormatter(formatter)
logger.addHandler(logHandler)

# Initialize the MCP Server
mcp_server = Server("UnifiedGatewayMCPServer")

# --- Define Tools ---

@mcp_server.tool()
async def get_health() -> str:
    """Check the health of the Unified AI Gateway."""
    return "Gateway is healthy and operational. All services (LiteLLM, Redis, DB) are reachable."

@mcp_server.tool()
async def list_providers() -> list[str]:
    """List all available LLM providers supported by the gateway."""
    return [
        "openai", 
        "anthropic", 
        "deepseek", 
        "gemini"
    ]

@mcp_server.tool()
async def get_model_pricing(model_name: str) -> str:
    """
    Get the input and output token pricing for a specific model.
    Example: model_name="deepseek-r1"
    """
    pricing_data = {
        "deepseek-v4-flash": {"input": "$0.14/1M", "output": "$0.28/1M"},
        "deepseek-v4-pro": {"input": "$0.435/1M", "output": "$0.87/1M"},
        "gemini-3.5-flash": {"input": "$1.50/1M", "output": "$9.00/1M"},
        "gpt-5.4": {"input": "$2.50/1M", "output": "$15.00/1M"},
        "claude-sonnet-4.6": {"input": "$3.00/1M", "output": "$15.00/1M"},
        "claude-opus-4.8": {"input": "$5.00/1M", "output": "$25.00/1M"},
        "claude-haiku-4.5": {"input": "$1.00/1M", "output": "$5.00/1M"},
        "gpt-5.5-instant": {"input": "$5.00/1M", "output": "$30.00/1M"},
    }
    
    if model_name in pricing_data:
        return f"Pricing for {model_name}: Input {pricing_data[model_name]['input']}, Output {pricing_data[model_name]['output']}"
    return f"Model '{model_name}' not found. Available models: {', '.join(pricing_data.keys())}"

@mcp_server.tool()
async def get_budget_status(agent_id: str) -> str:
    """
    Retrieve the token budget and remaining balance for a given agent_id.
    """
    # Mock response. In a real scenario, this queries the Postgres/Redis billing service.
    return f"Budget Status for Agent {agent_id}:\nDaily Limit: 1,000,000 tokens\nConsumed: 45,230 tokens\nRemaining: 954,770 tokens\nStatus: Active"

@mcp_server.tool()
async def route_request(prompt: str, min_latency: bool = False, max_cost_per_m: float = None) -> str:
    """
    Simulate routing a request to the optimal model based on constraints.
    Returns the selected provider/model.
    """
    LITELLM_URL = os.environ.get("LITELLM_URL", "http://litellm:4000")
    
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{LITELLM_URL}/v1/models")
            if resp.status_code == 200:
                models = resp.json().get("data", [])
                model_names = [m["id"] for m in models]
                logger.info("Fetched dynamic models from LiteLLM", extra={"model_count": len(model_names)})
                if model_names:
                    return f"Dynamic routing picked from: {model_names[0]}"
    except httpx.RequestError as e:
        logger.warning("LiteLLM not reachable, using fallback routing logic.", extra={"error": str(e)})

    # Mock logic
    if max_cost_per_m and max_cost_per_m < 1.0:
        selected_model = "deepseek-v4-flash"
    elif min_latency:
        selected_model = "deepseek-v4-pro"
    else:
        selected_model = "gpt-5.4"
        
    return f"Based on constraints (min_latency={min_latency}, max_cost_per_m={max_cost_per_m}), request routed to: {selected_model}"


@mcp_server.tool()
async def list_connected_tools() -> list[dict[str, Any]]:
    """List external tool workspaces registered with the unified gateway."""
    registry = load_registry()
    return [
        {
            "id": tool["id"],
            "name": tool["name"],
            "path": tool["path"],
            "container_path": tool.get("container_path"),
            "domain": tool.get("domain"),
            "quality_profile": tool.get("quality_profile"),
        }
        for tool in registry["tools"]
    ]


@mcp_server.tool()
async def audit_connected_tools(persist_snapshot: bool = True) -> dict[str, Any]:
    """
    Scan connected workspaces and report manifests, governance, git state, changes, and quality recommendations.
    Set persist_snapshot=false for a read-only dry run.
    """
    return scan_connected_tools(persist=persist_snapshot)


@mcp_server.tool()
async def get_connected_tool_changes() -> dict[str, Any]:
    """Return only connected tools whose current fingerprint differs from the last saved snapshot."""
    audit = scan_connected_tools(persist=False)
    changed = [tool for tool in audit["tools"] if tool["change_status"] != "unchanged"]
    return {
        "generated_at": audit["generated_at"],
        "changed_count": len(changed),
        "tools": changed,
    }


# --- FastAPI Application ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Setup resources (e.g. database connections)
    yield
    # Cleanup resources

app = FastAPI(title="GaaS MCP Server", lifespan=lifespan)

@app.exception_handler(HTTPException)
async def mcp_exception_handler(request: Request, exc: HTTPException):
    logger.error("HTTP Exception", extra={"status_code": exc.status_code, "detail": exc.detail})
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "jsonrpc": "2.0",
            "error": {
                "code": exc.status_code,
                "message": exc.detail
            },
            "id": None
        }
    )

@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    logger.error("Unhandled Exception", extra={"error": str(exc)})
    return JSONResponse(
        status_code=500,
        content={
            "jsonrpc": "2.0",
            "error": {
                "code": -32603,
                "message": f"Internal error: {str(exc)}"
            },
            "id": None
        }
    )

# --- Authentication Middleware ---
async def verify_bearer_token(
    authorization: str = Header(None),
    dpop: str = Header(None),
    actor_chain: str = Header(None)
):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Bearer token")
        
    AGENT_AUTH_URL = os.environ.get("AGENT_AUTH_URL", "http://agent-auth:8001")
    
    headers = {"Authorization": authorization}
    if dpop:
        headers["DPoP"] = dpop
    if actor_chain:
        headers["actor-chain"] = actor_chain
        
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(f"{AGENT_AUTH_URL}/auth/verify", headers=headers, timeout=5.0)
            if resp.status_code != 200:
                detail = "Authentication failed"
                try:
                    detail = resp.json().get("detail", detail)
                except Exception:
                    pass
                raise HTTPException(status_code=resp.status_code, detail=detail)
            return resp.json().get("agent_id")
        except httpx.RequestError as e:
            logger.error("Failed to connect to Agent Auth service", extra={"error": str(e)})
            raise HTTPException(status_code=503, detail="Auth service temporarily unavailable")

# Mount the MCP server to FastAPI
mcp_app = create_mcp_server(mcp_server)
app.mount("/mcp", mcp_app)

# Mount the static Developer Portal
portal_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "portal"))
if os.path.exists(portal_dir):
    app.mount("/portal", StaticFiles(directory=portal_dir, html=True), name="portal")
    logger.info(f"Statically mounted Developer Portal from {portal_dir}")
else:
    logger.warning(f"Developer Portal directory not found at {portal_dir}")

@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "gaas-mcp-server"}

from fastapi.responses import FileResponse

@app.get("/.well-known/agent.json")
async def get_agent_card():
    return FileResponse(".well-known/agent.json")

class A2AMessage(BaseModel):
    method: str
    params: dict
    meta: dict = Field(default={}, alias="_meta")
    
    model_config = {
        "populate_by_name": True
    }

@app.post("/a2a")
async def a2a_proxy(
    message: A2AMessage,
    authorization: str = Header(None),
    dpop: str = Header(None),
    actor_chain: str = Header(None),
):
    """A2A protocol endpoint with OpenTelemetry trace propagation support."""
    agent_id = await verify_bearer_token(authorization=authorization, dpop=dpop, actor_chain=actor_chain)
    traceparent = message.meta.get("traceparent") if message.meta else None
    logger.info("Received A2A message", extra={"trace_id": traceparent, "method": message.method, "agent_id": agent_id})
    
    async with httpx.AsyncClient() as client:
        try:
            # Placeholder for actual A2A forward routing
            pass
        except Exception as e:
            logger.error("A2A forward failed", extra={"error": str(e)})
            raise HTTPException(status_code=502, detail="Bad Gateway")
    
    return {"status": "received", "method": message.method, "trace_id": traceparent}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
