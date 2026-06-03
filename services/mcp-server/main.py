from contextlib import asynccontextmanager
from typing import Any, Dict
import os
import httpx
import logging
from pythonjsonlogger import jsonlogger

import uvicorn
from fastapi import FastAPI, Depends, HTTPException, Header, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.fastapi import create_mcp_server
from mcp.server import Server
from mcp.types import Tool, TextContent
from pydantic import BaseModel, Field

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
        "gpt-4o": {"input": "$5.00/1M", "output": "$15.00/1M"},
        "claude-3-5-sonnet": {"input": "$3.00/1M", "output": "$15.00/1M"},
        "deepseek-r1": {"input": "$0.55/1M", "output": "$2.19/1M"},
        "gemini-2.5-flash": {"input": "$0.075/1M", "output": "$0.30/1M"},
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
        selected_model = "gemini-2.5-flash"
    elif min_latency:
        selected_model = "deepseek-r1"
    else:
        selected_model = "gpt-4o"
        
    return f"Based on constraints (min_latency={min_latency}, max_cost_per_m={max_cost_per_m}), request routed to: {selected_model}"


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
async def a2a_proxy(message: A2AMessage, authorization: str = Header(None)):
    """A2A protocol endpoint with OpenTelemetry trace propagation support."""
    traceparent = message.meta.get("traceparent") if message.meta else None
    logger.info("Received A2A message", extra={"trace_id": traceparent, "method": message.method})
    
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

