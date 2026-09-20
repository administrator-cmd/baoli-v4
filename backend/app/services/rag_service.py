"""印章主体检索：用待核对主体查询相似印章主体。

范围与边界（本轮已收窄，与 docs/规则库与审核执行契约.md 一致）：

- **本模块只做印章主体检索**，不实现合同全文向量检索、条款检索、通用问答，
  也不做 LLM 主体裁决。条款语义审核仍走原有的字段事实 + LLM 校验路径。
- 索引只对印章识别产物中的**主体名称**建向量。页码、坐标、识别置信度、条目序号、
  印章类型不进入向量文本；原始产物按既有机制完整保留，不在本模块改写。
- 「只保留主体字段」不等于只保留第一枚印章：识别出的**不同主体都成为候选**，
  不自动合并、不按相似度择一。同一主体在多个印章条目中重复出现时，各条目分别保留。
- 检索先按**当前任务、材料范围、当前上传版本、产物内容版本、索引版本、模型与向量维度**
  过滤候选，再在候选内做余弦相似度排序；禁止跨任务、跨上传版本、跨模型检索，
  也禁止先全库检索再交给前端筛选。
- 相似度只用于候选排序；本模块不设置“超过某相似度即主体一致”的逻辑，
  也不自动把前几条候选标为人工已采纳。
- 索引失败、模型缺失、产物损坏都抛出明确错误，不返回空结果或默认成功。
- 每次检索保存查询内容、限定范围、模型/索引版本、候选与相似度；人工结论另行保存。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..config import get_settings
from ..errors import BusinessError, exception_summary
from ..models import (
    AuditTask,
    DocumentUpload,
    EvidenceChunk,
    EvidenceIndexVersion,
    EvidenceRetrieval,
    StoredArtifact,
    TaskDocument,
)
from .embedding_service import (
    cosine_similarity,
    embedding_runtime,
    index_version_of,
    vector_from_json,
    vector_norm,
    vector_to_json,
)
from ..domain.identity import public_no


logger = logging.getLogger(__name__)

# 索引状态。
INDEX_BUILDING = "BUILDING"
INDEX_READY = "READY"
INDEX_FAILED = "FAILED"
INDEX_INVALIDATED = "INVALIDATED"

# 页号来源：合成序号只表示产物内的顺序，不等于原 PDF 页码。
PAGE_NO_PROVIDER = "PROVIDER"
PAGE_NO_SYNTHETIC = "SYNTHETIC_SEQUENCE"
PAGE_NO_ABSENT = "ABSENT"

# 主体名称转成向量的唯一入口：文本必须非空且不超过模型实际长度。
# 超出模型长度时**报错**而不是截断：截断后的两个不同主体会在向量上更接近，
# 属于静默改变证据。真实主体名称远短于该上限，触发即说明输入不是主体名称。


# ---------------------------------------------------------------------------
# 建索引
# ---------------------------------------------------------------------------


def _artifact_content(artifact: StoredArtifact) -> dict[str, Any]:
    # 产物信封是内部契约（domain），不依赖具体供应商模块。
    from pydantic import ValidationError

    from ..domain.external_artifacts import ExternalArtifact

    try:
        return ExternalArtifact.model_validate(artifact.metadata_json).content
    except ValidationError as exc:
        raise BusinessError(
            "ARTIFACT_METADATA_INVALID",
            "文件处理产物的元数据不符合契约，无法建立索引",
            status_code=500,
            details={"artifact_no": artifact.artifact_no},
        ) from exc


def _seal_items(artifact: StoredArtifact) -> list[dict[str, Any]]:
    """印章识别产物的**全部**主体条目。

    每条记录都单独保留：不选择其中一枚后丢掉其余记录，也不把不同识别名合并成一个
    确定主体。主体名称来自产物中作为主体名称归一化的那个字段（见 integrations 层），
    本模块不猜字段含义。
    """
    content = _artifact_content(artifact)
    items = content.get("seal_subjects")
    if not isinstance(items, list):
        # 本次归一化之前写入的印章产物只有 pages[].seals[]，其中的 text 就是主体名称
        # （与供应商字段一致，旁证见 integrations 层的归一化说明）。这里按下标读取旧结构，
        # 让历史任务无需清库重建也能建主体索引；新旧结构都缺失才是明确的契约错误。
        legacy = _legacy_seal_subjects(content)
        if legacy is None:
            raise BusinessError(
                "ARTIFACT_CONTENT_INVALID",
                "印章识别产物缺少主体条目结构，无法建立主体索引",
                status_code=500,
                details={"artifact_no": artifact.artifact_no},
            )
        return legacy
    entries: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            raise BusinessError(
                "ARTIFACT_CONTENT_INVALID",
                "印章识别产物的主体条目结构不符合契约，无法建立主体索引",
                status_code=500,
                details={"artifact_no": artifact.artifact_no},
            )
        entries.append(item)
    return entries


def _legacy_seal_subjects(content: dict[str, Any]) -> list[dict[str, Any]] | None:
    """旧结构（pages[].seals[]）→ 主体条目；结构不存在时返回 None。

    仅在 seal_subjects 缺失时使用；它读的是同一份供应商字段，不涉及字段含义的猜测。
    """
    pages = content.get("pages")
    if not isinstance(pages, list):
        return None
    entries: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        seals = page.get("seals")
        if not isinstance(seals, list):
            continue
        for entry_no, seal in enumerate(seals, start=1):
            if not isinstance(seal, dict):
                continue
            entries.append(
                {
                    "entry_no": entry_no,
                    "page_no": page.get("page_no"),
                    "seal_type": seal.get("seal_type") or seal.get("class_name"),
                    "subject_name": seal.get("subject_name") or seal.get("text"),
                    "bbox": seal.get("bbox"),
                    "confidence": seal.get("confidence"),
                }
            )
    return entries


def _build_seal_subject_chunks(artifact: StoredArtifact) -> list[dict[str, Any]]:
    """印章主体 → 分块。**向量只由主体名称生成**。

    主体名称以外的信息（页码、坐标、置信度、条目序号、印章类型）一律不进入检索文本：
    它们会稀释名称相似度，也会让“主体名称相近”变成“位置或类型相近”。
    这些信息仍保留在原始产物与索引记录的定位字段里，可追溯但不参与相似度。
    """
    chunks: list[dict[str, Any]] = []
    for entry in _seal_items(artifact):
        subject_name = entry.get("subject_name")
        if not isinstance(subject_name, str) or not subject_name.strip():
            # 没有识别出主体名称的条目没有可检索的主体：跳过，但原始条目仍完整保留。
            continue
        normalized = subject_name.strip()
        chunks.append(
            {
                "text": normalized,
                "heading_path": None,
                "char_start": 0,
                "char_end": len(normalized),
                "page_no": entry.get("page_no") if isinstance(entry.get("page_no"), int) else None,
                "page_no_source": PAGE_NO_PROVIDER if isinstance(entry.get("page_no"), int) else PAGE_NO_ABSENT,
                # 保留它在原产物中的条目序号：可以回到原始条目，不只保存向量。
                "source_entry_no": entry.get("entry_no") if isinstance(entry.get("entry_no"), int) else None,
                "context_derived": False,
            }
        )
    return chunks


def _chunk_index_rows(runtime: Any, artifact: StoredArtifact) -> list[dict[str, Any]]:
    """索引的行来源：本轮只索引印章识别产物的主体名称。"""
    if artifact.artifact_type == "SEAL":
        return _build_seal_subject_chunks(artifact)
    return []

def _current_upload(db: Session, task: AuditTask, material_key: str) -> DocumentUpload | None:
    document = db.scalar(
        select(TaskDocument).where(
            TaskDocument.audit_task_id == task.id,
            TaskDocument.material_key == material_key,
        )
    )
    if document is None or document.current_upload_id is None:
        return None
    return db.get(DocumentUpload, document.current_upload_id)


def build_task_index(
    db: Session,
    task: AuditTask,
    *,
    material_keys: list[str] | None = None,
) -> dict[str, Any]:
    """为当前任务当前上传版本的**印章识别产物**建立主体索引。

    - 只处理**当前**上传版本的产物：旧上传版本的产物不进入当前索引（历史保留）；
    - 索引绑定**产物内容哈希**：同一份 PDF 重新识别后内容变化即产生新索引，
      旧索引不会继续冒充当前产物；
    - 批次全部成功后才把索引版本标为 READY；任一步失败则整批标为 FAILED 并记录原因，
      不留下“看起来可用”的半成品索引；
    - 重试是幂等的：同一 (上传, 产物内容, 索引版本) 已存在 READY 记录时直接复用，
      不会重复累积有效分块。
    """
    runtime = embedding_runtime()
    version_key = index_version_of(
        runtime.model_id,
        runtime.model_revision,
        runtime.dimension,
        get_settings().embedding_chunk_version,
    )
    created: list[str] = []
    reused: list[str] = []
    skipped: list[dict[str, str]] = []
    for document, upload in _current_task_uploads(db, task):
        if upload is None:
            continue
        if material_keys is not None and document.material_key not in material_keys:
            continue
        # 本轮只索引印章识别产物：主体核对需要的是印章主体，不是合同正文。
        artifacts = db.scalars(
            select(StoredArtifact).where(
                StoredArtifact.document_upload_id == upload.id,
                StoredArtifact.upload_sha256 == upload.sha256,
                StoredArtifact.status == "READY",
                StoredArtifact.artifact_type == "SEAL",
            )
        ).all()
        if not artifacts:
            skipped.append(
                {
                    "material_key": document.material_key,
                    "reason": "当前上传文件没有可用于主体检索的印章识别产物",
                }
            )
            continue
        for artifact in artifacts:
            if not artifact.content_sha256:
                # 产物内容版本未知时不能建索引：无法把索引与“当时的产物内容”绑定，
                # 产物重建后就无法判断旧索引是否仍然适用。
                raise BusinessError(
                    "ARTIFACT_CONTENT_HASH_MISSING",
                    "印章识别产物缺少内容哈希，无法建立可核对的主体索引",
                    status_code=500,
                    details={"artifact_no": artifact.artifact_no},
                )
            existing = db.scalar(
                select(EvidenceIndexVersion).where(
                    EvidenceIndexVersion.document_upload_id == upload.id,
                    EvidenceIndexVersion.artifact_type == artifact.artifact_type,
                    EvidenceIndexVersion.artifact_content_sha256 == artifact.content_sha256,
                    EvidenceIndexVersion.index_version == version_key,
                )
            )
            if existing is not None and existing.status == INDEX_READY:
                reused.append(existing.index_no)
                continue
            index = existing or EvidenceIndexVersion(
                index_no=public_no("IDX"),
                audit_task_id=task.id,
                document_upload_id=upload.id,
                artifact_id=artifact.id,
                artifact_type=artifact.artifact_type,
                artifact_sha256=artifact.upload_sha256,
                artifact_content_sha256=artifact.content_sha256,
                index_version=version_key,
                model_id=runtime.model_id,
                model_revision=runtime.model_revision,
                embedding_dim=runtime.dimension,
                chunk_version=get_settings().embedding_chunk_version,
                status=INDEX_BUILDING,
                chunk_count=0,
            )
            if existing is None:
                db.add(index)
                db.flush()
            if existing is not None:
                # 上一次构建失败后重试：清掉旧的失败分块，重新构建整批。
                db.execute(
                    EvidenceChunk.__table__.delete().where(EvidenceChunk.index_version_id == index.id)
                )
                db.flush()
            try:
                # 每个产物是一个索引批次：用保存点保证「整批成功才可见」。
                # 中途失败时该产物的分块全部回滚，已就绪的其它产物不受影响；
                # 失败原因写入索引记录，重试会重新构建整批，不会累积重复分块。
                with db.begin_nested():
                    rows = _chunk_index_rows(runtime, artifact)
                    chunks = _insert_chunks(db, task, runtime, index, rows)
                    index.status = INDEX_READY
                    index.chunk_count = chunks
                    index.failure_code = None
                    index.failure_message = None
                    index.built_at = datetime.now(UTC)
            except BusinessError as exc:
                index.status = INDEX_FAILED
                index.failure_code = exc.code
                index.failure_message = exc.message
                index.chunk_count = 0
                db.flush()
                raise
            except Exception as exc:  # noqa: BLE001 - 未预期失败同样必须让索引显式失败，不留 BUILDING
                index.status = INDEX_FAILED
                index.failure_code = "EVIDENCE_INDEX_BUILD_FAILED"
                index.failure_message = f"索引构建发生未预期错误：{exception_summary(exc)}"
                index.chunk_count = 0
                db.flush()
                raise
            db.flush()
            created.append(index.index_no)
    return {
        "index_version": version_key,
        "model_id": runtime.model_id,
        "model_revision": runtime.model_revision,
        "built": created,
        "reused": reused,
        "skipped": skipped,
    }


def _current_task_uploads(db: Session, task: AuditTask) -> list[tuple[TaskDocument, DocumentUpload | None]]:
    documents = db.scalars(
        select(TaskDocument).where(TaskDocument.audit_task_id == task.id).order_by(TaskDocument.id)
    ).all()
    return [
        (document, db.get(DocumentUpload, document.current_upload_id) if document.current_upload_id else None)
        for document in documents
    ]


def encode_texts(runtime: Any, texts: list[str]) -> list[list[Decimal]]:
    """把一段或多段文本编码成向量，并校验它们没有超过模型的实际长度。

    主体名称与查询文本必须走同一个入口、用同一份 tokenizer 计数：超长时**报错**，
    既不静默截断（截断会让不同主体更相似），也不改用字符数估算。
    返回向量的维度由 embedding 层校验，非法数值在那里直接报错。
    """
    if not texts:
        return []
    max_length = runtime.max_seq_length
    for text in texts:
        if not isinstance(text, str) or not text.strip():
            raise BusinessError("EMBEDDING_TEXT_EMPTY", "待编码文本为空，不能生成向量", status_code=500)
        token_count = len(runtime.tokenize(text).get("input_ids") or [])
        if max_length and token_count > max_length:
            raise BusinessError(
                "EMBEDDING_TEXT_TOO_LONG",
                f"待编码文本长度为 {token_count} 个 token，超过本地模型上限 {max_length}；"
                "本项不截断，请核对输入内容",
                status_code=400,
                details={"token_count": token_count, "max_seq_length": max_length},
            )
    vectors = runtime.encode(texts)
    if len(vectors) != len(texts):
        raise BusinessError(
            "EMBEDDING_VECTOR_COUNT_MISMATCH",
            "本地嵌入模型返回的向量条数与输入文本数不一致",
            status_code=503,
        )
    return vectors


def _insert_chunks(
    db: Session,
    task: AuditTask,
    runtime: Any,
    index: EvidenceIndexVersion,
    rows: list[dict[str, Any]],
) -> int:
    """把主体分块写入库：先校验长度，再批量嵌入，任一步失败整批不落库。"""
    prepared: list[tuple[int, dict[str, Any], str]] = []
    chunk_no = 0
    for row in rows:
        chunk_no += 1
        prepared.append((chunk_no, row, str(row["text"])))
    if not prepared:
        return 0
    vectors = encode_texts(runtime, [item[2] for item in prepared])
    for (number, row, chunk_text), vector in zip(prepared, vectors):
        norm = vector_norm(vector)
        db.add(
            EvidenceChunk(
                audit_task_id=task.id,
                index_version_id=index.id,
                chunk_no=number,
                chunk_text=chunk_text,
                context_derived=bool(row.get("context_derived")),
                heading_path=row.get("heading_path"),
                char_start=int(row["char_start"]),
                char_end=int(row["char_end"]),
                page_no=row.get("page_no"),
                page_no_source=row.get("page_no_source") or PAGE_NO_ABSENT,
                source_entry_no=row.get("source_entry_no"),
                embedding_json=vector_to_json(vector),
                embedding_norm=format(norm, "f"),
                # token 数用模型真实的 tokenizer 计，不按字符数估算。
                token_count=len(runtime.tokenize(chunk_text).get("input_ids") or []),
            )
        )
    db.flush()
    return len(prepared)


def invalidate_upload_indexes(
    db: Session,
    *,
    task_id: int,
    upload_id: int | None,
    reason: str,
) -> int:
    """上传版本被替换/移除或产物重建后，让不再适用的索引退出当前检索。

    只把状态改成 INVALIDATED 并记录原因，不物理删除：历史检索记录与旧分块仍可追溯。
    """
    conditions = [EvidenceIndexVersion.audit_task_id == task_id, EvidenceIndexVersion.status != INDEX_INVALIDATED]
    if upload_id is not None:
        conditions.append(EvidenceIndexVersion.document_upload_id == upload_id)
    result = db.execute(
        update(EvidenceIndexVersion).where(*conditions).values(
            status=INDEX_INVALIDATED,
            failure_code="INDEX_INVALIDATED",
            failure_message=reason,
        )
    )
    # 重试结果与旧上传版本的索引都要退出当前检索，任务需要重新建索引。
    db.flush()
    return int(result.rowcount or 0)


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------


def _scope_candidates(
    db: Session,
    task: AuditTask,
    *,
    material_keys: list[str],
    runtime: Any,
    version_key: str,
) -> tuple[list[EvidenceChunk], list[str], list[dict[str, str]], list[dict[str, str]]]:
    """按当前任务、材料范围、当前上传版本、产物内容、索引版本与模型过滤候选。

    过滤发生在向量排序**之前**；范围为空时不退化到全库检索。返回
    （分块, 可用索引号, 被排除的索引, 无法覆盖的材料及原因），后两项供页面说明
    “为什么没有证据”，不能把不同原因的缺失合并成一句“没有索引”。
    """
    requested = list(dict.fromkeys(material_keys))
    known = {document.material_key for document, _ in _current_task_uploads(db, task)}
    # 非法材料（不属于当前任务）必须明确报错：它不能被描述成“没有索引”，
    # 那会把调用方的范围错误伪装成检索结果为空。
    unknown = [key for key in requested if key not in known]
    if unknown:
        raise BusinessError(
            "RETRIEVAL_MATERIAL_UNKNOWN",
            "检索范围包含不属于当前任务的材料：" + "、".join(unknown),
            details={"task_no": task.task_no, "material_keys": unknown},
        )
    uploads: list[DocumentUpload] = []
    uncovered: list[dict[str, str]] = []
    excluded: list[dict[str, str]] = []
    for document, upload in _current_task_uploads(db, task):
        if document.material_key not in requested:
            continue
        if upload is None:
            uncovered.append({"material_key": document.material_key, "reason": "该材料尚未上传，没有可检索的产物"})
            continue
        uploads.append(upload)
    if not uploads:
        return [], [], excluded, uncovered
    indexes = db.scalars(
        select(EvidenceIndexVersion).where(
            EvidenceIndexVersion.audit_task_id == task.id,
            EvidenceIndexVersion.document_upload_id.in_([item.id for item in uploads]),
            # 只接受当前模型、当前索引版本：不同模型的向量不能混用，
            # 也不能拿旧模型的索引冒充当前模型的检索结果。
            EvidenceIndexVersion.model_id == runtime.model_id,
            EvidenceIndexVersion.model_revision == runtime.model_revision,
            EvidenceIndexVersion.embedding_dim == runtime.dimension,
            EvidenceIndexVersion.index_version == version_key,
            EvidenceIndexVersion.status == INDEX_READY,
        )
    ).all()
    usable: list[EvidenceIndexVersion] = []
    covered_uploads: set[int] = set()
    for index in indexes:
        artifact = db.get(StoredArtifact, index.artifact_id)
        if artifact is None:
            excluded_reason = "索引对应的印章识别产物已不存在"
        elif artifact.status != "READY":
            excluded_reason = "印章识别产物当前不可用，索引不再适用"
        elif artifact.content_sha256 != index.artifact_content_sha256:
            # 产物内容已变化（例如重新识别）：旧索引不得被当作当前产物的索引。
            excluded_reason = "印章识别产物已重新生成，索引不再适用于当前产物"
        else:
            upload = next((item for item in uploads if item.id == index.document_upload_id), None)
            if upload is None or upload.sha256 != index.artifact_sha256:
                excluded_reason = "上传版本已变化，索引不再适用于当前上传"
            else:
                usable.append(index)
                # chunk_count=0 的索引虽处于 READY，但没有任何可检索主体：
                # 它不能算“已建索引”，否则该材料既不会落进“尚未识别主体”，
                # 也不会落进“尚未建索引”，三种缺失原因全部表达不出来。
                if index.chunk_count:
                    covered_uploads.add(index.document_upload_id)
                continue
        excluded.append({"index_no": index.index_no, "reason": excluded_reason})
    # 有上传但没有可用索引的材料：分别说明是还没有主体、还是尚未建索引、还是索引失效。
    scoped_upload_ids = {item.id for item in uploads}
    for document, upload in _current_task_uploads(db, task):
        if upload is None or upload.id not in scoped_upload_ids or upload.id in covered_uploads:
            continue
        seal_artifacts = db.scalars(
            select(StoredArtifact).where(
                StoredArtifact.document_upload_id == upload.id,
                StoredArtifact.upload_sha256 == upload.sha256,
                StoredArtifact.artifact_type == "SEAL",
                StoredArtifact.status == "READY",
            )
        ).all()
        empty_indexes = [item for item in usable if item.document_upload_id == upload.id and not item.chunk_count]
        if empty_indexes:
            reason = "该材料的印章识别产物中没有识别出主体名称，索引存在但没有可检索的主体"
        elif not seal_artifacts:
            reason = "该材料的印章识别尚未完成，没有可检索的主体"
        elif all(not (item.content_sha256 or "") for item in seal_artifacts):
            reason = "印章识别产物缺少内容哈希，无法建立可核对的主体索引"
        else:
            reason = "该材料尚未建立主体索引，或现有索引已失效"
        uncovered.append({"material_key": document.material_key, "reason": reason})
    if not usable:
        return [], [], excluded, uncovered
    chunks = db.scalars(
        select(EvidenceChunk)
        .where(
            EvidenceChunk.audit_task_id == task.id,
            EvidenceChunk.index_version_id.in_(
                [item.id for item in usable if item.chunk_count]
            ),
        )
        .order_by(EvidenceChunk.index_version_id, EvidenceChunk.chunk_no)
    ).all()
    return list(chunks), [item.index_no for item in usable if item.chunk_count], excluded, uncovered


def retrieve_evidence(
    db: Session,
    task: AuditTask,
    *,
    query_text: str,
    material_keys: list[str],
    rule_key: str | None = None,
    node_code: str | None = None,
    top_k: int | None = None,
) -> dict[str, Any]:
    """用待核对主体在当前任务范围内查询相似印章主体，返回候选。

    只返回候选，不给出结论：相似度只用于排序，候选是否与待核对主体一致由人工确认。
    本函数不自动“采纳”任何候选——`adopted` 只表示本次实际送入后续步骤的正文条目，
    与人工结论是两件事，两者在记录里分开保存。
    """
    query = query_text.strip()
    if not query:
        raise BusinessError("RETRIEVAL_QUERY_EMPTY", "待核对主体为空，无法检索印章主体")
    if not material_keys:
        raise BusinessError("RETRIEVAL_SCOPE_EMPTY", "检索范围为空：必须明确本次检索覆盖哪些材料")
    settings = get_settings()
    runtime = embedding_runtime()
    version_key = index_version_of(
        runtime.model_id,
        runtime.model_revision,
        runtime.dimension,
        settings.embedding_chunk_version,
    )
    limit = top_k or settings.retrieval_top_k
    chunks, index_nos, excluded, uncovered = _scope_candidates(
        db,
        task,
        material_keys=material_keys,
        runtime=runtime,
        version_key=version_key,
    )
    scope = {
        "audit_task_id": task.id,
        "task_no": task.task_no,
        "material_keys": list(material_keys),
        "index_nos": index_nos,
        "excluded_indexes": excluded,
        "uncovered_materials": uncovered,
        "query_text": query,
        "index_version": version_key,
        "model_id": runtime.model_id,
        "model_revision": runtime.model_revision,
        "embedding_dim": runtime.dimension,
        "chunk_version": settings.embedding_chunk_version,
    }
    if not chunks:
        # 分别表达“没有识别主体 / 尚未建索引 / 索引失效”，不合并成一句“没有索引”。
        reasons = "；".join(
            f"{item['material_key']}：{item['reason']}" for item in uncovered
        ) or "本次范围内没有可用的主体索引"
        message = (
            f"本次范围内没有可用的印章主体索引（{reasons}）。"
            "这不等于合同不存在该主体，请先完成印章识别与主体索引后重试"
        )
        record = _save_retrieval(
            db,
            task,
            rule_key=rule_key,
            node_code=node_code,
            query_text=query,
            scope=scope,
            runtime=runtime,
            index_nos=index_nos,
            candidates=[],
            adopted=[],
            top_k=limit,
            status="NO_INDEX",
            message=message,
            index_version=version_key,
        )
        return {
            "status": "NO_INDEX",
            "message": message,
            "retrieval_no": record.retrieval_no,
            "candidates": [],
            "adopted": [],
            "uncovered_materials": uncovered,
            "excluded_indexes": excluded,
        }

    query_vector = encode_texts(runtime, [query])[0]
    query_norm = vector_norm(query_vector)
    scored: list[tuple[Decimal, EvidenceChunk]] = []
    for chunk in chunks:
        # 候选向量来自索引记录：维度、有限值与范数异常都在这里明确报错，
        # 不让它作为 0 分参与排序后混进候选列表。
        chunk_vector = vector_from_json(list(chunk.embedding_json))
        chunk_norm = Decimal(chunk.embedding_norm)
        score = cosine_similarity(
            query_vector,
            query_norm,
            chunk_vector,
            chunk_norm,
            left_label="待核对主体向量",
            right_label=f"索引 {chunk.index_version_id} 分块 {chunk.chunk_no} 的向量",
        )
        scored.append((score, chunk))
    scored.sort(key=lambda item: (-item[0], item[1].index_version_id, item[1].chunk_no))
    candidates = scored[:limit]
    upload_by_id = {item.id: item for item in _all_task_uploads(db, task)}
    document_by_upload = _document_by_upload(db, task)
    candidate_rows = [
        _candidate_row(db, chunk, score, upload_by_id, document_by_upload)
        for score, chunk in candidates
    ]
    message = (
        "已按当前任务与材料范围完成印章主体检索。相似度只用于候选排序，"
        "不构成主体一致的结论；请由工作人员确认一致、不一致或证据不足。"
    )
    record = _save_retrieval(
        db,
        task,
        rule_key=rule_key,
        node_code=node_code,
        query_text=query,
        scope=scope,
        runtime=runtime,
        index_nos=index_nos,
        candidates=candidate_rows,
        # 候选全部保留；这里不预先勾选任何一条，人工结论单独保存。
        adopted=[],
        top_k=limit,
        status="OK",
        message=message,
        index_version=version_key,
    )
    return {
        "status": "OK",
        "message": message,
        "retrieval_no": record.retrieval_no,
        "candidates": candidate_rows,
        "adopted": [],
        "uncovered_materials": uncovered,
        "excluded_indexes": excluded,
    }


def _all_task_uploads(db: Session, task: AuditTask) -> list[DocumentUpload]:
    return list(
        db.scalars(
            select(DocumentUpload)
            .join(TaskDocument, TaskDocument.id == DocumentUpload.task_document_id)
            .where(TaskDocument.audit_task_id == task.id)
        ).all()
    )


def _document_by_upload(db: Session, task: AuditTask) -> dict[int, TaskDocument]:
    rows = db.scalars(
        select(DocumentUpload).join(TaskDocument, TaskDocument.id == DocumentUpload.task_document_id).where(
            TaskDocument.audit_task_id == task.id
        )
    ).all()
    return {row.id: db.get(TaskDocument, row.task_document_id) for row in rows}


def _candidate_row(
    db: Session,
    chunk: EvidenceChunk,
    score: Decimal,
    upload_by_id: dict[int, DocumentUpload],
    document_by_upload: dict[int, TaskDocument],
) -> dict[str, Any]:
    """一条印章主体候选：主体名称、归属与候选身份。

    主体名称就是索引文本本身（向量只由它生成）。页码只作为归属定位保留在记录里，
    不在主体确认界面展示，也不参与相似度。
    """
    index = db.get(EvidenceIndexVersion, chunk.index_version_id)
    upload = upload_by_id.get(index.document_upload_id) if index else None
    document = document_by_upload.get(upload.id) if upload else None
    return {
        # 候选身份：用于人工选择与提交校验，不用相似度数值做身份。
        "chunk_no": chunk.chunk_no,
        "chunk_id": chunk.id,
        "index_no": index.index_no if index else None,
        "source_entry_no": chunk.source_entry_no,
        "subject_name": chunk.chunk_text,
        "material_key": document.material_key if document else None,
        "material_name": document.material_name if document else None,
        "upload_no": upload.upload_no if upload else None,
        "filename": upload.original_filename if upload else None,
        "similarity": format(score, "f"),
    }


def _save_retrieval(
    db: Session,
    task: AuditTask,
    *,
    rule_key: str | None,
    node_code: str | None,
    query_text: str,
    scope: dict[str, Any],
    runtime: Any,
    index_nos: list[str],
    candidates: list[dict[str, Any]],
    adopted: list[dict[str, Any]],
    top_k: int,
    status: str,
    message: str,
    index_version: str,
) -> EvidenceRetrieval:
    record = EvidenceRetrieval(
        retrieval_no=public_no("RTV"),
        audit_task_id=task.id,
        scenario_version_id=task.scenario_version_id,
        rule_key=rule_key,
        node_code=node_code,
        query_text=query_text,
        scope_json=scope,
        model_id=runtime.model_id,
        model_revision=runtime.model_revision,
        embedding_dim=runtime.dimension,
        chunk_version=get_settings().embedding_chunk_version,
        index_version=index_version,
        index_versions=index_nos,
        candidates_json=candidates,
        adopted_json=adopted,
        top_k=top_k,
        status=status,
        message=message,
        input_revision=task.input_revision,
    )
    db.add(record)
    db.flush()
    return record


# 人工对候选的结论取值。
DECISION_CONSISTENT = "CONSISTENT"
DECISION_INCONSISTENT = "INCONSISTENT"
DECISION_INSUFFICIENT = "EVIDENCE_INSUFFICIENT"
HUMAN_DECISIONS = {DECISION_CONSISTENT, DECISION_INCONSISTENT, DECISION_INSUFFICIENT}


def record_human_decision(
    db: Session,
    task: AuditTask,
    *,
    retrieval_no: str,
    decision: str,
    note: str | None,
    selected: dict[str, Any] | None,
    expected_input_revision: int | None,
    decided_by: str | None = None,
) -> EvidenceRetrieval:
    """保存工作人员对本次检索候选的结论。

    - 只有 CONSISTENT / INCONSISTENT / EVIDENCE_INSUFFICIENT 三种取值，
      **不接受“自动采纳”**：相似度不能代替审核结论。
    - 结论随任务输入修订保存：上传替换、识别重跑或人工改值之后，
      旧结论不会套用到新的候选上。
    """
    if decision not in HUMAN_DECISIONS:
        raise BusinessError(
            "RETRIEVAL_DECISION_INVALID",
            "主体核对结论只能是“一致”“不一致”或“证据不足”",
            details={"decision": decision, "allowed": sorted(HUMAN_DECISIONS)},
        )
    record = db.scalar(
        select(EvidenceRetrieval).where(
            EvidenceRetrieval.audit_task_id == task.id,
            EvidenceRetrieval.retrieval_no == retrieval_no,
        )
    )
    if record is None:
        raise BusinessError(
            "RETRIEVAL_NOT_FOUND",
            "要提交结论的检索记录不存在或不属于当前任务",
            status_code=404,
            details={"retrieval_no": retrieval_no},
        )
    if record.status != "OK":
        raise BusinessError(
            "RETRIEVAL_NOT_DECIDABLE",
            "本次检索没有可用候选，不能提交主体核对结论",
            details={"retrieval_no": retrieval_no, "status": record.status},
        )
    if expected_input_revision is not None and int(expected_input_revision) != task.input_revision:
        raise BusinessError(
            "RETRIEVAL_INPUT_REVISION_CONFLICT",
            f"任务输入已变化（当前修订 {task.input_revision}），本次候选已过期，请重新检索后再提交结论",
            status_code=409,
            details={"expected": int(expected_input_revision), "actual": task.input_revision},
        )
    # 只比较客户端提交的修订不够：候选本身也有自己的事实版本（生成时任务的输入修订）、
    # 查询字段的事实版本、当前上传与索引/模型版本。记录一旦不再对应当前状态，
    # 旧候选就不能被记成当前结论。
    _assert_retrieval_still_current(db, task, record)
    # 选择必须落在本次真实候选上：不接受一个从未返回过的候选身份。
    if decision in {DECISION_CONSISTENT, DECISION_INCONSISTENT}:
        if not isinstance(selected, dict) or not selected:
            raise BusinessError(
                "RETRIEVAL_SELECTION_REQUIRED",
                "确认一致或不一致时必须指明所依据的候选",
                details={"retrieval_no": retrieval_no},
            )
        candidate_keys = {
            _candidate_identity(item) for item in (record.candidates_json or [])
        }
        if _candidate_identity(selected) not in candidate_keys:
            raise BusinessError(
                "RETRIEVAL_SELECTION_UNKNOWN",
                "所选择的候选不在本次检索返回的候选内",
                details={
                    "retrieval_no": retrieval_no,
                    "selected": _candidate_identity(selected),
                },
            )
    record.human_decision = decision
    record.human_decision_note = (note or "").strip() or None
    record.human_selected = selected if decision != DECISION_INSUFFICIENT else None
    record.human_decided_at = datetime.now(UTC)
    record.human_decided_by = decided_by
    record.decision_input_revision = task.input_revision
    db.flush()
    return record


def _assert_retrieval_still_current(db: Session, task: AuditTask, record: EvidenceRetrieval) -> None:
    """人工结论的有效性校验：检索记录自身与它依赖的真实状态都必须仍然成立。

    只比较客户端提交的当前修订，会把“检索发生在输入变化之前”的旧候选记成当前结论。
    这里逐项核对：

      - 检索时的任务输入修订 = 记录生成时的输入修订（记录自身一致）；
      - 当前任务的输入修订与检索时一致（候选未因替换/补传材料而过期）；
      - 检索时的模型与索引版本仍与配置一致；
      - 记录里引用的上传与索引仍然存在且仍对应当前上传。
    """
    settings = get_settings()
    if record.input_revision != task.input_revision:
        raise BusinessError(
            "RETRIEVAL_RECORD_STALE",
            f"本次检索基于任务输入修订 {record.input_revision}，当前已是 {task.input_revision}，"
            "候选已过期，请重新检索后再提交结论",
            status_code=409,
            details={"retrieval_no": record.retrieval_no, "record_revision": record.input_revision, "task_revision": task.input_revision},
        )
    runtime = embedding_runtime()
    current_version = index_version_of(
        runtime.model_id,
        runtime.model_revision,
        runtime.dimension,
        settings.embedding_chunk_version,
    )
    if record.index_version != current_version:
        raise BusinessError(
            "RETRIEVAL_INDEX_VERSION_STALE",
            "本次检索使用的索引版本与当前模型/切分配置不一致，候选已不再适用，请重新检索",
            status_code=409,
            details={"retrieval_no": record.retrieval_no, "record_version": record.index_version, "current_version": current_version},
        )
    index_nos = list(record.index_versions or [])
    if not index_nos:
        raise BusinessError(
            "RETRIEVAL_INDEX_MISSING",
            "本次检索记录没有可核对的索引号，无法确认候选仍然有效",
            status_code=409,
            details={"retrieval_no": record.retrieval_no},
        )
    current_upload_ids = {item.id for _, item in _current_task_uploads(db, task) if item is not None}
    indexes = db.scalars(
        select(EvidenceIndexVersion).where(
            EvidenceIndexVersion.audit_task_id == task.id,
            EvidenceIndexVersion.index_no.in_(index_nos),
        )
    ).all()
    if len(indexes) != len(set(index_nos)):
        raise BusinessError(
            "RETRIEVAL_INDEX_MISSING",
            "本次检索使用的索引已不存在，候选已过期，请重新检索",
            status_code=409,
            details={"retrieval_no": record.retrieval_no, "index_nos": index_nos},
        )
    for index in indexes:
        if index.status != INDEX_READY:
            raise BusinessError(
                "RETRIEVAL_INDEX_INVALIDATED",
                "本次检索使用的索引已失效，候选已过期，请重新检索",
                status_code=409,
                details={"retrieval_no": record.retrieval_no, "index_no": index.index_no, "status": index.status},
            )
        if index.document_upload_id not in current_upload_ids:
            raise BusinessError(
                "RETRIEVAL_UPLOAD_CHANGED",
                "本次检索使用的材料上传版本已被替换或移除，候选已过期，请重新检索",
                status_code=409,
                details={"retrieval_no": record.retrieval_no, "index_no": index.index_no},
            )
        artifact = db.get(StoredArtifact, index.artifact_id)
        if artifact is None or artifact.content_sha256 != index.artifact_content_sha256:
            raise BusinessError(
                "RETRIEVAL_ARTIFACT_CHANGED",
                "印章识别产物已重新生成，本次检索候选不再适用于当前产物，请重新检索",
                status_code=409,
                details={"retrieval_no": record.retrieval_no, "index_no": index.index_no},
            )


def retrieval_evidence(db: Session, task: AuditTask, retrieval_no: str) -> dict[str, Any]:
    """取回某条检索记录的真实候选与双方原文，供人工复核与页面展示。

    内容是本次检索**自己保存的**记录，不是重新检索、也不是重新推断：
    复核快照与页面必须能看到系统当时查到了什么，才能说明人工结论依据的是哪一条。
    """
    record = db.scalar(
        select(EvidenceRetrieval).where(
            EvidenceRetrieval.audit_task_id == task.id,
            EvidenceRetrieval.retrieval_no == retrieval_no,
        )
    )
    if record is None:
        return {}
    candidates = [item for item in (record.candidates_json or []) if isinstance(item, dict)]
    scope = record.scope_json if isinstance(record.scope_json, dict) else {}
    return {
        "retrieval_no": record.retrieval_no,
        "rule_key": record.rule_key,
        "node_code": record.node_code,
        "status": record.status,
        "message": record.message,
        # 待核对主体原文：人工要同时看到查询对象与候选对象才能判断是否一致。
        "query_text": record.query_text,
        "scope": scope,
        "index_versions": list(record.index_versions or []),
        "model_id": record.model_id,
        "model_revision": record.model_revision,
        "index_version": record.index_version,
        "input_revision": record.input_revision,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "human_decision": record.human_decision,
        "human_decision_note": record.human_decision_note,
        "human_selected": record.human_selected,
        "human_decided_at": record.human_decided_at,
        "human_decided_by": record.human_decided_by,
        "decision_input_revision": record.decision_input_revision,
    }


def _candidate_identity(item: dict[str, Any]) -> tuple[Any, Any, Any]:
    """候选身份：索引号 + 分块号 + 产物条目序号。

    用它校验人工选择确实来自本次返回的候选，不用相似度数值或材料名做身份。
    """
    return (item.get("index_no"), item.get("chunk_no"), item.get("source_entry_no"))


def retrieval_scope_hash(payload: dict[str, Any]) -> str:
    """检索范围的规范化哈希：任务、材料、上传版本与索引版本共同决定它。"""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 只读视图：索引状态与检索历史
# ---------------------------------------------------------------------------


def index_status_view(db: Session, task: AuditTask) -> dict[str, Any]:
    """当前任务各上传版本的索引状态；只读，不触发建索引。"""
    settings = get_settings()
    uploads = {item.id: item for item in _all_task_uploads(db, task)}
    documents = _document_by_upload(db, task)
    indexes = db.scalars(
        select(EvidenceIndexVersion)
        .where(EvidenceIndexVersion.audit_task_id == task.id)
        .order_by(EvidenceIndexVersion.id)
    ).all()
    items: list[dict[str, Any]] = []
    for index in indexes:
        upload = uploads.get(index.document_upload_id)
        document = documents.get(upload.id) if upload else None
        artifact = db.get(StoredArtifact, index.artifact_id)
        # 索引是否仍适用于当前产物：内容哈希一旦变化，旧索引就只是历史记录。
        current = (
            artifact is not None
            and artifact.status == "READY"
            and bool(artifact.content_sha256)
            and artifact.content_sha256 == index.artifact_content_sha256
        )
        items.append(
            {
                "index_no": index.index_no,
                "artifact_type": index.artifact_type,
                "status": index.status,
                "current_for_artifact": current,
                "chunk_count": index.chunk_count,
                "model_id": index.model_id,
                "model_revision": index.model_revision,
                "embedding_dim": index.embedding_dim,
                "chunk_version": index.chunk_version,
                "index_version": index.index_version,
                "document_upload_no": upload.upload_no if upload else None,
                "filename": upload.original_filename if upload else None,
                "material_name": document.material_name if document else None,
                "built_at": index.built_at,
                "failure_code": index.failure_code,
                "failure_message": index.failure_message,
            }
        )
    return {
        "task_no": task.task_no,
        # 没有索引记录时不编造一个“当前版本”，只显示配置的模型与切分版本。
        "index_version": items[0]["index_version"] if items else None,
        "model_id": items[0]["model_id"] if items else settings.embedding_model_id,
        "model_revision": items[0]["model_revision"] if items else settings.embedding_model_revision,
        "chunk_version": settings.embedding_chunk_version,
        "items": items,
        # “已建索引”只统计仍然适用于当前产物的索引：失效索引不算已建。
        "indexed": any(item["current_for_artifact"] and item["status"] == INDEX_READY for item in items),
    }


def retrieval_history_view(db: Session, task: AuditTask, *, limit: int = 50) -> dict[str, Any]:
    """本任务的检索记录，供页面说明“系统查过什么、用了哪些材料”。"""
    records = db.scalars(
        select(EvidenceRetrieval)
        .where(EvidenceRetrieval.audit_task_id == task.id)
        .order_by(EvidenceRetrieval.id.desc())
        .limit(limit)
    ).all()
    return {
        "task_no": task.task_no,
        "items": [
            {
                "retrieval_no": record.retrieval_no,
                "rule_key": record.rule_key,
                "node_code": record.node_code,
                "query_text": record.query_text,
                "scope": record.scope_json,
                "model_id": record.model_id,
                "model_revision": record.model_revision,
                "index_versions": record.index_versions,
                "status": record.status,
                "message": record.message,
                "top_k": record.top_k,
                "candidate_count": len(record.candidates_json or []),
                # 候选与人工结论分开返回：adopted 只是本次送入后续步骤的条目，
                # 不代表工作人员已经确认过其中任何一条。
                "candidates": record.candidates_json or [],
                "adopted": record.adopted_json or [],
                "human_decision": record.human_decision,
                "human_decision_note": record.human_decision_note,
                "human_selected": record.human_selected,
                "human_decided_at": record.human_decided_at,
                "human_decided_by": record.human_decided_by,
                "decision_input_revision": record.decision_input_revision,
                "index_version": record.index_version,
                "embedding_dim": record.embedding_dim,
                "input_revision": record.input_revision,
                "created_at": record.created_at,
            }
            for record in records
        ],
    }
