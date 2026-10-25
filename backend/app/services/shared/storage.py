"""上传文件及业务产物的存储路径、保存与读取。"""

import hashlib
import shutil
from pathlib import Path

from fastapi import UploadFile

from ...common.exceptions import BusinessError
from ...core.config import get_settings

RULEBOOK_EXTENSIONS = {".xlsx", ".xlsm"}


DOCUMENT_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}


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


def remove_storage_tree(relative_path: str) -> None:
    """删除存储根目录下的一组业务文件；路径越界或类型错误时明确失败。"""
    target = resolve_storage_path(relative_path)
    if not target.exists():
        return
    if not target.is_dir():
        raise BusinessError(
            "STORAGE_PATH_INVALID",
            "待清理的业务存储路径不是目录",
            status_code=500,
            details={"path": relative_path},
        )
    shutil.rmtree(target)
