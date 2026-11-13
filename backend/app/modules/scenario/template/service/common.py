"""场景配置的类型常量、引用与作用域转换。"""

from typing import Any

from sqlalchemy.orm import Session

from app.constants.status import SCOPE_NOT_APPLICABLE
from app.domain.configuration import manifest as manifest_module
from app.domain.configuration import node_profile
from app.domain.shared.identity import public_no
from app.domain.configuration.manifest import FORM_VALUE_TYPES

SYSTEM_OPERATORS = manifest_module.SYSTEM_OPERATORS


DRAFT_VALUE_TYPES = FORM_VALUE_TYPES


FIELD_ARTIFACT_SOURCES = {"SEAL"}


DRAFT_SOURCE_LABELS = {"FORM": "页面编辑", "EXCEL": "规则库导入", "COPY": "复制场景", "REVISION": "版本修订"}


class ScenarioTemplateCommonService:
    """本实体的服务方法；db 由构造时注入。"""

    def __init__(self, db: Session | None = None) -> None:
        self.db = db

    def _field_scope(self, payload: dict[str, Any], key: str) -> str:
        """口径取自规则库原文；未涉及时使用存储唯一键所需的非空标识。"""
        return str(payload.get(key) or "").strip() or SCOPE_NOT_APPLICABLE


    def _item_ref(self) -> str:
        return public_no("ref")


    def _node_type_label(self, node_type: str) -> str:
        """技术执行容器的展示名称。"""
        return node_profile.NODE_DISPLAY_NAME


    def _normalized_source_ref(self, value: Any) -> str | None:
        """业务编号：留空表示页面新建、没有来源编号，不伪造一个。"""
        text = str(value or "").strip()
        return text or None
