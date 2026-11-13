"""处理产物的内容读写、哈希与上传版本定位；不提交事务。"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.domain.external_artifacts import ExternalArtifact, ExternalServiceResult, OcrContent
from app.modules.audit.artifact.crud import AuditArtifactCRUD
from app.modules.audit.artifact.model import StoredArtifact
from app.modules.audit.task.model import AuditTask
from app.modules.material.document.model import DocumentUpload
from app.services.shared.storage import resolve_storage_path


class AuditArtifactArtifactsService:
    """读写真实产物及其内容，不管理数据库提交。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def _artifact_for_upload(self, upload: DocumentUpload, service_code: str) -> StoredArtifact | None:
        return AuditArtifactCRUD(self.db).by_upload_and_type(upload.id, service_code)


    def _artifact_content_path(self, artifact: StoredArtifact) -> Path:
        """产物信封的落盘位置：与 relative_path 同目录、按产物类型命名的 JSON。"""
        return Path(artifact.relative_path).parent / f"{artifact.artifact_type.lower()}.json"


    def _artifact_payload(self, artifact: StoredArtifact) -> ExternalArtifact:
        """读取磁盘上的产物信封；文件缺失或结构不符时明确报错。"""
        path = resolve_storage_path(str(self._artifact_content_path(artifact)))
        if not path.is_file():
            raise BusinessError(
                "ARTIFACT_CONTENT_MISSING",
                "文件处理产物的内容文件不存在，需要重新执行该材料的处理",
                status_code=500,
                details={"artifact_no": artifact.artifact_no, "relative_path": str(path)},
            )
        try:
            with path.open(encoding="utf-8") as content:
                payload = ExternalArtifact.model_validate(json.load(content))
                if artifact.artifact_type == "OCR":
                    OcrContent.model_validate(payload.content)
        except (OSError, ValueError, ValidationError) as exc:
            raise BusinessError(
                "ARTIFACT_METADATA_INVALID",
                "文件处理产物内容不符合内部契约",
                status_code=500,
                details={"artifact_no": artifact.artifact_no},
            ) from exc
        if payload.status != "SUCCESS":
            raise BusinessError(
                "ARTIFACT_STATUS_INVALID",
                "当前文件处理产物不是成功结果",
                status_code=500,
                details={"artifact_no": artifact.artifact_no},
            )
        return payload


    def _artifact_content_digest(self, content: Any, binary: bytes | None) -> str:
        """产物内容哈希：JSON 产物按规范序列化的字节，二进制产物按真实文件字节。

        它与上传文件的 sha256 是两件事：同一份上传文件重新识别后上传哈希不变，
        产物内容却可能不同。内容哈希用于判断产物是否更新。
        """
        if binary is not None:
            return hashlib.sha256(binary).hexdigest()
        canonical = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


    def _persist_external_artifact(self, task: AuditTask,
        upload: DocumentUpload,
        service_code: str,
        result: ExternalServiceResult,
    ) -> tuple[str, ExternalArtifact]:
        """写产物信封；二进制产物（8010）另写二进制文件，relative_path 指向二进制。

        信封与二进制同目录，`_artifact_payload` 按产物类型定位信封，不依赖数据库 JSON。
        """
        artifact = result.artifact
        if service_code == "OCR":
            OcrContent.model_validate(artifact.content)
        directory = resolve_storage_path(f"tasks/{task.task_no}/artifacts/{upload.upload_no}")
        directory.mkdir(parents=True, exist_ok=True)
        envelope = directory / f"{service_code.lower()}.json"
        with envelope.open("w", encoding="utf-8") as output:
            json.dump(artifact.model_dump(mode="json"), output, ensure_ascii=False)
        if result.binary_content is None:
            if service_code == "DOCUMENT_PARSE":
                raise BusinessError("ARTIFACT_BINARY_MISSING", "文件处理服务未返回可用的去章文件", status_code=500)
            return envelope.relative_to(resolve_storage_path(".")).as_posix(), artifact
        if service_code != "DOCUMENT_PARSE":
            raise BusinessError("ARTIFACT_BINARY_TYPE_INVALID", "非文件处理服务不能保存二进制产物", status_code=500)
        suffix = Path(result.filename or upload.original_filename).suffix.lower() or ".bin"
        binary = directory / f"document_parse{suffix}"
        with binary.open("wb") as output:
            output.write(result.binary_content)
        return str(binary.relative_to(resolve_storage_path(".")).as_posix()), artifact


    def _restore_job_columns(self, record: StoredArtifact, attempt: dict[str, Any]) -> None:
        """提交前失败时恢复原作业号、截止时间与失败信息。"""
        record.external_job_id = attempt.get("previous_external_job_id")
        previous_deadline = attempt.get("previous_deadline_at")
        if isinstance(previous_deadline, str):
            parsed = datetime.fromisoformat(previous_deadline)
            # 历史 metadata 里可能是不带时区的 UTC 文本；数据库列统一按 UTC 存储。
            record.deadline_at = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        else:
            record.deadline_at = None
        record.failure_code = attempt.get("previous_failure_code")
        record.failure_message = attempt.get("previous_failure_message")


    def _ocr_job_metadata(self, artifact: StoredArtifact) -> tuple[str, datetime]:
        if artifact.status != "PENDING":
            raise BusinessError("OCR_JOB_METADATA_INVALID", "当前上传文件的 OCR 作业状态无效", status_code=500)
        job_id = artifact.external_job_id
        deadline_at = artifact.deadline_at
        if not isinstance(job_id, str) or not job_id.strip() or deadline_at is None:
            raise BusinessError("OCR_JOB_METADATA_INVALID", "当前上传文件的 OCR 作业记录不完整", status_code=500)
        if deadline_at.tzinfo is None:
            raise BusinessError("OCR_JOB_METADATA_INVALID", "当前上传文件的 OCR 作业截止时间缺少时区", status_code=500)
        return job_id.strip(), deadline_at


    def _processed_document_source(self, artifact: StoredArtifact) -> tuple[Path, str]:
        payload = self._artifact_payload(artifact)
        filename = payload.content.get("processed_filename")
        if not isinstance(filename, str) or not filename:
            raise BusinessError(
                "PROCESSED_ARTIFACT_MISSING",
                "当前上传文件缺少可用的 8010 处理产物，无法执行 OCR",
                status_code=409,
            )
        path = resolve_storage_path(artifact.relative_path)
        if not path.is_file():
            raise BusinessError(
                "PROCESSED_ARTIFACT_MISSING",
                "当前上传文件的 8010 处理产物不存在，无法执行 OCR",
                status_code=409,
            )
        return path, filename


    def material_classifications(self, upload_ids: list[int]) -> dict[int, dict[str, Any]]:
        """批量读取已就绪的 8018 识别结果，缺失或未就绪的上传不进入映射。

        8018 已改为只记录不判定：识别结论与置信度只是展示用的事实，不改变材料状态、
        不挂人工复核、不影响任何分支。因此这里既不按 `matches_declared_material` 写材料状态，
        也不据它生成待办——材料类型不符要由人按识别结论另行裁决，不由自动识别代替。
        """
        results = {}
        for artifact in AuditArtifactCRUD(self.db).by_uploads_and_type(upload_ids, "MATERIAL_CLASSIFY"):
            if artifact.status != "READY":
                continue
            payload = self._artifact_payload(artifact)
            if not isinstance(payload.content.get("matches_declared_material"), bool):
                raise BusinessError("MATERIAL_CLASSIFY_CONTRACT_INVALID", "材料识别服务未返回类型匹配结论")
            results[artifact.document_upload_id] = payload.content
        return results
