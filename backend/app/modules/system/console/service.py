"""系统渠道状态：数据库与大模型的统一探测。

状态页只展示能给出真实结论的两项。每个渠道都走同一条探测流程：
配置缺失时明确返回未配置且不发请求；配置完整时执行一次只读、可重复的探测，
记录本次耗时、检查时间与失败原因。不用「已配置」冒充「已连通」。
一次探测的结论在短窗口内复用，响应仍带着那次探测的检查时间与耗时；显式刷新会重新探测，
避免每次查看状态都产生一次计费的大模型调用。
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.integrations.llm import gateway as llm_gateway
from app.modules.system.console.crud import SystemConsoleCRUD

# 探测结论的复用窗口。只影响重复查看，不影响结论本身：
# 每次响应都回传那次探测的时间与耗时，超过窗口后重新探测。
PROBE_CACHE_SECONDS = 60.0


@dataclass(frozen=True)
class ChannelStatus:
    """一次渠道探测的完整结果；页面只投影它，不再自行推断连接状态。"""

    name: str
    capability: str
    configuration_status: str
    connection_status: str
    checked_at: datetime | None
    latency_ms: int | None
    missing: tuple[str, ...] = ()
    note: str | None = None

    def as_service_item(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "capability": self.capability,
            "configuration_status": self.configuration_status,
            "connection_status": self.connection_status,
            "last_checked_at": self.checked_at,
            "latency_ms": self.latency_ms,
            "missing": list(self.missing),
            "note": self.note,
        }




_probe_cache: dict[str, tuple[float, ChannelStatus]] = {}


class ConsoleService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def _elapsed_ms(self, started: float) -> int:
        return max(0, int((time.monotonic() - started) * 1000))


    def _probe(self, name: str, probe: Callable[[], ChannelStatus], *, refresh: bool) -> ChannelStatus:
        now = time.monotonic()
        cached = _probe_cache.get(name)
        if not refresh and cached is not None and now - cached[0] < PROBE_CACHE_SECONDS:
            return cached[1]
        result = probe()
        _probe_cache[name] = (now, result)
        return result


    def _configuration_status(self, required: dict[str, bool]) -> tuple[str, list[str]]:
        missing = [name for name, configured in required.items() if not configured]
        if len(missing) == len(required):
            return "UNCONFIGURED", missing
        if missing:
            return "CONFIGURATION_INCOMPLETE", missing
        return "CONFIGURED", []


    def check_database(self) -> ChannelStatus:
        """数据库：配置完整时执行一次 SELECT 1，不修改任何状态。"""
        settings = get_settings()
        name = "数据库"
        capability = "业务数据与流程检查点存储（baoli_v4）"
        if not settings.database_url:
            return ChannelStatus(
                name, capability, "UNCONFIGURED", "NOT_CONFIGURED", None, None,
                ("BAOLI_DATABASE_URL",), "缺少数据库连接配置",
            )
        started = time.monotonic()
        try:
            SystemConsoleCRUD(self.db).ping()
        except SQLAlchemyError as exc:
            # 不返回异常文本：SQLAlchemy 的连接错误可能带出含密码的 DSN。
            return ChannelStatus(
                name, capability, "CONFIGURED", "FAILED", datetime.now(UTC),
                self._elapsed_ms(started), (), f"连接失败（{type(exc).__name__}）",
            )
        return ChannelStatus(
            name, capability, "CONFIGURED", "CONNECTED", datetime.now(UTC),
            self._elapsed_ms(started),
        )


    def check_llm(self) -> ChannelStatus:
        """大模型：配置完整时用一次最小 chat 请求验证地址、鉴权与模型。"""
        settings = get_settings()
        name = "大模型"
        capability = "规则转换、字段抽取与语义判断"
        configuration, missing = self._configuration_status({
            "BAOLI_LLM_BASEURL": bool(settings.llm_baseurl),
            "BAOLI_LLM_MODEL_NAME": bool(settings.llm_model_name),
        })
        if missing:
            return ChannelStatus(
                name, capability, configuration, "NOT_CONFIGURED", None, None,
                tuple(missing), f"缺少：{'、'.join(missing)}",
            )
        probe = llm_gateway.probe_connection()
        checked_at = datetime.now(UTC)
        if probe.ok:
            return ChannelStatus(
                name, capability, configuration, "CONNECTED", checked_at, probe.latency_ms,
            )
        return ChannelStatus(
            name, capability, configuration, "FAILED", checked_at, probe.latency_ms,
            (), f"探测失败：{probe.error_message or probe.error_code}",
        )


    def service_items(self, *, refresh: bool = False) -> list[ChannelStatus]:
        return [
            self._probe("database", self.check_database, refresh=refresh),
            self._probe("llm", self.check_llm, refresh=refresh),
        ]


    def status_view(self) -> dict[str, Any]:
        """系统总体状态：数据库与大模型的连接结论都来自同一份真实探测。"""
        items = self.service_items()
        database, llm = items
        healthy = (
            database.connection_status == "CONNECTED"
            and llm.connection_status == "CONNECTED"
        )
        return {
            "service": "RUNNING" if healthy else "DEGRADED",
            "database": database.connection_status,
            "services": [item.as_service_item() for item in items],
        }
