"""
ZiZu 全局配置管理

基于 pydantic-settings，从环境变量 / .env 文件加载。
所有可配置项集中在此，避免散落各处的 hardcode。
"""
from __future__ import annotations

from functools import lru_cache
import ipaddress
from typing import Literal

from pydantic import Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.secret_policy import insecure_development_enabled, validate_secret


class Settings(BaseSettings):
    """全局配置 — 单例，通过 settings() 函数获取。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 应用基础 ----
    app_name: str = "ZiZu"
    app_port: int = 9000
    debug: bool = False
    log_level: str = "INFO"
    public_api_base_url: str | None = None
    deployment_mode: Literal["production", "development"] = "production"
    allow_insecure_dev_secrets: bool = False

    @property
    def insecure_development_mode(self) -> bool:
        return insecure_development_enabled(
            self.deployment_mode,
            self.allow_insecure_dev_secrets,
        )

    @property
    def effective_public_api_base_url(self) -> str:
        """验收模块访问本实例公开接口的基址。"""
        return self.public_api_base_url or f"http://127.0.0.1:{self.app_port}"

    # ---- 数据库 (TimescaleDB / PostgreSQL) ----
    db_host: str = "timescaledb"
    db_port: int = 5432
    db_name: str = "zizu"
    db_user: str = "zizu"
    db_password: str = Field(min_length=1)
    db_pool_min: int = 2
    db_pool_max: int = 15

    @property
    def database_url(self) -> str:
        """同步连接串 (psycopg2 批量写入用)。"""
        return (
            f"postgresql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def database_url_async(self) -> str:
        """异步连接串 (asyncpg / FastAPI 用)。"""
        return (
            f"postgresql+asyncpg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    # ---- MQTT (nanoMQ) ----
    mqtt_host: str = "nanomq"
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    mqtt_client_id: str = "zizu-backend"
    mqtt_qos: int = 0  # QoS 0: at-most-once, broker 不缓存重发，降低内存堆积风险
    # 订阅的 topic 模式 — Neuron 上报 telemetry 使用此前缀
    # 支持逗号分隔的多 topic，例如 "telemetry/#,/neuron/MQTT"
    mqtt_telemetry_topic: str = "/neuron/#"
    # 分级告警 MQTT topic，payload 中应包含 error1/error2/error3 分组
    mqtt_alarm_topic: str = "/alarm/#"
    # MQTT 连接保活
    mqtt_keepalive: int = 60

    @property
    def mqtt_telemetry_topics(self) -> list[str]:
        """将逗号分隔的 topic 字符串解析为 topic 列表。"""
        return [t.strip() for t in self.mqtt_telemetry_topic.split(",") if t.strip()]

    @property
    def mqtt_alarm_topics(self) -> list[str]:
        """告警 topic 列表。"""
        return [t.strip() for t in self.mqtt_alarm_topic.split(",") if t.strip()]
    # 断线重连间隔 (秒)
    mqtt_reconnect_delay: float = 5.0

    # ---- Neuron (设备接入网关) ----
    neuron_api_url: str = "http://neuron:7000"
    neuron_api_version: str = "/api/v2"
    neuron_username: str = "admin"
    neuron_password: str = Field(min_length=1)

    # ---- nanoMQ REST API ----
    nanomq_api_url: str = "http://nanomq:8081"
    nanomq_api_username: str = "admin"
    nanomq_api_password: str = Field(min_length=1)
    nanomq_conf_path: str = "/app/config/nanomq.conf"


    # ---- CORS ----
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:5173"]

    # ---- JWT (M7 RPC 控制) ----
    jwt_secret: str = Field(min_length=32)
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440  # 24h
    auth_session_minutes: int = Field(default=480, ge=5, le=1440)
    auth_require_https: bool = True
    auth_trust_proxy_headers: bool = False
    auth_trusted_proxy_cidrs: list[str] = Field(default_factory=list)

    @field_validator(
        "db_password",
        "neuron_password",
        "nanomq_api_password",
        "jwt_secret",
    )
    @classmethod
    def validate_runtime_secret(cls, value: str, info: ValidationInfo) -> str:
        kinds = {
            "db_password": "database",
            "neuron_password": "neuron",
            "nanomq_api_password": "nanomq",
            "jwt_secret": "jwt",
        }
        allow_insecure = insecure_development_enabled(
            info.data.get("deployment_mode", "production"),
            info.data.get("allow_insecure_dev_secrets", False),
        )
        return validate_secret(
            kinds[info.field_name],
            value,
            allow_insecure=allow_insecure,
            warn=allow_insecure,
        )

    @model_validator(mode="after")
    def validate_development_mode(self) -> "Settings":
        insecure_development_enabled(
            self.deployment_mode,
            self.allow_insecure_dev_secrets,
        )
        if self.deployment_mode == "production" and not self.auth_require_https:
            raise ValueError("production requires AUTH_REQUIRE_HTTPS=true")
        if self.auth_trust_proxy_headers and not self.auth_trusted_proxy_cidrs:
            raise ValueError(
                "AUTH_TRUST_PROXY_HEADERS requires AUTH_TRUSTED_PROXY_CIDRS"
            )
        for cidr in self.auth_trusted_proxy_cidrs:
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError as exc:
                raise ValueError(
                    f"invalid AUTH_TRUSTED_PROXY_CIDRS entry: {cidr!r}"
                ) from exc
        return self

    # ---- 管道性能参数 ----
    # 批量写入 TSDB 的条数阈值 (攒够 N 条或 T 秒就 flush)
    pipeline_batch_size: int = 200
    pipeline_flush_interval_sec: float = 1.0
    # tag 规则/映射动态重载间隔 (秒)；导入新点位后无需重启即可生效
    pipeline_reload_rules_interval_sec: float = 30.0
    # CE Path C 跨节点聚合调度间隔
    ce_aggregation_interval_sec: float = 10.0


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取配置单例。"""
    return Settings()


settings = get_settings()
