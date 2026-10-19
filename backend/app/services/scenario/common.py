"""场景配置的类型常量、引用与作用域转换。"""

from typing import Any
from ...domain import manifest as manifest_module, node_profile
from ...domain.audit_vocab import SCOPE_NOT_APPLICABLE
from ...domain.manifest import FORM_VALUE_TYPES
from ...domain.identity import public_no


SYSTEM_OPERATORS = manifest_module.SYSTEM_OPERATORS


DRAFT_VALUE_TYPES = FORM_VALUE_TYPES


FIELD_ARTIFACT_SOURCES = {"SEAL"}


DRAFT_SOURCE_LABELS = {"FORM": "页面编辑", "EXCEL": "规则库导入", "COPY": "复制场景", "REVISION": "版本修订"}


def _field_scope(payload: dict[str, Any], key: str) -> str:
    """口径取自规则库原文；未涉及时使用存储唯一键所需的非空标识。"""
    return str(payload.get(key) or "").strip() or SCOPE_NOT_APPLICABLE


def _item_ref() -> str:
    return public_no("ref")


def _node_type_label(node_type: str) -> str:
    """技术执行容器的展示名称。"""
    return node_profile.NODE_DISPLAY_NAME


def _normalized_source_ref(value: Any) -> str | None:
    """业务编号：留空表示页面新建、没有来源编号，不伪造一个。"""
    text = str(value or "").strip()
    return text or None
