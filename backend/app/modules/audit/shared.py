"""审核链路共享读取辅助与引文排版处理，不执行金额、日期或值与引文的业务核对。"""

import re
from typing import Any

from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.domain.audit import fact_projection
from app.domain.audit.execution_scope import pending_rerun_keys_of
from app.modules.audit.execution.model import NodeExecution
from app.modules.audit.task.model import AuditTask
from app.modules.material.document.crud import MaterialDocumentCRUD
from app.modules.material.document.model import DocumentUpload, TaskDocument
from app.modules.scenario.template.model import ScenarioVersion
from app.modules.scenario.template.service.crud import ScenarioTemplateCRUD

_OCR_LATEX_WRAP = re.compile(
    r"\\(?:underline|text|textbf|textit|textrm|mathrm|mathit|emph|mbox|hbox)\s*\{([^{}]*)\}"
)

_OCR_HTML_TAG = re.compile(r"<[^>]*>")

_OCR_MARK = re.compile(r"\*+")

_OCR_HEADING = re.compile(r"(?m)^\s*#+\s*")

def _verification_text(value: str) -> str:
    """用于「引文是否有原文依据」的比对文本：保留原始引文，只抵消排版差异。

    OCR 产物是有损文本：行内公式包裹、Markdown 粗体与标题标记、HTML 图片标签、空白与换行
    都是排版产物，不是内容差异——模型引用的可读文本与这些标记逐字不相等，但内容确实来自原文。

    本函数只消除排版差异，不删除文字、不做同义替换、不做相似度匹配：「内容是否有原文依据」
    仍按规范化后的连续子串判定，找不到就是找不到。原始引文按原样保存与展示。
    """
    text = value.replace("\u200b", "").replace("\ufeff", "")
    text = _OCR_HTML_TAG.sub("", text)
    previous = None
    while previous != text:
        previous = text
        text = _OCR_LATEX_WRAP.sub(r"\1", text)
    text = text.replace("$", "")
    text = _OCR_MARK.sub("", text)
    text = _OCR_HEADING.sub("", text)
    return " ".join(text.split())

def _locatable_text(value: str) -> str:
    """原文内容的比对文本：在 `_verification_text` 基础上再去掉空白。

    排版差异在 OCR 产物里会穿行：同一句话可能被拆成多行或被块边界打断，
    引文与产物文本的空白分布因此不同。内容核对按规范化后的连续子串匹配。
    """
    return "".join(_verification_text(value).split())


def _normalize_field_value(
    value_type: str, value: Any, *, unit: str | None = None, currency: str | None = None,
    extraction_strategy: str | None = None, structure_schema: dict[str, Any] | None = None,
) -> Any:
    """人工纠错使用同一取值类型转换；单位和币种不参与程序业务判断。"""
    if value is None:
        raise ValueError("字段缺少取值")
    if value_type == "BOOLEAN" and isinstance(value, str):
        choices = {"true": True, "false": False, "是": True, "否": False}
        if value.strip().lower() not in choices:
            raise ValueError("布尔字段必须是 true、false、是或否")
        value = choices[value.strip().lower()]
    try:
        return fact_projection.canonical_fact_value(value, {
            "value_type": value_type, "extraction_strategy": extraction_strategy,
            "structure_schema": structure_schema,
        })
    except BusinessError as exc:
        raise ValueError(exc.message) from exc


def _task_field_definitions(task: AuditTask) -> dict[str, dict[str, Any]]:
    return {item["field_key"]: item for item in task.task_schema_json["fields"]}

def _scenario(db: Session, task: AuditTask) -> ScenarioVersion:
    scenario = ScenarioTemplateCRUD(db).get_of(ScenarioVersion, task.scenario_version_id)
    if not scenario:
        raise BusinessError("SCENARIO_VERSION_MISSING", "任务关联的场景版本不存在", status_code=500)
    return scenario


def _task_manifest(task: AuditTask) -> dict[str, Any]:
    """任务创建时冻结的方案配置，是执行期的配置来源。"""
    manifest = task.manifest_json
    if not isinstance(manifest, dict) or not manifest:
        raise BusinessError("TASK_MANIFEST_MISSING", "任务缺少已冻结的执行配置", status_code=500)
    return manifest

def pending_rerun_keys(execution: NodeExecution) -> set[str]:
    """执行批次里尚未完成重跑的规则键；续跑与报告失效判断共用这一份归一化。"""
    return pending_rerun_keys_of(
        (execution.result_json or {}).get("pending_rerun_keys"), execution_id=execution.id,
    )


def current_upload(db: Session, document: TaskDocument) -> DocumentUpload | None:
    """材料位的当前上传版本；is_current 是唯一判定来源。"""
    return MaterialDocumentCRUD(db).current_upload(document.id)


def _task_documents(db: Session, task: AuditTask) -> list[TaskDocument]:
    """任务的全部材料位，按 id 升序。"""
    return MaterialDocumentCRUD(db).documents_for_task(task.id)


def _current_upload_map(db: Session, task: AuditTask) -> dict[int, DocumentUpload]:
    """任务下各材料位的当前上传版本，按材料位 id 索引。"""
    return MaterialDocumentCRUD(db).current_upload_map(task.id)


def _current_uploads(db: Session, task: AuditTask) -> list[tuple[TaskDocument, DocumentUpload | None]]:
    """材料位与各自的当前上传；未上传的材料位也在列表里，上传为 None。"""
    return MaterialDocumentCRUD(db).current_uploads(task.id)


def _material_entries(
    pairs: list[tuple[TaskDocument, DocumentUpload | None]], states: dict[str, str | None],
) -> list[dict[str, Any]]:
    """材料位与当前上传的只读视图，按材料位键排序；状态由调用方按同一份快照算出。"""
    return [
        {
            "material_key": document.material_key,
            "material_name": document.material_name,
            "required": bool(document.required),
            "document_status": document.status,
            "material_state": states.get(document.material_key),
            "upload_id": upload.id if upload else None,
            "upload_sha256": upload.sha256 if upload else None,
            "upload_revision": upload.upload_no if upload else None,
            "upload_filename": upload.original_filename if upload else None,
            "upload_content_type": upload.content_type if upload else None,
            "upload_size_bytes": upload.size_bytes if upload else None,
        }
        for document, upload in sorted(pairs, key=lambda item: item[0].material_key)
    ]

def _material_names(manifest: dict[str, Any]) -> dict[str, str]:
    return {item["material_key"]: item["name"] for item in manifest["materials"]}
