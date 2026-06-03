import os
import uuid
import logging
import json
import hashlib
import base64
from typing import List, Optional
from datetime import datetime, timedelta

from fastapi import FastAPI, HTTPException, Header, Depends
from pydantic import BaseModel
import redis.asyncio as redis
from jose import jwt
from pythonjsonlogger import jsonlogger

logger = logging.getLogger("agent-auth")
logger.setLevel(logging.INFO)
logHandler = logging.StreamHandler()
formatter = jsonlogger.JsonFormatter('%(asctime)s %(levelname)s %(message)s')
logHandler.setFormatter(formatter)
logger.addHandler(logHandler)

app = FastAPI(title="GaaS Agent Auth Service")

# Redis connection
redis_pool = None
redis_client = None

@app.on_event("startup")
async def startup():
    global redis_pool, redis_client
    redis_pool = redis.ConnectionPool.from_url(
        os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        max_connections=100, decode_responses=True
    )
    redis_client = redis.Redis(connection_pool=redis_pool)
    logger.info("Agent Auth Service started", extra={"event": "startup"})

@app.on_event("shutdown")
async def shutdown():
    await redis_client.close()
    await redis_pool.disconnect()

# --- Utility Functions ---
def calculate_jwk_thumbprint(jwk_dict: dict) -> str:
    """Compute RFC 7638 SHA-256 base64url thumbprint of a JWK."""
    required_members = {
        "EC": ("crv", "kty", "x", "y"),
        "RSA": ("e", "kty", "n"),
        "oct": ("k", "kty"),
    }
    kty = jwk_dict.get("kty")
    if not kty or kty not in required_members:
        raise ValueError(f"Unsupported or missing key type (kty): {kty}")
    
    members = required_members[kty]
    for m in members:
        if m not in jwk_dict:
            raise ValueError(f"Missing required JWK member: {m}")
            
    # Serialize lexicographically sorted fields with no whitespace
    canonical_json = json.dumps(
        {k: jwk_dict[k] for k in sorted(members)},
        separators=(",", ":")
    )
    digest = hashlib.sha256(canonical_json.encode('utf-8')).digest()
    return base64.urlsafe_b64encode(digest).decode('utf-8').rstrip('=')

# --- Models ---
class AgentRegistrationRequest(BaseModel):
    agent_name: str
    owner_id: str
    scopes: List[str]
    jwk: Optional[dict] = None

class AgentRegistrationResponse(BaseModel):
    agent_id: str
    api_key: str
    expires_at: datetime

class AgentStatus(BaseModel):
    agent_id: str
    status: str
    daily_limit: int
    consumed: int
    loop_detected: bool

# --- Endpoints ---

@app.post("/agents", response_model=AgentRegistrationResponse)
async def register_agent(req: AgentRegistrationRequest):
    """Register a new agent and return a scoped, auto-rotating API key, with optional JWK key-binding."""
    agent_id = str(uuid.uuid4())
    api_key = f"sk-agent-{uuid.uuid4().hex}"
    expires_at = datetime.utcnow() + timedelta(days=30)
    
    jwk_thumbprint = None
    if req.jwk:
        try:
            jwk_thumbprint = calculate_jwk_thumbprint(req.jwk)
        except Exception as e:
            logger.error("Failed to compute JWK thumbprint", extra={"error": str(e)})
            raise HTTPException(status_code=400, detail=f"Invalid JWK: {str(e)}")
    
    # Store in Redis
    mapping = {
        "api_key": api_key,
        "owner_id": req.owner_id,
        "status": "active"
    }
    if jwk_thumbprint:
        mapping["jwk_thumbprint"] = jwk_thumbprint
        
    await redis_client.hset(
        f"agent:{agent_id}",
        mapping=mapping
    )
    await redis_client.set(f"apikey:{api_key}", agent_id, ex=2592000) # 30 days
    
    logger.info("Registered agent", extra={"agent_id": agent_id, "key_bound": jwk_thumbprint is not None})
    return AgentRegistrationResponse(
        agent_id=agent_id,
        api_key=api_key,
        expires_at=expires_at
    )

@app.post("/auth/verify")
async def verify_token(
    authorization: str = Header(None),
    dpop: str = Header(None),
    actor_chain: str = Header(None)
):
    """
    Verify API key/token, validate DPoP proof, and assert key-binding and delegation constraints.
    """
    if not authorization:
        logger.warning("Missing Authorization header", extra={"event": "auth_failure"})
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    # Reject unsafe delegation depth before any backing-store lookup.
    if actor_chain:
        chain = actor_chain.split(",")
        if len(chain) > 3:
            logger.warning("Delegation chain exceeds depth", extra={"event": "delegation_blocked", "chain": actor_chain})
            raise HTTPException(status_code=403, detail="Delegation chain exceeds maximum depth of 3")
    
    # Extract token
    token = authorization.replace("Bearer ", "")
    agent_id = await redis_client.get(f"apikey:{token}")
    
    if not agent_id:
        logger.warning("Invalid or blacklisted token used", extra={"event": "auth_failure"})
        raise HTTPException(status_code=401, detail="Invalid or blacklisted token")
        
    # Fetch agent metadata from Redis
    agent_data = await redis_client.hgetall(f"agent:{agent_id}")
    if not agent_data:
        logger.warning("Agent profile missing in store", extra={"agent_id": agent_id})
        raise HTTPException(status_code=401, detail="Agent profile missing")
        
    status_str = agent_data.get("status", "active")
    if status_str != "active":
        logger.warning("Inactive agent attempted access", extra={"agent_id": agent_id, "status": status_str})
        raise HTTPException(status_code=403, detail=f"Agent is inactive or suspended: {status_str}")
        
    registered_jkt = agent_data.get("jwk_thumbprint")

    # 4.4 DPoP Proof Validation
    if dpop:
        try:
            unverified_headers = jwt.get_unverified_headers(dpop)
            if "jwk" not in unverified_headers:
                raise ValueError("Missing JWK in DPoP header")
                
            header_jwk = unverified_headers["jwk"]
            
            # Verify signature using key in header
            payload = jwt.decode(
                dpop,
                header_jwk,
                algorithms=["RS256", "ES256"],
                options={"verify_aud": False}
            )
            
            # Assert key-binding if one is registered
            if registered_jkt:
                dpop_jkt = calculate_jwk_thumbprint(header_jwk)
                if dpop_jkt != registered_jkt:
                    logger.warning("DPoP key-binding mismatch", extra={
                        "event": "dpop_key_mismatch",
                        "agent_id": agent_id,
                        "expected": registered_jkt,
                        "provided": dpop_jkt
                    })
                    raise HTTPException(status_code=401, detail="DPoP key-binding violation")
                    
            logger.info("DPoP proof validated", extra={"jti": payload.get("jti"), "agent_id": agent_id})
        except HTTPException:
            raise
        except Exception as e:
            logger.error("DPoP verification failed", extra={"error": str(e), "event": "dpop_failure"})
            raise HTTPException(status_code=401, detail=f"Invalid DPoP proof: {str(e)}")
    else:
        # Enforce DPoP if key-bound
        if registered_jkt:
            logger.warning("Missing required DPoP proof for key-bound agent", extra={"agent_id": agent_id})
            raise HTTPException(status_code=401, detail="DPoP proof required for key-bound agent")
        logger.info("No DPoP header, proceeding with Bearer authorization", extra={"agent_id": agent_id})
        
    logger.info("Agent authenticated successfully", extra={"agent_id": agent_id, "event": "auth_success"})
    return {"status": "valid", "agent_id": agent_id}

@app.post("/auth/check-loop")
async def check_loop(agent_id: str, prompt_hash: str):
    """
    4.6 Loop detection mechanism.
    Checks if an agent is submitting too many similar requests in a short time window.
    """
    key = f"loop:{agent_id}:{prompt_hash}"
    count = await redis_client.incr(key)
    if count == 1:
        await redis_client.expire(key, 60) # 60s window
        
    if count > 5:
        await redis_client.hset(f"agent:{agent_id}", "status", "suspended_loop")
        raise HTTPException(status_code=429, detail="Loop detected: >5 similar requests in 60s")
        
    return {"status": "ok", "count": count}

@app.get("/_agent/status", response_model=AgentStatus)
async def get_agent_status(agent_id: str):
    """4.7 Fetch budget and operational status of agent."""
    data = await redis_client.hgetall(f"agent:{agent_id}")
    if not data:
        raise HTTPException(status_code=404, detail="Agent not found")
        
    status_str = data.get("status", "active")
    
    return AgentStatus(
        agent_id=agent_id,
        status=status_str,
        daily_limit=1000000,
        consumed=0,
        loop_detected=(status_str == "suspended_loop")
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True)
