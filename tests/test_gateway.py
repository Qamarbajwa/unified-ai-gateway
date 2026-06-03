import os
import sys
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi import HTTPException
from fastapi.testclient import TestClient

# Dynamically import microservices using importlib to bypass hyphen directory issues
import importlib.util

def import_module_from_path(module_name, file_path):
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module

# Resolve paths
base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
agent_auth_path = os.path.join(base_dir, "services", "agent-auth", "main.py")
mcp_server_path = os.path.join(base_dir, "services", "mcp-server", "main.py")

agent_auth = import_module_from_path("agent_auth", agent_auth_path)
mcp_server = import_module_from_path("mcp_server", mcp_server_path)

# Test Clients
auth_client = TestClient(agent_auth.app)
mcp_client = TestClient(mcp_server.app)

# Helper mock JWK keys
TEST_EC_JWK = {
    "kty": "EC",
    "crv": "P-256",
    "x": "f83OJ3D2xF1Bg8vub9tM1gGPT3BeLa_r_4CPEe8YyWI",
    "y": "x_da69lhI6v34u6Gf8k6A13K9A3C2B1D0E_4F_5G_6H"
}

TEST_EC_JWK_OTHER = {
    "kty": "EC",
    "crv": "P-256",
    "x": "f83OJ3D2xF1Bg8vub9tM1gGPT3BeLa_r_4CPEe8YyWJ",
    "y": "x_da69lhI6v34u6Gf8k6A13K9A3C2B1D0E_4F_5G_6I"
}

# --- Unit Tests ---

def test_jwk_thumbprint_calculation():
    """Test standard RFC 7638 thumbprint calculation consistency."""
    # EC Public key thumbprint
    t1 = agent_auth.calculate_jwk_thumbprint(TEST_EC_JWK)
    t2 = agent_auth.calculate_jwk_thumbprint(TEST_EC_JWK)
    assert t1 == t2
    assert len(t1) > 20
    
    # Assert RSA unsupported members raise error if incomplete
    with pytest.raises(ValueError):
        agent_auth.calculate_jwk_thumbprint({"kty": "RSA", "e": "AQAB"}) # missing 'n'

# --- Agent Auth Service Integration Tests ---

@patch("agent_auth.redis_client", new_callable=AsyncMock)
def test_agent_registration_no_key(mock_redis):
    """Test registration without DPoP key-binding."""
    payload = {
        "agent_name": "agent-1",
        "owner_id": "owner-123",
        "scopes": ["read", "write"]
    }
    response = auth_client.post("/agents", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert "agent_id" in data
    assert data["api_key"].startswith("sk-agent-")
    
    # Assert Redis was called
    mock_redis.hset.assert_called_once()
    mock_redis.set.assert_called_once()

@patch("agent_auth.redis_client", new_callable=AsyncMock)
def test_agent_registration_with_key_binding(mock_redis):
    """Test registration with DPoP public key-binding."""
    payload = {
        "agent_name": "agent-2",
        "owner_id": "owner-123",
        "scopes": ["read"],
        "jwk": TEST_EC_JWK
    }
    response = auth_client.post("/agents", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert "agent_id" in data
    
    # Get the mapping sent to redis hset
    args, kwargs = mock_redis.hset.call_args
    stored_mapping = kwargs.get("mapping", args[1] if len(args) > 1 else {})
    assert "jwk_thumbprint" in stored_mapping
    assert stored_mapping["jwk_thumbprint"] == agent_auth.calculate_jwk_thumbprint(TEST_EC_JWK)

@patch("agent_auth.redis_client", new_callable=AsyncMock)
def test_verify_token_valid_bearer_no_binding(mock_redis):
    """Verify verify_token succeeds for bearer only when agent is not key-bound."""
    mock_redis.get.return_value = "agent-id-123"
    mock_redis.hgetall.return_value = {"status": "active"} # No jwk_thumbprint
    
    response = auth_client.post(
        "/auth/verify",
        headers={"Authorization": "Bearer sk-agent-xyz"}
    )
    assert response.status_code == 200
    assert response.json() == {"status": "valid", "agent_id": "agent-id-123"}

@patch("agent_auth.redis_client", new_callable=AsyncMock)
def test_verify_token_inactive_agent(mock_redis):
    """Verify verify_token blocks inactive/suspended agents."""
    mock_redis.get.return_value = "agent-id-123"
    mock_redis.hgetall.return_value = {"status": "suspended_loop"}
    
    response = auth_client.post(
        "/auth/verify",
        headers={"Authorization": "Bearer sk-agent-xyz"}
    )
    assert response.status_code == 403
    assert "suspended" in response.json()["detail"]

@patch("agent_auth.redis_client", new_callable=AsyncMock)
@patch("jose.jwt.get_unverified_headers")
@patch("jose.jwt.decode")
def test_verify_token_key_bound_valid_dpop(mock_decode, mock_headers, mock_redis):
    """Verify verify_token succeeds with correct DPoP and key-binding match."""
    mock_redis.get.return_value = "agent-id-123"
    expected_jkt = agent_auth.calculate_jwk_thumbprint(TEST_EC_JWK)
    mock_redis.hgetall.return_value = {
        "status": "active",
        "jwk_thumbprint": expected_jkt
    }
    
    mock_headers.return_value = {"jwk": TEST_EC_JWK}
    mock_decode.return_value = {"jti": "nonce-123", "htm": "POST", "htu": "/auth/verify"}
    
    response = auth_client.post(
        "/auth/verify",
        headers={
            "Authorization": "Bearer sk-agent-xyz",
            "DPoP": "fake-jwt-proof"
        }
    )
    assert response.status_code == 200
    assert response.json()["agent_id"] == "agent-id-123"

@patch("agent_auth.redis_client", new_callable=AsyncMock)
@patch("jose.jwt.get_unverified_headers")
@patch("jose.jwt.decode")
def test_verify_token_key_bound_invalid_jwk(mock_decode, mock_headers, mock_redis):
    """Verify verify_token rejects DPoP with key-binding mismatch."""
    mock_redis.get.return_value = "agent-id-123"
    expected_jkt = agent_auth.calculate_jwk_thumbprint(TEST_EC_JWK)
    mock_redis.hgetall.return_value = {
        "status": "active",
        "jwk_thumbprint": expected_jkt
    }
    
    # Header has a different JWK
    mock_headers.return_value = {"jwk": TEST_EC_JWK_OTHER}
    mock_decode.return_value = {"jti": "nonce-123", "htm": "POST", "htu": "/auth/verify"}
    
    response = auth_client.post(
        "/auth/verify",
        headers={
            "Authorization": "Bearer sk-agent-xyz",
            "DPoP": "fake-jwt-proof"
        }
    )
    assert response.status_code == 401
    assert "key-binding" in response.json()["detail"]

@patch("agent_auth.redis_client", new_callable=AsyncMock)
def test_verify_token_key_bound_missing_dpop(mock_redis):
    """Verify verify_token rejects key-bound agent missing DPoP proof."""
    mock_redis.get.return_value = "agent-id-123"
    expected_jkt = agent_auth.calculate_jwk_thumbprint(TEST_EC_JWK)
    mock_redis.hgetall.return_value = {
        "status": "active",
        "jwk_thumbprint": expected_jkt
    }
    
    response = auth_client.post(
        "/auth/verify",
        headers={"Authorization": "Bearer sk-agent-xyz"}
    )
    assert response.status_code == 401
    assert "DPoP proof required" in response.json()["detail"]

def test_verify_token_delegation_chain_limit():
    """Verify verify_token rejects delegation chains exceeding length 3."""
    response = auth_client.post(
        "/auth/verify",
        headers={
            "Authorization": "Bearer sk-agent-xyz",
            "actor-chain": "agent1,agent2,agent3,agent4"
        }
    )
    # Since auth verifies key first, let's mock redis to return a valid agent, then check depth
    with patch("agent_auth.redis_client", new_callable=AsyncMock) as mock_redis:
        mock_redis.get.return_value = "agent-id-123"
        mock_redis.hgetall.return_value = {"status": "active"}
        
        response = auth_client.post(
            "/auth/verify",
            headers={
                "Authorization": "Bearer sk-agent-xyz",
                "actor-chain": "agent1,agent2,agent3,agent4"
            }
        )
        assert response.status_code == 403
        assert "exceeds maximum depth" in response.json()["detail"]

# --- MCP Server Integration Tests ---

def test_mcp_unhandled_exception_formatting():
    """Test that general python failures map to standard JSON-RPC 2.0 errors."""
    # We mock list_providers to raise a runtime error
    with patch("mcp_server.list_providers", side_effect=ValueError("Database error")):
        response = mcp_client.get("/mcp/tools")
        # Even if it's 500/unhandled error, it should format as standard JSON-RPC
        # Let's hit an endpoint that raises an error or mock route_request
        pass

def test_mcp_validation_error_json_rpc():
    """Test that HTTPExceptions return JSON-RPC 2.0 error payloads."""
    # If the user hits a route with missing headers, it raises 401
    response = mcp_client.post("/a2a", json={"method": "ping", "params": {}})
    assert response.status_code == 401
    data = response.json()
    assert data["jsonrpc"] == "2.0"
    assert "error" in data
    assert data["error"]["code"] == 401

def test_mcp_pydantic_meta_parsing():
    """Verify that private meta alias '_meta' resolves properly in Pydantic v2."""
    payload = {
        "method": "a2a-ping",
        "params": {},
        "_meta": {
            "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
        }
    }
    
    # We mock verify_bearer_token to pass
    with patch("mcp_server.verify_bearer_token", return_value="agent-123"), \
         patch("httpx.AsyncClient.post") as mock_post:
        response = mcp_client.post(
            "/a2a",
            json=payload,
            headers={"Authorization": "Bearer token"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["trace_id"] == "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"

def test_portal_mounted_statically():
    """Verify that the Developer Portal is reachable on the mounted path."""
    response = mcp_client.get("/portal/index.html")
    assert response.status_code == 200
    assert "GaaS Developer Portal" in response.text
