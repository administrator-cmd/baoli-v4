"""上传文件及业务产物的存储路径、保存与读取。"""

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from fastapi import UploadFile

from app.common.exceptions import BusinessError
from app.core.config import get_settings

RULEBOOK_EXTENSIONS = {".xlsx", ".xlsm"}

# 规则库文件只接受 Excel；按扩展名给出真实的下载类型，未知扩展名按二进制流返回。
_RULEBOOK_MEDIA_TYPES = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".xls": "application/vnd.ms-excel",
}


def rulebook_media_type(path: Path) -> str:
    return _RULEBOOK_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


DOCUMENT_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}


def storage_entry(kind: str, number: str, created_at: datetime | None = None) -> str:
    stamp = (created_at or datetime.now(UTC)).astimezone(UTC)
    return f"{kind}_{stamp:%Y%m%dT%H%M%SZ}_{number}"


def audit_directory(task_no: str, created_at: datetime) -> str:
    return f"audit_tasks/{storage_entry('audit', task_no, created_at)}"


def resolve_storage_path(relative_path: str) -> Path:
    """把相对路径解析到存储根目录下；越出根目录时明确报错。"""
    root = get_settings().storage_root
    target = (root / relative_path).resolve()
    if root != target and root not in target.parents:
        raise BusinessError("STORAGE_PATH_INVALID", "文件存储路径不安全", status_code=500)
    return target


def save_upload_sync(
    upload: UploadFile,
    *,
    relative_path: str,
    allowed_extensions: set[str],
    max_bytes: int,
) -> tuple[int, str]:
    """把上传写入存储并返回（字节数, sha256）；扩展名不允许或超出上限时明确报错。"""
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in allowed_extensions:
        raise BusinessError(
            "FILE_TYPE_UNSUPPORTED",
            "文件格式不受支持",
            details={"filename": upload.filename or "", "allowed_extensions": sorted(allowed_extensions)},
        )
    target = resolve_storage_path(relative_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    total = 0
    try:
        with target.open("wb") as output:
            while chunk := upload.file.read(1024 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    raise BusinessError(
                        "FILE_TOO_LARGE",
                        "文件超过允许大小",
                        details={"max_bytes": max_bytes},
                    )
                digest.update(chunk)
                output.write(chunk)
    except Exception:
        if target.exists():
            target.unlink()
        raise
    if total == 0:
        target.unlink(missing_ok=True)
        raise BusinessError("FILE_EMPTY", "上传文件为空")
    return total, digest.hexdigest()
