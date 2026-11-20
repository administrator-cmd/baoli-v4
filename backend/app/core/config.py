from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_DIR = BACKEND_DIR.parent

# 业务库与开发验证库是两个明确的部署目标。这里集中声明允许的库名，
# database.py 与维护脚本共用同一口径，避免只改一处导致应用和脚本实际连接到不同目标。
# 其它库名一律拒绝，防止误把清库操作指向未知库。
ALLOWED_DATABASE_NAMES = frozenset({"baoli_v4", "baoli_v4_dev"})


def validated_database_url(database_url: str | None) -> URL:
    """解析并校验应用允许使用的 MySQL 数据库目标。

    返回原始 URL 的主机、账号、参数与数据库名，不偷偷改写目标库。
    调用方根据自己的错误类型包装 ValueError，避免配置层依赖 HTTP 或业务异常。
    """
    if not database_url:
        raise ValueError("BAOLI_DATABASE_URL 未配置")
    try:
        url = make_url(database_url)
    except Exception as exc:  # noqa: BLE001 - 配置错误需要暴露原始原因
        raise ValueError("BAOLI_DATABASE_URL 格式无效") from exc
    if url.drivername != "mysql+pymysql":
        raise ValueError(f"数据库驱动必须是 mysql+pymysql，当前是 {url.drivername}")
    if url.database not in ALLOWED_DATABASE_NAMES:
        allowed = "、".join(sorted(ALLOWED_DATABASE_NAMES))
        raise ValueError(f"数据库目标必须是允许的库（{allowed}），当前是 {url.database or '未指定'}")
    return url


class Settings(BaseSettings):
    """baoli-v4 的唯一环境配置入口。"""

    model_config = SettingsConfigDict(
        env_file=PROJECT_DIR / ".env",
        env_file_encoding="utf-8",
        env_prefix="BAOLI_",
        extra="ignore",
    )

    app_name: str = "保理合同审核验证版"
    api_prefix: str = "/api"
    database_url: str | None = None
    data_dir: Path = Path("data")
    # 上传入口的体量上限。两者互相独立：规则库是配置表，业务材料是合同/协议扫描件，
    # 不是一回事，不能共用一个数值。
    # 只校验为正数，不设固定上界：真实运行环境里几十 MiB 的扫描件是正常的，
    # 写死上界会让正常业务在入口就被拒。
    # 这里只约束本系统的上传入口；8018/8010/OCR 与部署入口（反向代理等）
    # 各自的体量上限是另一回事，本配置不构成对它们的承诺，也未经连通验证。
    max_rulebook_bytes: int = Field(default=30 * 1024 * 1024, gt=0)
    max_document_bytes: int = Field(default=128 * 1024 * 1024, gt=0)
    # 工作簿 IR 的预览行数、列数与单元格字符数；LLM 分页读取不使用这些设置。
    workbook_preview_rows: int = Field(default=200, ge=1, le=200)
    workbook_preview_columns: int = Field(default=40, ge=1, le=40)
    workbook_cell_chars: int = Field(default=500, ge=1, le=500)

    llm_baseurl: str | None = None
    llm_apikey: SecretStr | None = None
    llm_model_name: str | None = None
    # 规则库解析的供应商推理强度；未配置时不传，由供应商决定。
    # 与审核任务模型请求分开，避免复杂规则库整理耗尽长响应时间。
    rulebook_llm_reasoning_effort: str | None = None
    rulebook_grouped_import: bool = False
    # 仅字段配置组可以单独试用较低推理强度；未设置时沿用规则库设置。
    rulebook_group_field_reasoning_effort: str | None = None
    # 字符预算用于分包，不代表供应商 token 数。容不下不可分割的原文时显式报错。
    rulebook_group_input_chars: int = Field(default=48000, ge=8000)
    rulebook_group_objects: int = Field(default=8, ge=1, le=64)
    # 工具调用循环的轮数上限。达到上限时审核流程显式失败（LLM_TOOL_LOOP_LIMIT），
    # 不把「跑到上限」当作结论，也不静默降级成普通文本解析。
    # 0 表示不设上限，由调用方自行控制终止——上限是安全阀，不是业务默认。
    llm_tool_max_rounds: int = Field(default=12, ge=0)
    # 未指定时使用服务端的输出上限。
    llm_max_output_tokens: int | None = Field(default=None, gt=0)
    llm_max_concurrent_requests: int = Field(default=2, ge=1)
    # 每个槽位独立计完成间隔，设为 0 可立即复用槽位。
    llm_request_interval_seconds: float = Field(default=2.0, ge=0, allow_inf_nan=False)
    # 可重试 HTTP 错误的退避基数；再按尝试次数指数增长并加入抖动。
    llm_retry_base_seconds: float = Field(default=10.0, gt=0, allow_inf_nan=False)

    service_8018_url: str | None = None
    service_8010_url: str | None = None
    ocr_service_url: str | None = None
    seal_service_url: str | None = None
    ocr_token: SecretStr | None = None
    ocr_model: str | None = None
    ocr_poll_interval_seconds: float = Field(default=5.0, gt=0)
    ocr_max_wait_seconds: float = Field(default=600.0, gt=0)
    ocr_max_concurrent_jobs: int = Field(default=5, ge=1)

    # 普通外部 HTTP 请求的超时，不是整轮 OCR 作业的超时，也不用于 LLM 长响应。
    request_timeout_seconds: float = Field(default=600.0, gt=0)
    # 模型在持续输出时不能按总时长判失败：这是「连续多久收不到任何数据就判定请求卡住」
    # 的读取超时。规则库解析、抽取等长响应只要还在出块就一直继续。
    llm_timeout_seconds: float = Field(default=900.0, gt=0)
    # 单次模型请求（含自动重试）的总时限，只用于兜住永不结束的流。
    # 真实解析耗时超过它时按实际耗时调大，这与「网络不可达」是两件事。
    llm_max_seconds: float = Field(default=7200.0, gt=0)
    log_level: str = "INFO"
    httpx_log_level: str = "WARNING"

    @field_validator(
        "database_url",
        "llm_baseurl",
        "llm_apikey",
        "llm_model_name",
        "llm_max_output_tokens",
        "rulebook_llm_reasoning_effort",
        "rulebook_group_field_reasoning_effort",
        "service_8018_url",
        "service_8010_url",
        "ocr_service_url",
        "seal_service_url",
        "ocr_token",
        "ocr_model",
        mode="before",
    )
    @classmethod
    def _strip_optional_text(cls, value: Any) -> Any:
        if isinstance(value, str):
            normalized = value.strip()
            # 照抄 .env.example 的占位文本不算已配置，否则会被报告成可用的外部服务。
            if not normalized or "<填写" in normalized:
                return None
            return normalized
        return value

    @field_validator("log_level", "httpx_log_level")
    @classmethod
    def _validate_log_level(cls, value: str, info: ValidationInfo) -> str:
        normalized = value.strip().upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            env_name = f"BAOLI_{info.field_name.upper()}"
            raise ValueError(f"{env_name} 必须是 DEBUG、INFO、WARNING、ERROR 或 CRITICAL")
        return normalized

    @property
    def storage_root(self) -> Path:
        root = self.data_dir
        if not root.is_absolute():
            root = PROJECT_DIR / root
        return root.resolve()

    @staticmethod
    def _secret_value(value: SecretStr | None) -> str | None:
        if value is None:
            return None
        normalized = value.get_secret_value().strip()
        return normalized or None

    @property
    def llm_api_key_value(self) -> str | None:
        return self._secret_value(self.llm_apikey)

    @property
    def ocr_token_value(self) -> str | None:
        return self._secret_value(self.ocr_token)


@lru_cache
def get_settings() -> Settings:
    return Settings()
