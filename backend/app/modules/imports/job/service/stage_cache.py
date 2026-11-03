"""第一轮规则库目录的检查点：第二轮失败后可以只重跑第二轮。

检查点按作业编号固定存放，同时记录两级指纹：
- 输入指纹：源文件哈希、场景、schema 与第一轮请求契约，可在不读工作簿时核对；
- 完整指纹：输入指纹再加第一轮实际请求体，用于真正复用结果。
任一项变化都不再复用，避免把旧契约生成的目录当作当前结果。
"""

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.services.shared.storage import resolve_storage_path

logger = logging.getLogger(__name__)


class ImportsJobStageCacheService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def initial_relative_path(self, job_no: str) -> str:
        return f"imports/{job_no}/llm/initial.json"


    def build_fingerprint(self, *parts: str) -> str:
        """输入指纹：各项以不可出现在业务文本中的分隔符拼接后取哈希。"""
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


    def save_initial(self, job_no: str, fingerprint: str, input_fingerprint: str, inventory: dict[str, Any],
    ) -> None:
        """原子写入第一轮结果；失败由调用方记录，不影响本轮转换结果。

        作业目录已不存在（解析过程中被删除）时不再写检查点，避免删除后重建孤儿目录。
        模型线程可能在删除提交后返回，这一步只做本地文件判断，不访问数据库。
        """
        target = resolve_storage_path(self.initial_relative_path(job_no))
        if not target.parents[1].is_dir():
            logger.warning("导入作业目录已不存在，跳过保存第一轮检查点 job_no=%s", job_no)
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as output:
                json.dump(
                    {"fingerprint": fingerprint, "input_fingerprint": input_fingerprint, "inventory": inventory},
                    output, ensure_ascii=False,
                )
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)


    def _read_checkpoint(self, job_no: str) -> dict[str, Any] | None:
        path: Path = resolve_storage_path(self.initial_relative_path(job_no))
        if not path.is_file():
            return None
        try:
            with path.open(encoding="utf-8") as content:
                payload = json.load(content)
        except (OSError, ValueError):
            logger.warning("第一轮解析检查点无法读取，按不可复用处理 job_no=%s", job_no, exc_info=True)
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("inventory"), dict):
            return None
        return payload


    def load_initial(self, job_no: str, fingerprint: str) -> dict[str, Any] | None:
        """读取可复用的第一轮结果；缺失、损坏或完整指纹不符时返回 None。"""
        payload = self._read_checkpoint(job_no)
        if payload is None or payload.get("fingerprint") != fingerprint:
            return None
        return payload["inventory"]


    def initial_stage_available(self, job_no: str, input_fingerprint: str) -> bool:
        """输入层面可复用时返回 True；实际复用还要求完整指纹一致。"""
        payload = self._read_checkpoint(job_no)
        return payload is not None and payload.get("input_fingerprint") == input_fingerprint
