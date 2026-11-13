"""场景与方案的 JSON 配置读写；编辑器使用属性视图，不再读写业务明细表。"""

from copy import deepcopy
from dataclasses import asdict, dataclass, fields
from typing import Any, Unpack

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.common.exceptions import BusinessError
from app.core.base_crud import SessionCRUD
from app.domain.configuration.config_changes import FieldChanges, GeneratedRuleChanges, MaterialChanges, RuleChanges, SectionChanges
from app.modules.plan.plan.model import AuditPlan
from app.modules.scenario.draft.model import ScenarioDraft


@dataclass(frozen=True)
class ConfigItem:
    """JSON 配置的读取视图；写入通过本模块的显式更新方法标记持久化。"""

    owner: Any
    section: str
    data: dict[str, Any]
    _model: type | None = None

    def __getattr__(self, key: str) -> Any:
        if key in {"created_at", "updated_at"}:
            return getattr(self.owner, key)
        try:
            return self.data[key]
        except KeyError as exc:
            raise AttributeError(key) from exc


def _update(item: ConfigItem, section: str, changes: dict[str, Any], allowed: frozenset[str]) -> None:
    if item.section != section or changes.keys() - allowed:
        raise ValueError(f"配置更新目标或字段无效: {section}, {sorted(changes)}")
    if changes:
        item.data.update(deepcopy(changes))
        flag_modified(item.owner, "config_json")


def update_material(item: ConfigItem, **changes: Unpack[MaterialChanges]) -> None:
    _update(item, "materials", changes, MaterialChanges.__optional_keys__)


def update_field(item: ConfigItem, **changes: Unpack[FieldChanges]) -> None:
    _update(item, "fields", changes, FieldChanges.__optional_keys__)


def update_rule(item: ConfigItem, **changes: Unpack[RuleChanges]) -> None:
    _update(item, "rules", changes, RuleChanges.__optional_keys__)


def update_generated_rule(item: ConfigItem, **changes: Unpack[GeneratedRuleChanges]) -> None:
    """同步编译产物；用户编辑规则不能借此改写身份与来源。"""
    _update(item, "rules", changes, GeneratedRuleChanges.__optional_keys__)


def update_section(item: ConfigItem, **changes: Unpack[SectionChanges]) -> None:
    _update(item, "sections", changes, SectionChanges.__optional_keys__)


def update_extensions(item: ConfigItem, metadata: dict[str, Any]) -> None:
    """保留动态业务元数据；不能覆盖数据类的固定字段。"""
    if item._model is None:
        raise ValueError("发布配置视图不支持写入扩展元数据")
    reserved = {field.name for field in fields(item._model)}
    if metadata.keys() & reserved:
        raise ValueError(f"扩展元数据不能覆盖固定字段: {sorted(metadata.keys() & reserved)}")
    if metadata:
        item.data.update(deepcopy(metadata))
        flag_modified(item.owner, "config_json")


def _owner(db: Session, model: type, owner_id: int) -> Any:
    owner_type = ScenarioDraft if model.owner_key == "draft_id" else AuditPlan
    owner = SessionCRUD(db).get_of(owner_type, owner_id)
    if owner is None:
        raise BusinessError("CONFIG_OWNER_MISSING", "配置所属的场景草稿或方案不存在", status_code=404)
    if not isinstance(owner.config_json, dict):
        raise BusinessError("CONFIG_JSON_MISSING", "场景或方案缺少 JSON 配置", status_code=500)
    return owner


def rows(db: Session, model: type, owner_id: int) -> list[ConfigItem]:
    owner = _owner(db, model, owner_id)
    section = model.section
    items = [ConfigItem(owner, section, item, model) for item in owner.config_json[section]]
    order_key = "sequence_no" if section == "nodes" else "sort_no"
    return sorted(items, key=lambda item: (getattr(item, order_key), item.id))


def add(db: Session, item: Any) -> ConfigItem:
    """将编辑器配置对象写进 JSON；属性形状不属于数据库元数据或会话。"""
    model = type(item)
    owner_key = model.owner_key
    owner = _owner(db, model, getattr(item, owner_key))
    section = model.section
    data = asdict(item)
    data["id"] = max((entry["id"] for entry in owner.config_json[section]), default=0) + 1
    owner.config_json[section].append(data)
    flag_modified(owner, "config_json")
    return ConfigItem(owner, section, data, model)


def delete(item: ConfigItem) -> None:
    item.owner.config_json[item.section].remove(item.data)
    flag_modified(item.owner, "config_json")


def next_sort_no(db: Session, model: type, owner_id: int) -> int:
    return max((item.sort_no for item in rows(db, model, owner_id)), default=0) + 1


def add_mapping(db: Session, model: type, owner_id: int, payload: dict[str, Any], sort_no: int) -> ConfigItem:
    """编辑器协议提供已知属性形状，其余场景 JSON 元数据原样保留。"""
    owner_key = model.owner_key
    columns = {item.name for item in fields(model)}
    item = model(
        **{key: value for key, value in payload.items() if key in columns and key not in {"id", owner_key, "sort_no"}},
        **{owner_key: owner_id, "sort_no": sort_no},
    )
    row = add(db, item)
    update_extensions(row, {key: value for key, value in payload.items() if key not in columns})
    return row
