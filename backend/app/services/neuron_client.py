"""
Neuron API Client — Neuron 工业协议网关 API 封装

封装 Neuron REST API v2，提供节点/组/点位管理功能。
使用标准库 urllib 替代 httpx，避免目标镜像缺少 httpx 依赖。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Literal

from loguru import logger

from app.core.secret_policy import insecure_development_enabled, validate_secret


@dataclass
class NeuronConfig:
    url: str = "http://127.0.0.1:7000"
    username: str = "admin"
    password: str = ""
    timeout: float = 10.0
    deployment_mode: Literal["production", "development"] = "production"
    allow_insecure_dev_secrets: bool = False

    def __post_init__(self) -> None:
        allow_insecure = insecure_development_enabled(
            self.deployment_mode,
            self.allow_insecure_dev_secrets,
        )
        self.password = validate_secret(
            "neuron", self.password, allow_insecure=allow_insecure, warn=allow_insecure
        )


class NeuronClient:
    """Neuron API 客户端。"""

    def __init__(self, config: NeuronConfig):
        self.config = config
        self._token: str | None = None
        self._timeout = self.config.timeout

    def _http_request(self, method: str, url: str, data: dict | None = None, headers: dict | None = None) -> dict:
        """使用 urllib 发送 JSON 请求并解析响应。"""
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", **(headers or {})},
            method=method.upper(),
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            content = resp.read().decode("utf-8")
            return json.loads(content) if content else {}

    def _ensure_token(self) -> str:
        """确保已登录，返回 JWT token。"""
        if self._token:
            return self._token

        data = self._http_request(
            "POST",
            f"{self.config.url}/api/v2/login",
            data={"name": self.config.username, "pass": self.config.password},
        )
        self._token = data.get("token")
        if not self._token:
            raise RuntimeError(f"Neuron login failed: {data}")
        logger.info("[Neuron] Login success")
        return self._token

    def _request(self, method: str, path: str, **kwargs) -> Any:
        """发送 API 请求。"""
        token = self._ensure_token()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {token}"

        data = kwargs.pop("json", None)
        url = f"{self.config.url}{path}"
        return self._http_request(method, url, data=data, headers=headers)

    # ══════════════════════════════════════════
    # 节点管理 (Driver Nodes)
    # ══════════════════════════════════════════

    def get_nodes(self, node_type: int = 1) -> list[dict]:
        """
        获取节点列表。

        Args:
            node_type: 1=驱动节点(driver), 2=应用节点(app)
        """
        data = self._request("GET", f"/api/v2/node?type={node_type}")
        return data.get("nodes", [])

    def get_node(self, node_name: str) -> dict | None:
        """获取单个节点详情。"""
        try:
            data = self._request("GET", f"/api/v2/node/{node_name}")
            return data
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def add_node(self, name: str, plugin: str, params: dict) -> dict:
        """
        添加驱动节点。

        Args:
            name: 节点名称
            plugin: 插件名称 (如 "modbus-tcp", "modbus-rtu")
            params: 节点参数 (如 {"host": "192.168.1.100", "port": 502})
        """
        payload = {
            "name": name,
            "plugin": plugin,
            "params": params,
        }
        return self._request("POST", "/api/v2/node", json=payload)

    def update_node(self, name: str, params: dict) -> dict:
        """更新节点配置。"""
        payload = {"params": params}
        return self._request("PUT", f"/api/v2/node/{name}", json=payload)

    def delete_node(self, name: str) -> dict:
        """删除节点。"""
        return self._request("DELETE", f"/api/v2/node/{name}")

    def start_node(self, name: str) -> dict:
        """启动节点。"""
        return self._request("PUT", f"/api/v2/node/{name}/start")

    def stop_node(self, name: str) -> dict:
        """停止节点。"""
        return self._request("PUT", f"/api/v2/node/{name}/stop")

    def get_node_state(self, name: str) -> dict:
        """获取节点状态。"""
        return self._request("GET", f"/api/v2/node/{name}/state")

    # ══════════════════════════════════════════
    # 组管理 (Groups)
    # ══════════════════════════════════════════

    def get_groups(self, node_name: str) -> list[dict]:
        """获取节点下的组列表。"""
        data = self._request("GET", f"/api/v2/group?node={node_name}")
        return data.get("groups", [])

    def add_group(self, node_name: str, group_name: str, interval: int = 1000) -> dict:
        """
        添加采集组。

        Args:
            node_name: 节点名称
            group_name: 组名称
            interval: 采集间隔 (毫秒)
        """
        payload = {
            "node": node_name,
            "name": group_name,
            "interval": interval,
        }
        return self._request("POST", "/api/v2/group", json=payload)

    def update_group(self, node_name: str, group_name: str, interval: int) -> dict:
        """更新组配置。"""
        payload = {"interval": interval}
        return self._request("PUT", f"/api/v2/group/{node_name}/{group_name}", json=payload)

    def delete_group(self, node_name: str, group_name: str) -> dict:
        """删除组。"""
        return self._request("DELETE", f"/api/v2/group/{node_name}/{group_name}")

    # ══════════════════════════════════════════
    # 点位管理 (Tags)
    # ══════════════════════════════════════════

    def get_tags(self, node_name: str, group_name: str) -> list[dict]:
        """获取组下的点位列表。

        Neuron 2.10.x 使用复数路径 /api/v2/tags；单数 /api/v2/tag 在 2.10 会 404。
        """
        data = self._request("GET", f"/api/v2/tags?node={node_name}&group={group_name}")
        return data.get("tags", [])

    def add_tags(self, node_name: str, group_name: str, tags: list[dict]) -> dict:
        """
        批量添加点位。

        Args:
            tags: [{"name": "tag1", "address": "1!405001.0", "attribute": 1, "type": 4}, ...]
        """
        payload = {
            "node": node_name,
            "group": group_name,
            "tags": tags,
        }
        return self._request("POST", "/api/v2/tag", json=payload)

    def update_tag(self, node_name: str, group_name: str, tag_name: str, **kwargs) -> dict:
        """更新点位。"""
        payload = kwargs
        return self._request("PUT", f"/api/v2/tag/{node_name}/{group_name}/{tag_name}", json=payload)

    def delete_tag(self, node_name: str, group_name: str, tag_name: str) -> dict:
        """删除点位。"""
        return self._request("DELETE", f"/api/v2/tag/{node_name}/{group_name}/{tag_name}")

    def write_tag(self, node_name: str, group_name: str, tag_name: str, value) -> dict:
        """
        写单个点位。

        Neuron POST /api/v2/write
        payload: {"node": node_name, "group": group_name, "tag": tag_name, "value": value}
        """
        payload = {
            "node": node_name,
            "group": group_name,
            "tag": tag_name,
            "value": value,
        }
        return self._request("POST", "/api/v2/write", json=payload)

    # ══════════════════════════════════════════
    # 状态监控
    # ══════════════════════════════════════════

    def get_global_config(self) -> dict:
        """获取全局配置。"""
        return self._request("GET", "/api/v2/global/config")

    def get_plugin_list(self) -> list[dict]:
        """获取可用插件列表。"""
        data = self._request("GET", "/api/v2/plugin")
        return data.get("plugins", [])

    def get_version(self) -> dict:
        """获取 Neuron 版本信息。"""
        return self._request("GET", "/api/v2/version")

    def close(self) -> None:
        """关闭客户端。"""
        pass


# ══════════════════════════════════════════
# 全局单例
# ══════════════════════════════════════════

_neuron_client: NeuronClient | None = None


def get_neuron_client() -> NeuronClient:
    """获取 Neuron 客户端单例。"""
    global _neuron_client
    if _neuron_client is None:
        from app.core.config import settings

        config = NeuronConfig(
            url=settings.neuron_api_url,
            username=settings.neuron_username,
            password=settings.neuron_password,
            deployment_mode=settings.deployment_mode,
            allow_insecure_dev_secrets=settings.allow_insecure_dev_secrets,
        )
        _neuron_client = NeuronClient(config)
    return _neuron_client
