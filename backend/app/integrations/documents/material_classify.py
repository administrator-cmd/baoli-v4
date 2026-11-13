"""8018 材料识别：类型目录与上传校验。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.common.exceptions import BusinessError
from app.core.config import get_settings
from app.domain.materials.external_artifacts import ExternalArtifact, ExternalServiceResult
from app.integrations.documents.http import (
    client,
    confidence,
    contract_error,
    json_object,
    non_empty_string,
    request,
    required_service_url,
    strict_bool,
)

SERVICE_CODE = "MATERIAL_CLASSIFY"


def list_material_types() -> list[dict[str, Any]]:
    """读取 8018 支持的材料类型目录。

    材料识别类型必须从该目录选择，不能由业务自由输入：8018 的 doc_type 是严格枚举，
    自由输入的值会在执行文件处理时被服务端直接拒绝，问题要到运行时才暴露。

    目录是外部服务的事实，本函数不缓存、不内置任何默认清单，也不把
    8018 的取值与业务材料名称做映射。
    """
    settings = get_settings()
    base_url = required_service_url(settings.service_8018_url, SERVICE_CODE, "BAOLI_SERVICE_8018_URL")
    with client() as http:
        catalog_response = request(
            http, "GET", f"{base_url}/supported-types",
            service_code=SERVICE_CODE, operation="LIST_TYPES",
        )
    catalog = json_object(catalog_response, SERVICE_CODE, "LIST_TYPES")
    rows = catalog.get("types")
    if not isinstance(rows, list) or not rows:
        raise contract_error(SERVICE_CODE, "LIST_TYPES")
    items: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("id") is None:
            raise contract_error(SERVICE_CODE, "LIST_TYPES")
        try:
            items.append({"id": row["id"], "name": non_empty_string(row["name"])})
        except (KeyError, TypeError, ValueError) as exc:
            raise contract_error(SERVICE_CODE, "LIST_TYPES") from exc
    return items


def validate_material_type(
    *,
    file_path: Path,
    filename: str,
    content_type: str,
    material_type: str,
) -> ExternalServiceResult:
    """先核对 8018 的类型目录，再用它校验一份原始上传。"""
    settings = get_settings()
    base_url = required_service_url(settings.service_8018_url, SERVICE_CODE, "BAOLI_SERVICE_8018_URL")
    doc_type = material_type.strip()
    if not doc_type:
        raise BusinessError(
            "MATERIAL_TYPE_MISSING",
            "当前材料位未声明 8018 材料类型，无法执行材料校验",
            status_code=409,
        )
    with client() as http:
        try:
            catalog_response = request(
                http, "GET", f"{base_url}/supported-types",
                service_code=SERVICE_CODE, operation="LIST_TYPES",
            )
            supported_names = _supported_type_names(json_object(catalog_response, SERVICE_CODE, "LIST_TYPES"))
        except BusinessError as exc:
            # 类型目录查询是只读请求，/validate 尚未调用；本次产物占位可以安全撤销。
            raise BusinessError(
                "MATERIAL_TYPE_CATALOG_FAILED",
                f"8018 材料类型目录查询失败：{exc.message}",
                status_code=exc.status_code,
                details={"cause_code": exc.code, **(exc.details or {})},
            ) from exc
        if doc_type not in supported_names:
            raise BusinessError(
                "MATERIAL_TYPE_UNSUPPORTED",
                "当前材料类型不在 8018 支持目录中，无法执行材料校验",
                status_code=409,
                details={"service": SERVICE_CODE, "material_type": doc_type},
            )
        try:
            with file_path.open("rb") as input_file:
                response = request(
                    http, "POST", f"{base_url}/validate",
                    service_code=SERVICE_CODE, operation="VALIDATE",
                    files={"file": (filename, input_file, content_type)},
                    data={"doc_type": doc_type},
                )
        except OSError as exc:
            raise BusinessError(
                "DOCUMENT_SOURCE_MISSING",
                "当前上传文件无法读取，不能执行材料校验",
                status_code=500,
            ) from exc
    payload = json_object(response, SERVICE_CODE, "VALIDATE")
    try:
        success = strict_bool(payload["success"])
        matched = strict_bool(payload["is_valid"])
        score = confidence(payload["confidence"])
        matched_keywords = payload["matched_keywords"]
        provider_error = payload["error"]
        if not isinstance(matched_keywords, list) or not isinstance(provider_error, str):
            raise TypeError
        # 关键词原文（形如「合同(+4分)」）要原样保留：它是人工判断"文件是否放错材料位"
        # 最直接的依据，只留个计数等于把判据丢掉。
        matched_keyword_list = [str(item) for item in matched_keywords if isinstance(item, str)]
        # doc_type 在服务无法处理文件时真实返回空串（以不可读文件实测确认），
        # 因此按普通字符串读取；它只用于记录，不参与下面的分支判断。
        detected_type = str(payload["doc_type"] or "")
    except (KeyError, TypeError, ValueError) as exc:
        raise contract_error(SERVICE_CODE, "VALIDATE") from exc
    # 8018 的 success 与 is_valid 实测恒同真同假，区分不了「服务处理不了文件」与
    # 「文件不匹配声明的材料类型」。真正的判别字段是 error（两者均已实测）：
    #   error 非空（原文「无法提取文本内容」）→ 服务未能完成识别，属于调用失败；
    #   error 为空且 is_valid=false      → 识别成功但文件不匹配该材料类型。
    # 后者是可裁决的业务结论：落成 matches_declared_material=false 随产物保存，由业务按
    # 识别结论另行裁决（8018 只记录不判定，见 services/audit/artifacts.material_classify_content）。
    # 按 success 一律抛错会把一件该由人裁决的事变成整个文件处理节点的硬失败。
    if provider_error:
        raise BusinessError(
            "EXTERNAL_SERVICE_REJECTED",
            f"8018 未能完成材料类型校验：{provider_error}",
            status_code=502,
            details={
                "service": SERVICE_CODE,
                "operation": "VALIDATE",
                "declared_material_type": doc_type,
                "provider_error": provider_error,
                "confidence": score,
                "matched_keywords": matched_keyword_list,
            },
        )
    if not success and matched:
        # 响应自相矛盾：不挑一个当结论，按契约异常暴露。
        raise contract_error(SERVICE_CODE, "VALIDATE")
    return ExternalServiceResult(
        artifact=ExternalArtifact(
            status="SUCCESS",
            content={
                "matches_declared_material": matched,
                "declared_material_type": doc_type,
                "detected_material_type": detected_type,
                "confidence": score,
                "matched_keywords": matched_keyword_list,
                "matched_keyword_count": len(matched_keyword_list),
            },
        )
    )


def _supported_type_names(payload: dict[str, Any]) -> set[str]:
    rows = payload.get("types")
    if not isinstance(rows, list) or not rows:
        raise contract_error(SERVICE_CODE, "LIST_TYPES")
    names: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("id") is None:
            raise contract_error(SERVICE_CODE, "LIST_TYPES")
        try:
            names.add(non_empty_string(row["name"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise contract_error(SERVICE_CODE, "LIST_TYPES") from exc
    return names
