"""
nanoMQ REST API Client

使用 urllib 封装 nanoMQ v4 REST API，避免预构建镜像缺少 httpx 依赖。
支持：状态/客户端/订阅查询、消息发布、ACL 管理、配置文件读写。
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from loguru import logger

from app.core.secret_policy import insecure_development_enabled, validate_secret


@dataclass
class NanoMQConfig:
    url: str = "http://nanomq:8081"
    username: str = "admin"
    password: str = ""
    timeout: float = 10.0
    conf_path: str = "/app/config/nanomq.conf"
    deployment_mode: Literal["production", "development"] = "production"
    allow_insecure_dev_secrets: bool = False

    def __post_init__(self) -> None:
        allow_insecure = insecure_development_enabled(
            self.deployment_mode,
            self.allow_insecure_dev_secrets,
        )
        self.password = validate_secret(
            "nanomq", self.password, allow_insecure=allow_insecure, warn=allow_insecure
        )


class NanoMQClient:
    """nanoMQ REST API 客户端。"""

    def __init__(self, config: NanoMQConfig):
        self.config = config
        self._timeout = self.config.timeout

    def _auth_header(self) -> str:
        creds = f"{self.config.username}:{self.config.password}".encode("utf-8")
        return "Basic " + base64.b64encode(creds).decode("utf-8")

    def _request(
        self,
        method: str,
        path: str,
        data: dict | list | None = None,
        headers: dict | None = None,
    ) -> Any:
        url = f"{self.config.url.rstrip('/')}{path}"
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req_headers = {
            "Content-Type": "application/json",
            "Authorization": self._auth_header(),
            **(headers or {}),
        }
        req = urllib.request.Request(
            url,
            data=body,
            headers=req_headers,
            method=method.upper(),
        )
        logger.debug("[nanoMQ] {} {}", method.upper(), url)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                content = resp.read().decode("utf-8")
                return json.loads(content) if content else {}
        except urllib.error.HTTPError as e:
            content = e.read().decode("utf-8") or "{}"
            try:
                detail = json.loads(content)
            except Exception:
                detail = {"message": content}
            logger.warning("[nanoMQ] HTTP {}: {}", e.code, detail)
            raise NanoMQAPIError(status_code=e.code, detail=detail) from e
        except Exception as e:
            logger.error("[nanoMQ] Request failed: {}", e)
            raise NanoMQAPIError(status_code=503, detail={"message": str(e)}) from e

    # ══════════════════════════════════════════
    # 状态与监控
    # ══════════════════════════════════════════

    def get_brokers(self) -> dict:
        return self._request("GET", "/api/v4/brokers")

    def get_nodes(self) -> dict:
        return self._request("GET", "/api/v4/nodes")

    def get_metrics(self) -> dict:
        return self._request("GET", "/api/v4/metrics")

    def get_status(self) -> dict:
        """聚合 brokers/nodes/metrics 为统一状态。"""
        return {
            "brokers": self.get_brokers(),
            "nodes": self.get_nodes(),
            "metrics": self.get_metrics(),
        }

    # ══════════════════════════════════════════
    # 客户端与订阅
    # ══════════════════════════════════════════

    def get_clients(self) -> dict:
        return self._request("GET", "/api/v4/clients")

    def get_subscriptions(self) -> dict:
        return self._request("GET", "/api/v4/subscriptions")

    def get_routes(self) -> dict:
        return self._request("GET", "/api/v4/routes")

    # ══════════════════════════════════════════
    # 消息发布/订阅代理
    # ══════════════════════════════════════════

    def publish(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> dict:
        return self._request(
            "POST",
            "/api/v4/mqtt/publish",
            data={"topic": topic, "payload": payload, "qos": qos, "retain": retain},
        )

    def subscribe(self, topic: str, qos: int = 0) -> dict:
        return self._request(
            "POST",
            "/api/v4/mqtt/subscribe",
            data={"topic": topic, "qos": qos},
        )

    # ══════════════════════════════════════════
    # ACL 管理
    # ══════════════════════════════════════════

    def get_acl(self) -> dict:
        return self._request("GET", "/api/v4/acl")

    def set_acl(self, rules: list[dict]) -> dict:
        return self._request("POST", "/api/v4/acl", data=rules)

    # ══════════════════════════════════════════
    # 配置文件
    # ══════════════════════════════════════════

    def read_conf(self) -> str:
        path = Path(self.config.conf_path)
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def write_conf(self, content: str) -> None:
        path = Path(self.config.conf_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 写前备份
        if path.exists():
            backup = path.with_suffix(".conf.bak")
            backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        path.write_text(content, encoding="utf-8")


class NanoMQAPIError(Exception):
    """nanoMQ API 返回的错误。"""

    def __init__(self, status_code: int, detail: Any):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"nanoMQ API error {status_code}: {detail}")


# ══════════════════════════════════════════
# 全局客户端工厂
# ══════════════════════════════════════════

_nanomq_client: NanoMQClient | None = None


def get_nanomq_client() -> NanoMQClient:
    """从全局配置构造 nanoMQ 客户端（单例）。"""
    global _nanomq_client
    if _nanomq_client is None:
        from app.core.config import settings

        _nanomq_client = NanoMQClient(
            NanoMQConfig(
                url=settings.nanomq_api_url,
                username=settings.nanomq_api_username,
                password=settings.nanomq_api_password,
                conf_path=settings.nanomq_conf_path,
                deployment_mode=settings.deployment_mode,
                allow_insecure_dev_secrets=settings.allow_insecure_dev_secrets,
            )
        )
    return _nanomq_client


def reset_nanomq_client() -> None:
    """重置单例，用于配置热更新后重建客户端。"""
    global _nanomq_client
    _nanomq_client = None
