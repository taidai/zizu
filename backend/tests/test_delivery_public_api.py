from __future__ import annotations

import hashlib
import io
import random
import stat
import unittest
import zipfile
from unittest.mock import patch
from typing import Any
from uuid import UUID

import httpx
from fastapi import FastAPI

from app.api.health import router as health_router


TEST_PASSWORD = "test-only-delivery-password"


class AuthenticatedDeliveryClient:
    """Drive the public delivery seam with the role that owns each action."""

    def __init__(self, app: FastAPI) -> None:
        from app.api.auth import router as auth_router
        from app.api.security import get_identity
        from app.services.identity import (
            Identity,
            InMemoryIdentityRepository,
            UserIdentity,
            hash_password,
        )

        password_hash = hash_password(TEST_PASSWORD, salt=b"delivery-fixture")
        users = [
            UserIdentity(
                UUID("00000000-0000-0000-0000-000000000001"),
                "admin",
                password_hash,
                "admin",
                "active",
            ),
            UserIdentity(
                UUID("00000000-0000-0000-0000-000000000002"),
                "engineer",
                password_hash,
                "engineer",
                "active",
            ),
            UserIdentity(
                UUID("00000000-0000-0000-0000-000000000003"),
                "operator",
                password_hash,
                "operator",
                "active",
            ),
        ]
        identity = Identity(InMemoryIdentityRepository(users))
        app.include_router(auth_router, prefix="/api/v1")
        app.dependency_overrides[get_identity] = lambda: identity
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://testserver",
        )
        self._authorization: dict[str, str] = {}

    async def __aenter__(self) -> "AuthenticatedDeliveryClient":
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
            self._authorization[role] = (
                f"Bearer {response.json()['access_token']}"
            )
        return self._authorization[role]

    @staticmethod
    def _role(method: str, path: str) -> str:
        if method == "POST" and path == "/api/v1/solution-packages/import":
            return "admin"
        if method == "GET" and path == "/api/v1/solution-packages":
            return "admin"
        if method == "GET" and path.startswith("/api/v1/delivery-reports/"):
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


class AsgiPublicApiProbe:
    def __init__(self, app: FastAPI) -> None:
        self._app = app

    async def get(self, path: str, timeout_seconds: float) -> tuple[int, dict[str, Any]]:
        transport = httpx.ASGITransport(app=self._app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
            timeout=timeout_seconds,
        ) as client:
            response = await client.get(path)
        return response.status_code, response.json()


class FailingPublicApiProbe:
    def __init__(self, failure: str) -> None:
        self._failure = failure

    async def get(self, path: str, timeout_seconds: float) -> tuple[int, dict[str, Any]]:
        del path, timeout_seconds
        request = httpx.Request("GET", "http://testserver/api/v1/health/live")
        if self._failure == "timeout":
            raise httpx.ReadTimeout("probe timed out", request=request)
        if self._failure == "network":
            raise httpx.ConnectError("probe failed", request=request)
        if self._failure == "http":
            return 503, {"internal": "must not enter delivery evidence"}
        return 200, {"status": "wrong", "private": "must not enter delivery evidence"}


class FakeProbeResponse:
    status_code = 200

    @staticmethod
    def json() -> dict[str, str]:
        return {"status": "alive", "version": "0.4.77"}


def build_minimal_package(
    *,
    package_id: str = "org.zizu.minimal-liveness",
    platform_range: str = ">=0.4.77,<0.5.0",
    package_version: str = "1.0.0",
    acceptance_override: bytes | None = None,
    parameters_yaml: str = "",
) -> bytes:
    acceptance = (
        "schemaVersion: zizu.acceptance/v1alpha1\n"
        "id: acceptance.platform-liveness\n"
        "kind: platform_liveness\n"
        "required: true\n"
        "timeout: 5s\n"
    ).encode() if acceptance_override is None else acceptance_override
    asset_digest = hashlib.sha256(acceptance).hexdigest()
    manifest = (
        "schemaVersion: zizu.solution/v1alpha1\n"
        f"id: {package_id}\n"
        f"version: {package_version}\n"
        "displayName: Minimal liveness\n"
        "platform:\n"
        f"  version: \"{platform_range}\"\n"
        f"{parameters_yaml}"
        "assets:\n"
        "  - id: acceptance.platform-liveness\n"
        "    kind: acceptance\n"
        "    path: acceptance/liveness.yaml\n"
        f"    sha256: \"{asset_digest}\"\n"
        "acceptance:\n"
        "  - acceptance.platform-liveness\n"
    ).encode()
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("solution.yaml", manifest)
        package.writestr("acceptance/liveness.yaml", acceptance)
    return archive.getvalue()


def add_archive_entry(archive_bytes: bytes, path: str, content: bytes) -> bytes:
    source = zipfile.ZipFile(io.BytesIO(archive_bytes))
    target_buffer = io.BytesIO()
    with source, zipfile.ZipFile(target_buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            target.writestr(item, source.read(item))
        target.writestr(path, content)
    return target_buffer.getvalue()


def add_archive_entries(
    archive_bytes: bytes,
    entries: list[tuple[str | zipfile.ZipInfo, bytes, int]],
) -> bytes:
    source = zipfile.ZipFile(io.BytesIO(archive_bytes))
    target_buffer = io.BytesIO()
    with source, zipfile.ZipFile(target_buffer, "w") as target:
        for item in source.infolist():
            target.writestr(item, source.read(item))
        for path, content, compression in entries:
            target.writestr(path, content, compress_type=compression)
    return target_buffer.getvalue()


def mark_zip_encrypted(archive_bytes: bytes) -> bytes:
    archive = bytearray(archive_bytes)
    for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        cursor = 0
        while True:
            cursor = archive.find(signature, cursor)
            if cursor < 0:
                break
            index = cursor + flag_offset
            flags = int.from_bytes(archive[index : index + 2], "little") | 1
            archive[index : index + 2] = flags.to_bytes(2, "little")
            cursor += len(signature)
    return bytes(archive)


def rename_zip_member(archive_bytes: bytes, old: str, new: str) -> bytes:
    old_bytes = old.encode("utf-8")
    new_bytes = new.encode("utf-8")
    if len(old_bytes) != len(new_bytes):
        raise ValueError("ZIP test member names must keep the same byte length")
    archive = bytearray(archive_bytes)
    replacements = 0
    for signature, name_length_offset, name_offset in (
        (b"PK\x03\x04", 26, 30),
        (b"PK\x01\x02", 28, 46),
    ):
        cursor = 0
        while True:
            cursor = archive.find(signature, cursor)
            if cursor < 0:
                break
            name_length = int.from_bytes(
                archive[cursor + name_length_offset : cursor + name_length_offset + 2],
                "little",
            )
            start = cursor + name_offset
            if bytes(archive[start : start + name_length]) == old_bytes:
                archive[start : start + name_length] = new_bytes
                replacements += 1
            cursor = start + name_length
    if replacements != 2:
        raise ValueError("ZIP test member was not present in both headers")
    return bytes(archive)


class DeliveryPublicApiTest(unittest.IsolatedAsyncioTestCase):
    async def test_production_application_registers_delivery_routes(self) -> None:
        from app.main import create_app

        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            liveness = await client.get("/api/v1/health/live")

        self.assertEqual(liveness.status_code, 200)
        # Production does not publish interactive API documentation. Inspect the
        # application's generated schema directly to prove route registration.
        paths = app.openapi()["paths"]
        self.assertIn("/api/v1/solution-packages/import", paths)
        self.assertIn("/api/v1/solution-installations", paths)
        self.assertIn("/api/v1/delivery-reports/{report_id}", paths)
        self.assertIn("/api/v1/alarm-events", paths)
        self.assertIn("/api/v1/alarm-events/{event_id}/acknowledgements", paths)

    async def test_anonymous_liveness_exposes_only_stable_public_contract(self) -> None:
        app = FastAPI()
        app.include_router(health_router, prefix="/api/v1")
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.get("/api/v1/health/live")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"status": "alive", "version": "0.4.77"},
        )

    async def test_production_self_probe_ignores_environment_proxy(self) -> None:
        from app.services.solution_delivery import HttpxPublicApiProbe

        async def fake_get(client: httpx.AsyncClient, path: str) -> FakeProbeResponse:
            del client, path
            return FakeProbeResponse()

        with (
            patch.object(httpx.AsyncClient, "__init__", autospec=True) as init_client,
            patch.object(httpx.AsyncClient, "__aenter__", autospec=True) as enter_client,
            patch.object(httpx.AsyncClient, "__aexit__", autospec=True) as exit_client,
            patch.object(httpx.AsyncClient, "get", fake_get),
        ):
            init_client.return_value = None
            enter_client.return_value = httpx.AsyncClient.__new__(httpx.AsyncClient)
            exit_client.return_value = None
            status_code, body = await HttpxPublicApiProbe(
                "http://127.0.0.1:9000",
            ).get("/api/v1/health/live", 1)

        self.assertFalse(init_client.call_args.kwargs["trust_env"])
        self.assertEqual(status_code, 200)
        self.assertEqual(body, {"status": "alive", "version": "0.4.77"})

    async def test_engineer_imports_a_real_validated_solution_archive(self) -> None:
        from app.api.solution_delivery import (
            get_solution_delivery,
            router as delivery_router,
        )
        from app.services.solution_delivery import (
            InMemoryDeliveryRepository,
            SolutionDelivery,
        )

        app = FastAPI()
        app.include_router(delivery_router, prefix="/api/v1")
        delivery = SolutionDelivery(InMemoryDeliveryRepository(), platform_version="0.4.77")
        app.dependency_overrides[get_solution_delivery] = lambda: delivery
        async with AuthenticatedDeliveryClient(app) as client:
            response = await client.post(
                "/api/v1/solution-packages/import",
                files={
                    "archive": (
                        "minimal-liveness.zizu.zip",
                        build_minimal_package(),
                        "application/zip",
                    )
                },
            )
            repeated = await client.post(
                "/api/v1/solution-packages/import",
                files={
                    "archive": (
                        "minimal-liveness-copy.zizu.zip",
                        build_minimal_package(),
                        "application/zip",
                    )
                },
            )

        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(repeated.status_code, 201, repeated.text)
        self.assertEqual(repeated.json(), response.json())
        body = response.json()
        self.assertEqual(body["package_id"], "org.zizu.minimal-liveness")
        self.assertEqual(body["version"], "1.0.0")
        self.assertEqual(body["display_name"], "Minimal liveness")
        self.assertEqual(body["status"], "validated")
        self.assertEqual(len(body["digest"]), 64)
        self.assertEqual(
            body["acceptance_ids"],
            ["acceptance.platform-liveness"],
        )

    async def test_invalid_archive_is_rejected_without_creating_a_package(self) -> None:
        from app.api.solution_delivery import (
            get_solution_delivery,
            router as delivery_router,
        )
        from app.services.solution_delivery import (
            InMemoryDeliveryRepository,
            SolutionDelivery,
        )

        app = FastAPI()
        app.include_router(delivery_router, prefix="/api/v1")
        delivery = SolutionDelivery(InMemoryDeliveryRepository(), platform_version="0.4.77")
        app.dependency_overrides[get_solution_delivery] = lambda: delivery
        async with AuthenticatedDeliveryClient(app) as client:
            rejected = await client.post(
                "/api/v1/solution-packages/import",
                files={"archive": ("broken.zip", b"not-a-zip", "application/zip")},
            )
            listing = await client.get("/api/v1/solution-packages")

        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(
            rejected.json()["detail"]["code"],
            "PACKAGE_ARCHIVE_UNSAFE",
        )
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(listing.json(), {"items": [], "total": 0})

    async def test_http_upload_limit_rejects_oversized_multipart_without_writes(self) -> None:
        from app.api.solution_delivery import (
            get_solution_delivery,
            router as delivery_router,
        )
        from app.services.solution_delivery import (
            InMemoryDeliveryRepository,
            MAX_PACKAGE_ARCHIVE_BYTES,
            SolutionDelivery,
        )

        app = FastAPI()
        app.include_router(delivery_router, prefix="/api/v1")
        delivery = SolutionDelivery(InMemoryDeliveryRepository(), platform_version="0.4.77")
        app.dependency_overrides[get_solution_delivery] = lambda: delivery
        async with AuthenticatedDeliveryClient(app) as client:
            rejected = await client.post(
                "/api/v1/solution-packages/import",
                files={
                    "archive": (
                        "oversized.zip",
                        b"x" * (MAX_PACKAGE_ARCHIVE_BYTES + 1),
                        "application/zip",
                    )
                },
            )
            listing = await client.get("/api/v1/solution-packages")

        self.assertEqual(rejected.status_code, 422, rejected.text)
        self.assertEqual(rejected.json()["detail"]["code"], "PACKAGE_LIMIT_EXCEEDED")
        self.assertEqual(listing.json(), {"items": [], "total": 0})

    async def test_same_package_identity_cannot_be_replaced_by_other_content(self) -> None:
        from app.api.solution_delivery import (
            get_solution_delivery,
            router as delivery_router,
        )
        from app.services.solution_delivery import (
            InMemoryDeliveryRepository,
            SolutionDelivery,
        )

        app = FastAPI()
        app.include_router(delivery_router, prefix="/api/v1")
        delivery = SolutionDelivery(InMemoryDeliveryRepository(), platform_version="0.4.77")
        app.dependency_overrides[get_solution_delivery] = lambda: delivery
        async with AuthenticatedDeliveryClient(app) as client:
            original = await client.post(
                "/api/v1/solution-packages/import",
                files={"archive": ("original.zip", build_minimal_package(), "application/zip")},
            )
            conflict = await client.post(
                "/api/v1/solution-packages/import",
                files={
                    "archive": (
                        "conflict.zip",
                        build_minimal_package(
                            acceptance_override=(
                                "schemaVersion: zizu.acceptance/v1alpha1\n"
                                "id: acceptance.platform-liveness\n"
                                "kind: platform_liveness\n"
                                "required: true\n"
                                "timeout: 6s\n"
                            ).encode(),
                        ),
                        "application/zip",
                    )
                },
            )
            listing = await client.get("/api/v1/solution-packages")

        self.assertEqual(original.status_code, 201, original.text)
        self.assertEqual(conflict.status_code, 422, conflict.text)
        self.assertEqual(conflict.json()["detail"]["code"], "PACKAGE_DIGEST_CONFLICT")
        self.assertEqual(listing.json(), {"items": [original.json()], "total": 1})

    async def test_archive_path_traversal_is_rejected_without_writes(self) -> None:
        from app.api.solution_delivery import (
            get_solution_delivery,
            router as delivery_router,
        )
        from app.services.solution_delivery import (
            InMemoryDeliveryRepository,
            SolutionDelivery,
        )

        app = FastAPI()
        app.include_router(delivery_router, prefix="/api/v1")
        delivery = SolutionDelivery(InMemoryDeliveryRepository(), platform_version="0.4.77")
        app.dependency_overrides[get_solution_delivery] = lambda: delivery
        unsafe = add_archive_entry(build_minimal_package(), "../escape.yaml", b"x")

        async with AuthenticatedDeliveryClient(app) as client:
            rejected = await client.post(
                "/api/v1/solution-packages/import",
                files={"archive": ("unsafe.zip", unsafe, "application/zip")},
            )
            listing = await client.get("/api/v1/solution-packages")

        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(
            rejected.json()["detail"]["code"],
            "PACKAGE_ARCHIVE_UNSAFE",
        )
        self.assertEqual(listing.json(), {"items": [], "total": 0})

    async def test_archive_safety_limits_reject_unsafe_packages_without_writes(self) -> None:
        from app.api.solution_delivery import (
            get_solution_delivery,
            router as delivery_router,
        )
        from app.services.solution_delivery import (
            InMemoryDeliveryRepository,
            SolutionDelivery,
        )

        base = build_minimal_package()
        symlink = zipfile.ZipInfo("acceptance/link.yaml")
        symlink.create_system = 3
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        device = zipfile.ZipInfo("acceptance/device.yaml")
        device.create_system = 3
        device.external_attr = (stat.S_IFCHR | 0o600) << 16
        random_source = random.Random(0)
        total_limit_entries = [
            (
                f"notes/chunk-{index}.txt",
                random_source.randbytes(512 * 1024) * 4,
                zipfile.ZIP_DEFLATED,
            )
            for index in range(11)
        ]
        cases = {
            "raw-size": b"x" * (10 * 1024 * 1024 + 1),
            "file-count": add_archive_entries(
                base,
                [
                    (f"notes/{index}.txt", b"", zipfile.ZIP_STORED)
                    for index in range(255)
                ],
            ),
            "single-file-size": add_archive_entries(
                base,
                [("notes/large.txt", b"x" * (2 * 1024 * 1024 + 1), zipfile.ZIP_STORED)],
            ),
            "total-size": add_archive_entries(base, total_limit_entries),
            "compression-ratio": add_archive_entries(
                base,
                [("notes/bomb.txt", b"0" * (1024 * 1024), zipfile.ZIP_DEFLATED)],
            ),
            "absolute-path": add_archive_entry(base, "/escape.yaml", b"x"),
            "windows-drive": add_archive_entry(base, "C:/escape.yaml", b"x"),
            "backslash": rename_zip_member(
                base,
                "acceptance/liveness.yaml",
                "acceptance\\liveness.yaml",
            ),
            "duplicate": add_archive_entry(base, "solution.yaml", b"duplicate"),
            "casefold-collision": add_archive_entry(base, "SOLUTION.YAML", b"duplicate"),
            "symlink": add_archive_entries(base, [(symlink, b"solution.yaml", zipfile.ZIP_STORED)]),
            "device": add_archive_entries(base, [(device, b"x", zipfile.ZIP_STORED)]),
            "encrypted": mark_zip_encrypted(base),
            "unreferenced-executable": add_archive_entry(base, "scripts/install.sh", b"exit 0"),
        }

        for name, archive in cases.items():
            with self.subTest(case=name):
                app = FastAPI()
                app.include_router(delivery_router, prefix="/api/v1")
                delivery = SolutionDelivery(
                    InMemoryDeliveryRepository(),
                    platform_version="0.4.77",
                )
                app.dependency_overrides[get_solution_delivery] = lambda: delivery
                async with AuthenticatedDeliveryClient(app) as client:
                    rejected = await client.post(
                        "/api/v1/solution-packages/import",
                        files={"archive": ("unsafe.zip", archive, "application/zip")},
                    )
                    listing = await client.get("/api/v1/solution-packages")

                self.assertEqual(rejected.status_code, 422, rejected.text)
                self.assertIn(
                    rejected.json()["detail"]["code"],
                    {"PACKAGE_ARCHIVE_UNSAFE", "PACKAGE_LIMIT_EXCEEDED"},
                )
                self.assertEqual(listing.json(), {"items": [], "total": 0})

    async def test_manifest_and_acceptance_are_fully_validated_before_import(self) -> None:
        from app.api.solution_delivery import (
            get_solution_delivery,
            router as delivery_router,
        )
        from app.services.solution_delivery import (
            InMemoryDeliveryRepository,
            SolutionDelivery,
        )

        invalid_acceptance = {
            "schema": (
                "schemaVersion: zizu.acceptance/v9\n"
                "id: acceptance.platform-liveness\n"
                "kind: platform_liveness\n"
                "required: true\n"
                "timeout: 5s\n"
            ).encode(),
            "kind": (
                "schemaVersion: zizu.acceptance/v1alpha1\n"
                "id: acceptance.platform-liveness\n"
                "kind: arbitrary_http\n"
                "required: true\n"
                "timeout: 5s\n"
            ).encode(),
            "timeout": (
                "schemaVersion: zizu.acceptance/v1alpha1\n"
                "id: acceptance.platform-liveness\n"
                "kind: platform_liveness\n"
                "required: true\n"
                "timeout: forever\n"
            ).encode(),
            "extra-field": (
                "schemaVersion: zizu.acceptance/v1alpha1\n"
                "id: acceptance.platform-liveness\n"
                "kind: platform_liveness\n"
                "required: true\n"
                "timeout: 5s\n"
                "url: https://example.invalid/private\n"
            ).encode(),
        }
        cases = {
            "invalid-semver": build_minimal_package(package_version="not-semver"),
            "incompatible-platform": build_minimal_package(
                platform_range=">=0.5.0,<0.6.0",
            ),
            "invalid-parameter-type-shape": build_minimal_package(
                parameters_yaml=(
                    "parameters:\n"
                    "  - id: site.code\n"
                    "    type: [string]\n"
                    "    required: true\n"
                    "    description: Stable site code\n"
                ),
            ),
            "unsafe-parameter-pattern": build_minimal_package(
                parameters_yaml=(
                    "parameters:\n"
                    "  - id: site.code\n"
                    "    type: string\n"
                    "    required: true\n"
                    "    pattern: '^(a+)+$'\n"
                    "    description: Stable site code\n"
                ),
            ),
            **{
                f"acceptance-{name}": build_minimal_package(acceptance_override=content)
                for name, content in invalid_acceptance.items()
            },
        }

        for name, archive in cases.items():
            with self.subTest(case=name):
                app = FastAPI()
                app.include_router(delivery_router, prefix="/api/v1")
                delivery = SolutionDelivery(
                    InMemoryDeliveryRepository(),
                    platform_version="0.4.77",
                )
                app.dependency_overrides[get_solution_delivery] = lambda: delivery
                async with AuthenticatedDeliveryClient(app) as client:
                    rejected = await client.post(
                        "/api/v1/solution-packages/import",
                        files={"archive": ("invalid.zip", archive, "application/zip")},
                    )
                    listing = await client.get("/api/v1/solution-packages")

                self.assertEqual(rejected.status_code, 422, rejected.text)
                self.assertIn(
                    rejected.json()["detail"]["code"],
                    {
                        "MANIFEST_INVALID",
                        "ASSET_REFERENCE_INVALID",
                        "PLATFORM_INCOMPATIBLE",
                    },
                )
                self.assertEqual(listing.json(), {"items": [], "total": 0})

    async def test_engineer_creates_a_reviewable_installation_plan(self) -> None:
        from app.api.solution_delivery import (
            get_solution_delivery,
            router as delivery_router,
        )
        from app.services.solution_delivery import (
            InMemoryDeliveryRepository,
            SolutionDelivery,
        )

        app = FastAPI()
        app.include_router(delivery_router, prefix="/api/v1")
        delivery = SolutionDelivery(InMemoryDeliveryRepository(), platform_version="0.4.77")
        app.dependency_overrides[get_solution_delivery] = lambda: delivery
        async with AuthenticatedDeliveryClient(app) as client:
            imported = await client.post(
                "/api/v1/solution-packages/import",
                files={
                    "archive": (
                        "minimal-liveness.zizu.zip",
                        build_minimal_package(),
                        "application/zip",
                    )
                },
            )
            package = imported.json()
            planned = await client.post(
                f"/api/v1/solution-packages/{package['id']}/install-plans",
                json={},
            )
            repeated_plan = await client.post(
                f"/api/v1/solution-packages/{package['id']}/install-plans",
                json={},
            )
            queried = await client.get(f"/api/v1/install-plans/{planned.json()['id']}")

        self.assertEqual(planned.status_code, 201, planned.text)
        self.assertEqual(repeated_plan.status_code, 201, repeated_plan.text)
        self.assertEqual(repeated_plan.json(), planned.json())
        self.assertEqual(queried.status_code, 200, queried.text)
        self.assertEqual(queried.json(), planned.json())
        plan = planned.json()
        self.assertEqual(plan["configuration_digest"], package["digest"])
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["package_record_id"], package["id"])
        self.assertEqual(plan["package_digest"], package["digest"])
        self.assertEqual(plan["base_site_configuration_version"], 0)
        self.assertEqual(plan["blockers"], [])
        self.assertEqual(
            plan["items"],
            [
                {
                    "asset_id": "org.zizu.minimal-liveness",
                    "kind": "solution_package",
                    "action": "add",
                },
                {
                    "asset_id": "acceptance.platform-liveness",
                    "kind": "acceptance",
                    "action": "add",
                },
            ],
        )
        self.assertEqual(len(plan["digest"]), 64)

    async def test_missing_required_site_parameter_blocks_install_without_writes(
        self,
    ) -> None:
        from app.api.solution_delivery import (
            get_solution_delivery,
            router as delivery_router,
        )
        from app.services.solution_delivery import (
            InMemoryDeliveryRepository,
            SolutionDelivery,
        )

        app = FastAPI()
        app.include_router(delivery_router, prefix="/api/v1")
        delivery = SolutionDelivery(
            InMemoryDeliveryRepository(),
            platform_version="0.4.77",
        )
        app.dependency_overrides[get_solution_delivery] = lambda: delivery
        package_archive = build_minimal_package(
            package_id="org.zizu.ems-parameter-tracer",
            parameters_yaml=(
                "parameters:\n"
                "  - id: site.code\n"
                "    type: string\n"
                "    required: true\n"
                "    description: Stable site code\n"
            ),
        )

        async with AuthenticatedDeliveryClient(app) as client:
            imported = await client.post(
                "/api/v1/solution-packages/import",
                files={
                    "archive": (
                        "ems-parameter-tracer.zizu.zip",
                        package_archive,
                        "application/zip",
                    )
                },
            )
            self.assertEqual(imported.status_code, 201, imported.text)
            planned = await client.post(
                f"/api/v1/solution-packages/{imported.json()['id']}/install-plans",
                json={"parameters": {}, "secret_references": {}},
            )

            self.assertEqual(planned.status_code, 201, planned.text)
            plan = planned.json()
            self.assertEqual(plan["status"], "blocked")
            self.assertEqual(
                plan["blockers"],
                [
                    {
                        "code": "PARAMETER_REQUIRED",
                        "parameter_id": "site.code",
                        "message": "Required site parameter is missing",
                    }
                ],
            )
            rejected = await client.post(
                f"/api/v1/install-plans/{plan['id']}/apply",
                json={"plan_digest": plan["digest"]},
                headers={"Idempotency-Key": "blocked-parameter-plan"},
            )
            installations = await client.get("/api/v1/solution-installations")

        self.assertEqual(rejected.status_code, 409, rejected.text)
        self.assertEqual(
            rejected.json()["detail"]["code"],
            "INSTALL_PLAN_BLOCKED",
        )
        self.assertEqual(installations.json(), {"items": [], "total": 0})

    async def test_typed_site_parameters_are_normalized_in_the_reviewable_plan(
        self,
    ) -> None:
        from app.api.solution_delivery import (
            get_solution_delivery,
            router as delivery_router,
        )
        from app.services.solution_delivery import (
            InMemoryDeliveryRepository,
            SolutionDelivery,
        )

        app = FastAPI()
        app.include_router(delivery_router, prefix="/api/v1")
        delivery = SolutionDelivery(
            InMemoryDeliveryRepository(),
            platform_version="0.4.77",
        )
        app.dependency_overrides[get_solution_delivery] = lambda: delivery
        package_archive = build_minimal_package(
            package_id="org.zizu.ems-typed-parameters",
            parameters_yaml=(
                "parameters:\n"
                "  - id: site.code\n"
                "    type: string\n"
                "    required: true\n"
                "    pattern: '^[A-Z0-9-]{2,16}$'\n"
                "    description: Stable site code\n"
                "  - id: pcs.count\n"
                "    type: integer\n"
                "    required: true\n"
                "    minimum: 1\n"
                "    maximum: 8\n"
                "    description: PCS device count\n"
                "  - id: nominal.power\n"
                "    type: number\n"
                "    unit: kW\n"
                "    required: true\n"
                "    minimum: 0\n"
                "    maximum: 10000\n"
                "    description: Site nominal power\n"
                "  - id: dispatch.enabled\n"
                "    type: boolean\n"
                "    required: true\n"
                "    description: Enable dispatch\n"
                "  - id: dispatch.mode\n"
                "    type: enum\n"
                "    required: true\n"
                "    values: [self_consumption, peak_shaving]\n"
                "    description: Dispatch strategy\n"
                "  - id: controller.address\n"
                "    type: address\n"
                "    required: true\n"
                "    description: Controller address\n"
                "  - id: controller.port\n"
                "    type: port\n"
                "    required: true\n"
                "    description: Modbus TCP port\n"
                "  - id: poll.interval\n"
                "    type: duration\n"
                "    required: false\n"
                "    default: 5s\n"
                "    description: Poll interval\n"
                "  - id: neuron.credentials\n"
                "    type: secret\n"
                "    required: true\n"
                "    description: Neuron credential reference\n"
            ),
        )
        submitted_parameters = {
            "site.code": "EMS-01",
            "pcs.count": 2,
            "nominal.power": 250.5,
            "dispatch.enabled": True,
            "dispatch.mode": "self_consumption",
            "controller.address": "192.0.2.10",
            "controller.port": 502,
        }

        async with AuthenticatedDeliveryClient(app) as client:
            imported = await client.post(
                "/api/v1/solution-packages/import",
                files={
                    "archive": (
                        "ems-typed-parameters.zizu.zip",
                        package_archive,
                        "application/zip",
                    )
                },
            )
            self.assertEqual(imported.status_code, 201, imported.text)
            self.assertEqual(
                [
                    contract["id"]
                    for contract in imported.json()["parameter_contracts"]
                ],
                [
                    "site.code",
                    "pcs.count",
                    "nominal.power",
                    "dispatch.enabled",
                    "dispatch.mode",
                    "controller.address",
                    "controller.port",
                    "poll.interval",
                    "neuron.credentials",
                ],
            )
            planned = await client.post(
                f"/api/v1/solution-packages/{imported.json()['id']}/install-plans",
                json={
                    "parameters": submitted_parameters,
                    "secret_references": {
                        "neuron.credentials": "secret://site/neuron/credentials"
                    },
                },
            )

        self.assertEqual(planned.status_code, 201, planned.text)
        plan = planned.json()
        self.assertEqual(plan["status"], "ready")
        self.assertEqual(plan["blockers"], [])
        self.assertEqual(
            [contract["id"] for contract in plan["parameter_contracts"]],
            [
                "site.code",
                "pcs.count",
                "nominal.power",
                "dispatch.enabled",
                "dispatch.mode",
                "controller.address",
                "controller.port",
                "poll.interval",
                "neuron.credentials",
            ],
        )
        self.assertEqual(
            next(
                contract
                for contract in plan["parameter_contracts"]
                if contract["id"] == "nominal.power"
            ),
            {
                "id": "nominal.power",
                "type": "number",
                "unit": "kW",
                "required": True,
                "minimum": 0,
                "maximum": 10000,
                "description": "Site nominal power",
            },
        )
        self.assertEqual(
            plan["parameters"],
            {**submitted_parameters, "poll.interval": "5s"},
        )
        self.assertEqual(
            plan["secret_references"],
            {"neuron.credentials": "secret://site/neuron/credentials"},
        )
        self.assertNotIn("credentials", str(plan["parameters"]))

    async def test_install_persists_an_immutable_site_configuration_version(
        self,
    ) -> None:
        from app.api.solution_delivery import (
            get_solution_delivery,
            router as delivery_router,
        )
        from app.services.solution_delivery import (
            InMemoryDeliveryRepository,
            SolutionDelivery,
        )

        app = FastAPI()
        app.include_router(delivery_router, prefix="/api/v1")
        delivery = SolutionDelivery(
            InMemoryDeliveryRepository(),
            platform_version="0.4.77",
        )
        app.dependency_overrides[get_solution_delivery] = lambda: delivery
        package_archive = build_minimal_package(
            package_id="org.zizu.ems-site-configuration",
            parameters_yaml=(
                "parameters:\n"
                "  - id: site.code\n"
                "    type: string\n"
                "    required: true\n"
                "    description: Stable site code\n"
                "  - id: neuron.credentials\n"
                "    type: secret\n"
                "    required: true\n"
                "    description: Neuron credential reference\n"
            ),
        )

        async with AuthenticatedDeliveryClient(app) as client:
            imported = await client.post(
                "/api/v1/solution-packages/import",
                files={
                    "archive": (
                        "ems-site-configuration.zizu.zip",
                        package_archive,
                        "application/zip",
                    )
                },
            )
            planned = await client.post(
                f"/api/v1/solution-packages/{imported.json()['id']}/install-plans",
                json={
                    "parameters": {"site.code": "EMS-01"},
                    "secret_references": {
                        "neuron.credentials": "secret://site/neuron/credentials"
                    },
                },
            )
            plan = planned.json()
            installed = await client.post(
                f"/api/v1/install-plans/{plan['id']}/apply",
                json={"plan_digest": plan["digest"]},
                headers={"Idempotency-Key": "install-site-configuration"},
            )
            configuration = await client.get(
                f"/api/v1/site-configuration-versions/"
                f"{installed.json()['site_configuration_version']}"
            )

        self.assertEqual(installed.status_code, 201, installed.text)
        self.assertEqual(configuration.status_code, 200, configuration.text)
        snapshot = configuration.json()
        self.assertEqual(snapshot["version"], 1)
        self.assertEqual(snapshot["previous_version"], 0)
        self.assertEqual(snapshot["parameters"], {"site.code": "EMS-01"})
        self.assertEqual(
            snapshot["secret_references"],
            {"neuron.credentials": "secret://site/neuron/credentials"},
        )
        self.assertEqual(snapshot["digest"], plan["configuration_digest"])
        self.assertNotIn("password", str(snapshot).lower())

    async def test_changed_site_parameters_create_an_update_plan_and_new_version(
        self,
    ) -> None:
        from app.api.solution_delivery import (
            get_solution_delivery,
            router as delivery_router,
        )
        from app.services.solution_delivery import (
            InMemoryDeliveryRepository,
            SolutionDelivery,
        )

        app = FastAPI()
        app.include_router(delivery_router, prefix="/api/v1")
        delivery = SolutionDelivery(
            InMemoryDeliveryRepository(),
            platform_version="0.4.77",
        )
        app.dependency_overrides[get_solution_delivery] = lambda: delivery
        package_archive = build_minimal_package(
            package_id="org.zizu.ems-parameter-update",
            parameters_yaml=(
                "parameters:\n"
                "  - id: pcs.count\n"
                "    type: integer\n"
                "    required: true\n"
                "    minimum: 1\n"
                "    maximum: 8\n"
                "    description: PCS count\n"
                "  - id: site.name\n"
                "    type: string\n"
                "    required: false\n"
                "    default: package-default\n"
                "    description: Site display name\n"
            ),
        )

        async with AuthenticatedDeliveryClient(app) as client:
            imported = await client.post(
                "/api/v1/solution-packages/import",
                files={
                    "archive": (
                        "ems-parameter-update.zizu.zip",
                        package_archive,
                        "application/zip",
                    )
                },
            )

            async def plan_and_apply(
                count: int,
                key: str,
                *,
                site_name: str | None = None,
            ) -> tuple[dict, dict]:
                values: dict[str, object] = {"pcs.count": count}
                if site_name is not None:
                    values["site.name"] = site_name
                planned = await client.post(
                    f"/api/v1/solution-packages/{imported.json()['id']}/install-plans",
                    json={"parameters": values},
                )
                plan = planned.json()
                installed = await client.post(
                    f"/api/v1/install-plans/{plan['id']}/apply",
                    json={"plan_digest": plan["digest"]},
                    headers={"Idempotency-Key": key},
                )
                self.assertEqual(installed.status_code, 201, installed.text)
                return plan, installed.json()

            first_plan, first = await plan_and_apply(
                1,
                "install-one-pcs",
                site_name="customer-override",
            )
            update_plan, updated = await plan_and_apply(2, "install-two-pcs")
            second_configuration = await client.get(
                f"/api/v1/site-configuration-versions/"
                f"{updated['site_configuration_version']}"
            )
            rollback_plan, rolled_back = await plan_and_apply(
                1,
                "install-one-pcs-as-new-version",
            )
            latest = await client.get(
                f"/api/v1/site-configuration-versions/"
                f"{rolled_back['site_configuration_version']}"
            )

        self.assertEqual(
            {
                item["action"]
                for item in first_plan["items"]
                if item["kind"] == "site_parameter"
            },
            {"add"},
        )
        self.assertEqual(
            {
                item["action"]
                for item in update_plan["items"]
                if item["asset_id"] == "pcs.count"
            },
            {"update"},
        )
        self.assertEqual(
            {
                item["action"]
                for item in rollback_plan["items"]
                if item["asset_id"] == "pcs.count"
            },
            {"update"},
        )
        parameter_change = next(
            item
            for item in update_plan["items"]
            if item["kind"] == "site_parameter"
        )
        self.assertEqual(
            parameter_change,
            {
                "asset_id": "pcs.count",
                "kind": "site_parameter",
                "action": "update",
                "before": 1,
                "after": 2,
                "source": "engineer_input",
                "unit": None,
            },
        )
        site_name_change = next(
            item
            for item in update_plan["items"]
            if item["asset_id"] == "site.name"
        )
        self.assertEqual(site_name_change["action"], "preserve")
        self.assertEqual(site_name_change["before"], "customer-override")
        self.assertEqual(site_name_change["after"], "customer-override")
        self.assertEqual(site_name_change["source"], "engineer_input")
        self.assertEqual(first["site_configuration_version"], 1)
        self.assertEqual(updated["site_configuration_version"], 2)
        self.assertEqual(rolled_back["site_configuration_version"], 3)
        self.assertEqual(latest.json()["previous_version"], 2)
        self.assertEqual(
            latest.json()["parameters"],
            {"pcs.count": 1, "site.name": "customer-override"},
        )
        self.assertEqual(
            latest.json()["parameter_metadata"]["pcs.count"]["source"],
            "engineer_input",
        )
        self.assertTrue(
            latest.json()["parameter_metadata"]["pcs.count"]["modified_at"].endswith(
                "+00:00"
            )
        )
        self.assertEqual(
            second_configuration.json()["parameter_metadata"]["site.name"],
            latest.json()["parameter_metadata"]["site.name"],
        )

    async def test_package_and_site_parameter_changes_create_a_blocked_upgrade_plan(
        self,
    ) -> None:
        from app.api.solution_delivery import (
            get_solution_delivery,
            router as delivery_router,
        )
        from app.services.solution_delivery import (
            InMemoryDeliveryRepository,
            SolutionDelivery,
        )

        app = FastAPI()
        app.include_router(delivery_router, prefix="/api/v1")
        delivery = SolutionDelivery(
            InMemoryDeliveryRepository(),
            platform_version="0.4.77",
        )
        app.dependency_overrides[get_solution_delivery] = lambda: delivery
        v1_archive = build_minimal_package(
            package_id="org.zizu.ems-three-way-upgrade",
            package_version="1.0.0",
            parameters_yaml=(
                "parameters:\n"
                "  - id: site.name\n"
                "    type: string\n"
                "    required: false\n"
                "    default: package-name-v1\n"
                "    description: Site display name\n"
                "  - id: site.mode\n"
                "    type: string\n"
                "    required: false\n"
                "    default: package-mode-v1\n"
                "    description: Package-owned mode\n"
            ),
        )
        v2_archive = build_minimal_package(
            package_id="org.zizu.ems-three-way-upgrade",
            package_version="1.1.0",
            parameters_yaml=(
                "parameters:\n"
                "  - id: site.name\n"
                "    type: string\n"
                "    required: false\n"
                "    default: package-name-v2\n"
                "    description: Site display name\n"
                "  - id: site.mode\n"
                "    type: string\n"
                "    required: false\n"
                "    default: package-mode-v2\n"
                "    description: Package-owned mode\n"
            ),
        )

        async with AuthenticatedDeliveryClient(app) as client:
            imported_v1 = await client.post(
                "/api/v1/solution-packages/import",
                files={
                    "archive": (
                        "ems-three-way-v1.zizu.zip",
                        v1_archive,
                        "application/zip",
                    )
                },
            )
            initial_plan = await client.post(
                f"/api/v1/solution-packages/{imported_v1.json()['id']}/install-plans",
                json={"parameters": {"site.name": "customer-override"}},
            )
            initial_installation = await client.post(
                f"/api/v1/install-plans/{initial_plan.json()['id']}/apply",
                json={"plan_digest": initial_plan.json()["digest"]},
                headers={"Idempotency-Key": "install-three-way-v1"},
            )
            imported_v2 = await client.post(
                "/api/v1/solution-packages/import",
                files={
                    "archive": (
                        "ems-three-way-v2.zizu.zip",
                        v2_archive,
                        "application/zip",
                    )
                },
            )
            upgrade_plan = await client.post(
                f"/api/v1/solution-packages/{imported_v2.json()['id']}/install-plans",
                json={},
            )
            blocked_apply = await client.post(
                f"/api/v1/install-plans/{upgrade_plan.json()['id']}/apply",
                json={"plan_digest": upgrade_plan.json()["digest"]},
                headers={"Idempotency-Key": "install-three-way-v2"},
            )
            invalid_conflict_review = await client.post(
                f"/api/v1/solution-packages/{imported_v2.json()['id']}/install-plans",
                json={
                    "upgrade_risk_resolutions": {
                        "UPGRADE_PARAMETER_CONFLICT:site.name": "not a resolution"
                    }
                },
            )
            original_configuration = await client.get(
                "/api/v1/site-configuration-versions/"
                f"{initial_installation.json()['site_configuration_version']}"
            )
            resolved_plan = await client.post(
                f"/api/v1/solution-packages/{imported_v2.json()['id']}/install-plans",
                json={"parameters": {"site.name": "engineer-resolved-v2"}},
            )
            resolved_installation = await client.post(
                f"/api/v1/install-plans/{resolved_plan.json()['id']}/apply",
                json={"plan_digest": resolved_plan.json()["digest"]},
                headers={"Idempotency-Key": "install-three-way-v2-resolved"},
            )
            resolved_configuration = await client.get(
                "/api/v1/site-configuration-versions/"
                f"{resolved_installation.json()['site_configuration_version']}"
            )

        self.assertEqual(imported_v1.status_code, 201, imported_v1.text)
        self.assertEqual(initial_plan.status_code, 201, initial_plan.text)
        self.assertEqual(initial_installation.status_code, 201, initial_installation.text)
        self.assertEqual(imported_v2.status_code, 201, imported_v2.text)
        self.assertEqual(upgrade_plan.status_code, 201, upgrade_plan.text)
        self.assertEqual(upgrade_plan.json()["status"], "blocked")
        self.assertIn(
            {
                "code": "UPGRADE_PARAMETER_CONFLICT",
                "parameter_id": "site.name",
                "message": "Package and site override both changed this parameter",
            },
            upgrade_plan.json()["blockers"],
        )
        conflict_item = next(
            item
            for item in upgrade_plan.json()["items"]
            if item["kind"] == "site_parameter" and item["asset_id"] == "site.name"
        )
        self.assertEqual(conflict_item["action"], "conflict")
        self.assertEqual(conflict_item["before"], "customer-override")
        self.assertEqual(conflict_item["after"], "customer-override")
        package_change = next(
            item
            for item in upgrade_plan.json()["items"]
            if item["kind"] == "site_parameter" and item["asset_id"] == "site.mode"
        )
        self.assertEqual(package_change["action"], "update")
        self.assertEqual(package_change["before"], "package-mode-v1")
        self.assertEqual(package_change["after"], "package-mode-v2")
        self.assertEqual(package_change["source"], "package_default")
        self.assertEqual(blocked_apply.status_code, 409, blocked_apply.text)
        self.assertEqual(
            blocked_apply.json()["detail"]["code"], "INSTALL_PLAN_BLOCKED"
        )
        self.assertEqual(422, invalid_conflict_review.status_code)
        self.assertEqual(
            "UPGRADE_RISK_RESOLUTION_INVALID",
            invalid_conflict_review.json()["detail"]["code"],
        )
        self.assertEqual(original_configuration.status_code, 200, original_configuration.text)
        self.assertEqual(
            original_configuration.json()["parameters"],
            {"site.name": "customer-override", "site.mode": "package-mode-v1"},
        )
        self.assertEqual(resolved_plan.status_code, 201, resolved_plan.text)
        self.assertEqual(resolved_plan.json()["status"], "ready")
        self.assertEqual(resolved_installation.status_code, 201, resolved_installation.text)
        self.assertEqual(resolved_configuration.status_code, 200, resolved_configuration.text)
        self.assertEqual(resolved_configuration.json()["version"], 2)
        self.assertEqual(
            resolved_configuration.json()["parameters"],
            {
                "site.name": "engineer-resolved-v2",
                "site.mode": "package-mode-v2",
            },
        )

    async def test_removing_an_in_use_secret_reference_blocks_an_upgrade(self) -> None:
        from app.api.solution_delivery import (
            get_solution_delivery,
            router as delivery_router,
        )
        from app.services.solution_delivery import (
            InMemoryDeliveryRepository,
            SolutionDelivery,
        )

        app = FastAPI()
        app.include_router(delivery_router, prefix="/api/v1")
        delivery = SolutionDelivery(
            InMemoryDeliveryRepository(),
            platform_version="0.4.77",
        )
        app.dependency_overrides[get_solution_delivery] = lambda: delivery
        v1_archive = build_minimal_package(
            package_id="org.zizu.secret-removal-upgrade",
            package_version="1.0.0",
            parameters_yaml=(
                "parameters:\n"
                "  - id: neuron.credentials\n"
                "    type: secret\n"
                "    required: true\n"
                "    description: Runtime Neuron credentials\n"
            ),
        )
        v2_archive = build_minimal_package(
            package_id="org.zizu.secret-removal-upgrade",
            package_version="1.1.0",
        )

        async with AuthenticatedDeliveryClient(app) as client:
            imported_v1 = await client.post(
                "/api/v1/solution-packages/import",
                files={
                    "archive": (
                        "secret-removal-v1.zizu.zip",
                        v1_archive,
                        "application/zip",
                    )
                },
            )
            plan_v1 = await client.post(
                f"/api/v1/solution-packages/{imported_v1.json()['id']}/install-plans",
                json={
                    "secret_references": {
                        "neuron.credentials": "secret://site/neuron/credentials"
                    }
                },
            )
            installed_v1 = await client.post(
                f"/api/v1/install-plans/{plan_v1.json()['id']}/apply",
                json={"plan_digest": plan_v1.json()["digest"]},
                headers={"Idempotency-Key": "install-secret-v1"},
            )
            imported_v2 = await client.post(
                "/api/v1/solution-packages/import",
                files={
                    "archive": (
                        "secret-removal-v2.zizu.zip",
                        v2_archive,
                        "application/zip",
                    )
                },
            )
            plan_v2 = await client.post(
                f"/api/v1/solution-packages/{imported_v2.json()['id']}/install-plans",
                json={},
            )
            blocked_apply = await client.post(
                f"/api/v1/install-plans/{plan_v2.json()['id']}/apply",
                json={"plan_digest": plan_v2.json()["digest"]},
                headers={"Idempotency-Key": "install-secret-v2"},
            )

        self.assertEqual(201, installed_v1.status_code, installed_v1.text)
        self.assertEqual(201, imported_v2.status_code, imported_v2.text)
        self.assertEqual(201, plan_v2.status_code, plan_v2.text)
        self.assertEqual("blocked", plan_v2.json()["status"])
        self.assertIn(
            {
                "code": "UPGRADE_SECRET_REFERENCE_REMOVAL",
                "asset_id": "neuron.credentials",
                "message": "Upgrade removes an in-use Secret reference",
            },
            plan_v2.json()["blockers"],
        )
        secret_block = next(
            item
            for item in plan_v2.json()["items"]
            if item.get("kind") == "upgrade_safety"
        )
        self.assertEqual("block", secret_block["action"])
        self.assertEqual(
            "UPGRADE_SECRET_REFERENCE_REMOVAL:neuron.credentials",
            secret_block["risk_key"],
        )
        self.assertEqual(409, blocked_apply.status_code, blocked_apply.text)
        self.assertEqual(
            "INSTALL_PLAN_BLOCKED",
            blocked_apply.json()["detail"]["code"],
        )

    async def test_invalid_values_and_raw_secrets_only_create_redacted_blockers(
        self,
    ) -> None:
        from app.api.solution_delivery import (
            get_solution_delivery,
            router as delivery_router,
        )
        from app.services.solution_delivery import (
            InMemoryDeliveryRepository,
            SolutionDelivery,
        )

        app = FastAPI()
        app.include_router(delivery_router, prefix="/api/v1")
        delivery = SolutionDelivery(
            InMemoryDeliveryRepository(),
            platform_version="0.4.77",
        )
        app.dependency_overrides[get_solution_delivery] = lambda: delivery
        package_archive = build_minimal_package(
            package_id="org.zizu.ems-invalid-parameters",
            parameters_yaml=(
                "parameters:\n"
                "  - id: pcs.count\n"
                "    type: integer\n"
                "    required: true\n"
                "    minimum: 1\n"
                "    maximum: 8\n"
                "    description: PCS count\n"
                "  - id: neuron.credentials\n"
                "    type: secret\n"
                "    required: true\n"
                "    description: Neuron credentials\n"
            ),
        )
        raw_secret = "raw-secret-must-never-be-persisted-or-reflected"

        async with AuthenticatedDeliveryClient(app) as client:
            imported = await client.post(
                "/api/v1/solution-packages/import",
                files={
                    "archive": (
                        "ems-invalid-parameters.zizu.zip",
                        package_archive,
                        "application/zip",
                    )
                },
            )
            planned = await client.post(
                f"/api/v1/solution-packages/{imported.json()['id']}/install-plans",
                json={
                    "parameters": {
                        "pcs.count": 99,
                        "neuron.credentials": raw_secret,
                    },
                    "secret_references": {},
                },
            )
            malformed = await client.post(
                f"/api/v1/solution-packages/{imported.json()['id']}/install-plans",
                json={
                    "parameters": [raw_secret],
                    "secret_references": {},
                },
            )
            installations = await client.get("/api/v1/solution-installations")

        self.assertEqual(planned.status_code, 201, planned.text)
        response_text = planned.text
        self.assertNotIn(raw_secret, response_text)
        self.assertEqual(planned.json()["parameters"], {})
        self.assertEqual(planned.json()["secret_references"], {})
        self.assertEqual(malformed.status_code, 422, malformed.text)
        self.assertEqual(
            malformed.json()["detail"]["code"],
            "INSTALL_PLAN_REQUEST_INVALID",
        )
        self.assertNotIn(raw_secret, malformed.text)
        self.assertEqual(
            {blocker["code"] for blocker in planned.json()["blockers"]},
            {
                "PARAMETER_RANGE_INVALID",
                "SECRET_REFERENCE_REQUIRED",
                "SECRET_VALUE_FORBIDDEN",
            },
        )
        self.assertEqual(installations.json(), {"items": [], "total": 0})

    async def test_repeating_the_same_install_command_returns_the_same_installation(self) -> None:
        from app.api.solution_delivery import (
            get_solution_delivery,
            router as delivery_router,
        )
        from app.services.solution_delivery import (
            InMemoryDeliveryRepository,
            SolutionDelivery,
        )

        app = FastAPI()
        app.include_router(delivery_router, prefix="/api/v1")
        delivery = SolutionDelivery(InMemoryDeliveryRepository(), platform_version="0.4.77")
        app.dependency_overrides[get_solution_delivery] = lambda: delivery
        async with AuthenticatedDeliveryClient(app) as client:
            imported = await client.post(
                "/api/v1/solution-packages/import",
                files={
                    "archive": (
                        "minimal-liveness.zizu.zip",
                        build_minimal_package(),
                        "application/zip",
                    )
                },
            )
            planned = await client.post(
                f"/api/v1/solution-packages/{imported.json()['id']}/install-plans",
                json={},
            )
            plan = planned.json()
            request = {"plan_digest": plan["digest"]}
            headers = {"Idempotency-Key": "install-minimal-liveness-once"}
            missing_key = await client.post(
                f"/api/v1/install-plans/{plan['id']}/apply",
                json=request,
            )
            first = await client.post(
                f"/api/v1/install-plans/{plan['id']}/apply",
                json=request,
                headers=headers,
            )
            repeated = await client.post(
                f"/api/v1/install-plans/{plan['id']}/apply",
                json=request,
                headers=headers,
            )
            conflicting = await client.post(
                f"/api/v1/install-plans/{plan['id']}/apply",
                json={"plan_digest": "0" * 64},
                headers=headers,
            )
            repeated_with_new_key = await client.post(
                f"/api/v1/install-plans/{plan['id']}/apply",
                json=request,
                headers={"Idempotency-Key": "install-same-plan-new-key"},
            )
            wrong_digest_with_new_key = await client.post(
                f"/api/v1/install-plans/{plan['id']}/apply",
                json={"plan_digest": "0" * 64},
                headers={"Idempotency-Key": "reject-wrong-plan-digest"},
            )
            installations = await client.get("/api/v1/solution-installations")

        self.assertEqual(missing_key.status_code, 409, missing_key.text)
        self.assertEqual(
            missing_key.json()["detail"]["code"],
            "IDEMPOTENCY_KEY_REQUIRED",
        )
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(repeated.status_code, 201, repeated.text)
        self.assertEqual(repeated.json(), first.json())
        self.assertEqual(conflicting.status_code, 409, conflicting.text)
        self.assertEqual(
            conflicting.json()["detail"]["code"],
            "IDEMPOTENCY_KEY_REUSED",
        )
        self.assertEqual(repeated_with_new_key.status_code, 201, repeated_with_new_key.text)
        self.assertEqual(repeated_with_new_key.json(), first.json())
        self.assertEqual(wrong_digest_with_new_key.status_code, 409)
        self.assertEqual(
            wrong_digest_with_new_key.json()["detail"]["code"],
            "INSTALL_PLAN_DIGEST_MISMATCH",
        )
        self.assertEqual(first.json()["status"], "installed")
        self.assertEqual(first.json()["site_configuration_version"], 1)
        self.assertEqual(
            installations.json(),
            {"items": [first.json()], "total": 1},
        )

    async def test_replanning_an_installed_digest_preserves_the_site_version(self) -> None:
        from app.api.solution_delivery import (
            get_solution_delivery,
            router as delivery_router,
        )
        from app.services.solution_delivery import (
            InMemoryDeliveryRepository,
            SolutionDelivery,
        )

        app = FastAPI()
        app.include_router(delivery_router, prefix="/api/v1")
        delivery = SolutionDelivery(InMemoryDeliveryRepository(), platform_version="0.4.77")
        app.dependency_overrides[get_solution_delivery] = lambda: delivery
        async with AuthenticatedDeliveryClient(app) as client:
            imported = await client.post(
                "/api/v1/solution-packages/import",
                files={"archive": ("minimal.zip", build_minimal_package(), "application/zip")},
            )
            first_plan_response = await client.post(
                f"/api/v1/solution-packages/{imported.json()['id']}/install-plans",
                json={},
            )
            first_plan = first_plan_response.json()
            first_install = await client.post(
                f"/api/v1/install-plans/{first_plan['id']}/apply",
                json={"plan_digest": first_plan["digest"]},
                headers={"Idempotency-Key": "initial-install"},
            )
            preserve_plan_response = await client.post(
                f"/api/v1/solution-packages/{imported.json()['id']}/install-plans",
                json={},
            )
            preserve_plan = preserve_plan_response.json()
            preserved = await client.post(
                f"/api/v1/install-plans/{preserve_plan['id']}/apply",
                json={"plan_digest": preserve_plan["digest"]},
                headers={"Idempotency-Key": "preserve-existing-install"},
            )
            installations = await client.get("/api/v1/solution-installations")

        self.assertEqual(first_install.status_code, 201, first_install.text)
        self.assertEqual(preserve_plan_response.status_code, 201, preserve_plan_response.text)
        self.assertEqual(
            {item["action"] for item in preserve_plan["items"]},
            {"preserve"},
        )
        self.assertEqual(preserved.status_code, 201, preserved.text)
        self.assertEqual(preserved.json(), first_install.json())
        self.assertEqual(
            installations.json(),
            {"items": [first_install.json()], "total": 1},
        )

    async def test_stale_plan_is_rejected_without_creating_an_installation(self) -> None:
        from app.api.solution_delivery import (
            get_solution_delivery,
            router as delivery_router,
        )
        from app.services.solution_delivery import (
            InMemoryDeliveryRepository,
            SolutionDelivery,
        )

        app = FastAPI()
        app.include_router(delivery_router, prefix="/api/v1")
        delivery = SolutionDelivery(InMemoryDeliveryRepository(), platform_version="0.4.77")
        app.dependency_overrides[get_solution_delivery] = lambda: delivery
        async with AuthenticatedDeliveryClient(app) as client:
            first_package = await client.post(
                "/api/v1/solution-packages/import",
                files={
                    "archive": (
                        "first.zip",
                        build_minimal_package(package_id="org.zizu.first"),
                        "application/zip",
                    )
                },
            )
            second_package = await client.post(
                "/api/v1/solution-packages/import",
                files={
                    "archive": (
                        "second.zip",
                        build_minimal_package(package_id="org.zizu.second"),
                        "application/zip",
                    )
                },
            )
            first_plan_response = await client.post(
                f"/api/v1/solution-packages/{first_package.json()['id']}/install-plans",
                json={},
            )
            stale_plan_response = await client.post(
                f"/api/v1/solution-packages/{second_package.json()['id']}/install-plans",
                json={},
            )
            first_plan = first_plan_response.json()
            stale_plan = stale_plan_response.json()
            installed = await client.post(
                f"/api/v1/install-plans/{first_plan['id']}/apply",
                json={"plan_digest": first_plan["digest"]},
                headers={"Idempotency-Key": "advance-site-version"},
            )
            rejected = await client.post(
                f"/api/v1/install-plans/{stale_plan['id']}/apply",
                json={"plan_digest": stale_plan["digest"]},
                headers={"Idempotency-Key": "reject-stale-plan"},
            )
            installations = await client.get("/api/v1/solution-installations")

        self.assertEqual(installed.status_code, 201, installed.text)
        self.assertEqual(rejected.status_code, 409, rejected.text)
        self.assertEqual(rejected.json()["detail"]["code"], "INSTALL_PLAN_STALE")
        self.assertEqual(installations.json()["total"], 1)

    async def test_installed_package_runs_liveness_and_returns_immutable_report(self) -> None:
        from app.api.solution_delivery import (
            get_solution_delivery,
            router as delivery_router,
        )
        from app.services.solution_delivery import (
            InMemoryDeliveryRepository,
            SolutionDelivery,
        )

        app = FastAPI()
        app.include_router(health_router, prefix="/api/v1")
        app.include_router(delivery_router, prefix="/api/v1")
        repository = InMemoryDeliveryRepository()
        delivery = SolutionDelivery(
            repository,
            platform_version="0.4.77",
            public_api_probe=AsgiPublicApiProbe(app),
        )
        app.dependency_overrides[get_solution_delivery] = lambda: delivery
        async with AuthenticatedDeliveryClient(app) as client:
            imported = await client.post(
                "/api/v1/solution-packages/import",
                files={
                    "archive": (
                        "minimal-liveness.zizu.zip",
                        build_minimal_package(),
                        "application/zip",
                    )
                },
            )
            planned = await client.post(
                f"/api/v1/solution-packages/{imported.json()['id']}/install-plans",
                json={},
            )
            plan = planned.json()
            installed = await client.post(
                f"/api/v1/install-plans/{plan['id']}/apply",
                json={"plan_digest": plan["digest"]},
                headers={"Idempotency-Key": "install-for-acceptance"},
            )
            installation = installed.json()
            missing_key = await client.post(
                f"/api/v1/solution-installations/{installation['id']}/acceptance-runs",
            )
            run = await client.post(
                f"/api/v1/solution-installations/{installation['id']}/acceptance-runs",
                headers={"Idempotency-Key": "accept-minimal-liveness"},
            )
            repeated_run = await client.post(
                f"/api/v1/solution-installations/{installation['id']}/acceptance-runs",
                headers={"Idempotency-Key": "accept-minimal-liveness"},
            )
            fresh_run = await client.post(
                f"/api/v1/solution-installations/{installation['id']}/acceptance-runs",
                headers={"Idempotency-Key": "accept-minimal-liveness-again"},
            )
            report = await client.get(
                f"/api/v1/delivery-reports/{run.json()['id']}"
            )

        self.assertEqual(missing_key.status_code, 409, missing_key.text)
        self.assertEqual(
            missing_key.json()["detail"]["code"],
            "IDEMPOTENCY_KEY_REQUIRED",
        )
        self.assertEqual(run.status_code, 201, run.text)
        self.assertEqual(repeated_run.status_code, 201, repeated_run.text)
        self.assertEqual(repeated_run.json(), run.json())
        self.assertEqual(fresh_run.status_code, 201, fresh_run.text)
        self.assertNotEqual(fresh_run.json()["id"], run.json()["id"])
        self.assertEqual(report.status_code, 200, report.text)
        self.assertEqual(report.json(), run.json())
        body = report.json()
        self.assertEqual(body["status"], "passed")
        self.assertEqual(body["platform_version"], "0.4.77")
        self.assertEqual(body["package_id"], "org.zizu.minimal-liveness")
        self.assertEqual(body["package_version"], "1.0.0")
        self.assertEqual(body["site_configuration_version"], 1)
        self.assertEqual(len(body["digest"]), 64)
        self.assertEqual(
            body["items"],
            [
                {
                    "acceptance_id": "acceptance.platform-liveness",
                    "status": "passed",
                    "code": "PLATFORM_LIVE",
                    "required": True,
                    "duration_ms": body["items"][0]["duration_ms"],
                    "evidence": {"status": "alive", "version": "0.4.77"},
                }
            ],
        )

    async def test_liveness_failures_are_saved_as_redacted_machine_reports(self) -> None:
        from app.api.solution_delivery import (
            get_solution_delivery,
            router as delivery_router,
        )
        from app.services.solution_delivery import (
            InMemoryDeliveryRepository,
            SolutionDelivery,
        )

        expected = {
            "timeout": ("LIVENESS_TIMEOUT", {"error": "timeout"}),
            "network": ("LIVENESS_HTTP_ERROR", {"error": "request_failed"}),
            "http": ("LIVENESS_HTTP_ERROR", {"http_status": 503}),
            "contract": (
                "LIVENESS_RESPONSE_INVALID",
                {"status": "wrong", "version": None},
            ),
        }
        for failure, (code, evidence) in expected.items():
            with self.subTest(failure=failure):
                app = FastAPI()
                app.include_router(delivery_router, prefix="/api/v1")
                repository = InMemoryDeliveryRepository()
                delivery = SolutionDelivery(
                    repository,
                    platform_version="0.4.77",
                    public_api_probe=FailingPublicApiProbe(failure),
                )
                app.dependency_overrides[get_solution_delivery] = lambda: delivery
                async with AuthenticatedDeliveryClient(app) as client:
                    imported = await client.post(
                        "/api/v1/solution-packages/import",
                        files={"archive": ("minimal.zip", build_minimal_package(), "application/zip")},
                    )
                    plan_response = await client.post(
                        f"/api/v1/solution-packages/{imported.json()['id']}/install-plans",
                        json={},
                    )
                    plan = plan_response.json()
                    install_response = await client.post(
                        f"/api/v1/install-plans/{plan['id']}/apply",
                        json={"plan_digest": plan["digest"]},
                        headers={"Idempotency-Key": f"install-{failure}"},
                    )
                    installation = install_response.json()
                    run = await client.post(
                        f"/api/v1/solution-installations/{installation['id']}/acceptance-runs",
                        headers={"Idempotency-Key": f"accept-{failure}"},
                    )

                self.assertEqual(run.status_code, 201, run.text)
                report = run.json()
                self.assertEqual(report["status"], "failed")
                self.assertEqual(report["items"][0]["status"], "failed")
                self.assertEqual(report["items"][0]["code"], code)
                self.assertEqual(report["items"][0]["evidence"], evidence)


if __name__ == "__main__":
    unittest.main()
