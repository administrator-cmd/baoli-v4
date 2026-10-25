"""外部文档服务端口：审核层与具体供应商实现之间的唯一接缝。

审核层需要四种外部能力——材料类型校验、文件处理、印章识别、OCR 作业——但它们各自的 HTTP
契约、字段名与失败语义属于供应商细节。这里只声明审核层用到的入口，供应商适配器
（`integrations.documents`）在应用启动时注册进来。

这样审核层不导入任何供应商模块，供应商契约变化只影响 `integrations/*`；端口签名是显式的，
替换或缺失会在启动自检时暴露，不会拖到真实任务执行时才报「没有这个函数」。未注册任何实现时
调用明确报错，不静默退回空结果或假成功。
"""

from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from ...common.exceptions import BusinessError
from ...domain.external_artifacts import ExternalServiceResult, OcrJobSubmission


class DocumentServicePort(Protocol):
    """审核层用到的四种外部能力。"""

    def validate_material_type(
        self,
        *,
        file_path: Path,
        filename: str,
        content_type: str,
        material_type: str,
    ) -> ExternalServiceResult: ...

    def process_document(
        self,
        *,
        file_path: Path,
        filename: str,
        content_type: str,
    ) -> ExternalServiceResult: ...

    def detect_seals(
        self,
        *,
        file_path: Path,
        filename: str,
        content_type: str,
    ) -> ExternalServiceResult: ...

    def submit_processed_document(self, *, file_path: Path, filename: str) -> OcrJobSubmission: ...

    def poll_ocr_job(
        self, *, job_id: str, deadline_at: datetime,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> ExternalServiceResult: ...


_PORT: DocumentServicePort | None = None


def register_document_service(port: DocumentServicePort) -> None:
    """注册供应商实现。应用启动时调用一次（见 `main`）。"""
    global _PORT
    _PORT = port


def document_service() -> DocumentServicePort:
    """取已注册的供应商文档服务；未注册时明确报 503，不回落成空实现。"""
    if _PORT is None:
        raise BusinessError(
            "EXTERNAL_CONFIG_MISSING",
            "外部文档服务尚未注册，无法执行文件处理",
            status_code=503,
            details={"missing": ["DocumentServicePort"]},
        )
    return _PORT
