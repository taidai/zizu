from __future__ import annotations

from typing import Any
from uuid import UUID

import httpx
from fastapi import FastAPI


TEST_PASSWORD = "test-only-api-password"


class AuthenticatedApiClient:
    """Small authenticated ASGI client shared by current public API tests."""

    def __init__(self, app: FastAPI) -> None:
        from app.api.auth import router as auth_router
        from app.api.security import get_identity
        from app.services.identity import (
            Identity,
            InMemoryIdentityRepository,
            UserIdentity,
            hash_password,
        )

        password_hash = hash_password(TEST_PASSWORD, salt=b"api-fixture")
        users = [
            UserIdentity(UUID(int=index), role, password_hash, role, "active")
            for index, role in enumerate(("admin", "engineer", "operator"), start=1)
        ]
        identity = Identity(InMemoryIdentityRepository(users))
        app.include_router(auth_router, prefix="/api/v1")
        app.dependency_overrides[get_identity] = lambda: identity
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://testserver",
        )
        self._authorization: dict[str, str] = {}

    async def __aenter__(self) -> "AuthenticatedApiClient":
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self._client.__aexit__(*args)

    async def _bearer(self, role: str) -> str:
        if role not in self._authorization:
            response = await self._client.post(
                "/api/v1/auth/login",
                json={"username": role, "password": TEST_PASSWORD},
            )
            if response.status_code != 200:
                raise AssertionError(f"{role} login failed: {response.text}")
            self._authorization[role] = f"Bearer {response.json()['access_token']}"
        return self._authorization[role]

    @staticmethod
    def _role(method: str, path: str) -> str:
        if method == "GET" or "acknowledgements" in path:
            return "operator"
        return "engineer"

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = await self._bearer(self._role(method, path))
        return await self._client.request(method, path, headers=headers, **kwargs)

    async def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self._request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self._request("POST", path, **kwargs)
