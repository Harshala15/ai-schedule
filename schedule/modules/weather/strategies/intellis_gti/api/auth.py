"""Commercial client authentication and authorization handler."""

from __future__ import annotations

import os
from typing import Optional
from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader, APIKeyQuery

API_KEY_NAME = "X-API-Key"
api_key_header = APIKeyHeader(name=API_KEY_NAME, auto_error=False)
api_key_query = APIKeyQuery(name="api_key", auto_error=False)

DEFAULT_DEMO_KEY = "demo-key-2026"


def get_authorized_keys() -> set[str]:
    """Retrieve the set of allowed commercial client API keys from environment."""
    raw_keys = os.getenv("INTELLIS_CLIENT_KEYS", "").strip()
    if not raw_keys:
        return {DEFAULT_DEMO_KEY}
    return {k.strip() for k in raw_keys.split(",") if k.strip()}


def verify_client_api_key(
    header_key: Optional[str] = Security(api_key_header),
    query_key: Optional[str] = Security(api_key_query),
) -> str:
    """Validate client token from HTTP header or query parameter."""
    provided_key = (header_key or query_key or "").strip()
    authorized_keys = get_authorized_keys()

    if not provided_key or provided_key not in authorized_keys:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "Invalid or missing client API key. Provide a valid key via "
                "header 'X-API-Key: <key>' or query parameter '?api_key=<key>'."
            ),
        )

    return provided_key
