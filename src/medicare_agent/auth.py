from __future__ import annotations

import secrets
from functools import lru_cache
from typing import Annotated

import jwt
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient
from jwt.exceptions import PyJWTError

from medicare_agent.config import Settings, get_settings

bearer = HTTPBearer(auto_error=False)


@lru_cache(maxsize=8)
def _jwks_client(url: str) -> PyJWKClient:
    return PyJWKClient(url, cache_jwk_set=True, lifespan=300, timeout=5)


def _authenticate_oidc(token: str, settings: Settings) -> str:
    jwks_url, issuer, audience = (
        settings.oidc_jwks_url,
        settings.oidc_issuer,
        settings.oidc_audience,
    )
    if not jwks_url or not issuer or not audience:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OIDC reviewer authentication is not configured",
        )
    try:
        signing_key = _jwks_client(jwks_url).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except (PyJWTError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid reviewer bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    # Some identity providers emit a space-delimited "scope", others an "scp" list.
    raw_scope = claims.get("scope", claims.get("scp", ""))
    scopes = (
        {str(item) for item in raw_scope}
        if isinstance(raw_scope, list)
        else set(str(raw_scope).split())
    )
    if settings.oidc_required_scope not in scopes:
        raise HTTPException(status_code=403, detail="Reviewer scope is required")
    amr = claims.get("amr", [])
    if settings.oidc_require_mfa and (not isinstance(amr, list) or "mfa" not in amr):
        raise HTTPException(status_code=403, detail="Reviewer MFA claim is required")
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.strip() or len(subject) > 160:
        raise HTTPException(status_code=401, detail="Invalid reviewer subject")
    return subject


def require_service(
    settings: Annotated[Settings, Depends(get_settings)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    x_service_key: Annotated[str | None, Header()] = None,
) -> str:
    supplied = x_service_key or (
        credentials.credentials
        if credentials is not None and credentials.scheme.lower() == "bearer"
        else ""
    )
    if not supplied or not any(
        secrets.compare_digest(supplied, expected) for expected in settings.service_keys()
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid service credentials"
        )
    return "automation-service"


def require_reviewer(
    settings: Annotated[Settings, Depends(get_settings)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    x_reviewer_key: Annotated[str | None, Header()] = None,
) -> str:
    if settings.reviewer_auth_mode == "oidc":
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Reviewer bearer token is required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return _authenticate_oidc(credentials.credentials, settings)
    reviewer_id = settings.authenticate_reviewer(x_reviewer_key or "")
    if reviewer_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid reviewer credentials"
        )
    return reviewer_id
