"""Public health seam for the immutable release lock."""
from __future__ import annotations

import asyncio
import unittest
from uuid import UUID

import httpx
from fastapi import FastAPI

from app.api import health
from app.api.auth import router as auth_router
from app.api.security import get_identity
from app.services.identity import Identity, InMemoryIdentityRepository, UserIdentity, hash_password


class ReleaseLockHealthPublicApiTest(unittest.TestCase):
    def test_authenticated_health_exposes_a_sanitized_current_release_lock(self) -> None:
        app = FastAPI()
        app.include_router(health.router, prefix="/api/v1")
        app.include_router(auth_router, prefix="/api/v1")
        identity = Identity(
            InMemoryIdentityRepository(
                [
                    UserIdentity(
                        UUID("00000000-0000-0000-0000-000000000001"),
                        "operator",
                        hash_password("test-password", salt=b"release-lock-health"),
                        "operator",
                        "active",
                    )
                ]
            )
        )
        app.dependency_overrides[get_identity] = lambda: identity
        health.set_release_lock_summary_provider(
            lambda: {
                "status": "locked",
                "id": "00000000-0000-0000-0000-000000000004",
                "platform_version": "0.4.78",
                "architecture": "linux/arm64",
                "schema_version": "032",
                "site_configuration_version": 4,
                "package": {
                    "id": "org.zizu.pv-storage-charging",
                    "version": "1.0.0",
                    "digest": "d" * 64,
                },
                "image_digests": {
                    "platform": "sha256:" + "a" * 64,
                    "edge_proxy": "sha256:" + "b" * 64,
                },
                "generated_at": "2026-08-14T00:00:00+00:00",
            }
        )

        async def request() -> httpx.Response:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://testserver",
            ) as client:
                login = await client.post(
                    "/api/v1/auth/login",
                    json={"username": "operator", "password": "test-password"},
                )
                return await client.get(
                    "/api/v1/health",
                    headers={"Authorization": f"Bearer {login.json()['access_token']}"},
                )

        try:
            response = asyncio.run(request())
        finally:
            health.set_release_lock_summary_provider(None)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["release_lock"], {
            "status": "locked",
            "id": "00000000-0000-0000-0000-000000000004",
            "platform_version": "0.4.78",
            "architecture": "linux/arm64",
            "schema_version": "032",
            "site_configuration_version": 4,
            "package": {
                "id": "org.zizu.pv-storage-charging",
                "version": "1.0.0",
                "digest": "d" * 64,
            },
            "image_digests": {
                "platform": "sha256:" + "a" * 64,
                "edge_proxy": "sha256:" + "b" * 64,
            },
            "generated_at": "2026-08-14T00:00:00+00:00",
        })


if __name__ == "__main__":
    unittest.main()
