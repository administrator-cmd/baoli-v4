"""场景与方案的 JSON 配置读写；编辑器使用属性视图，不再读写业务明细表。"""

from copy import deepcopy
from dataclasses import asdict, fields
from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.common.exceptions import BusinessError
from app.core.base_crud import SessionCRUD
from app.modules.plan.plan.model import AuditPlan
from app.modules.scenario.draft.model import ScenarioDraft


class ConfigItem:
    """JSON 中一个配置对象的属性视图；修改直接落在所属配置上。"""

    def __init__(self, owner: Any, section: str, data: dict[str, Any]):
        object.__setattr__(self, "owner", owner)
        object.__setattr__(self, "section", section)
        object.__setattr__(self, "data", data)

    def __getattr__(self, key: str) -> Any:
        if key in {"created_at", "updated_at"}:
            return getattr(self.owner, key)
        try:
            return self.data[key]
        except KeyError as exc:
            raise AttributeError(key) from exc

    def __setattr__(self, key: str, value: Any) -> None:
        self.data[key] = value
        flag_modified(self.owner, "config_json")


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
    items = [ConfigItem(owner, section, item) for item in owner.config_json[section]]
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
    return ConfigItem(owner, section, data)


def delete(item: ConfigItem) -> None:
    item.owner.config_json[item.section].remove(item.data)
    flag_modified(item.owner, "config_json")


def next_sort_no(db: Session, model: type, owner_id: int) -> int:
    return max((item.sort_no for item in rows(db, model, owner_id)), default=0) + 1


def add_mapping(db: Session, model: type, owner_id: int, payload: dict[str, Any], sort_no: int) -> ConfigItem:
    """编辑器协议提供已知属性形状，其余场景 JSON 元数据原样保留。"""
    owner_key = model.owner_key
    columns = {item.name for item in fields(model)}
    item = model(**{key: value for key, value in payload.items() if key in columns and key not in {"id", owner_key, "sort_no"}},
                 **{owner_key: owner_id, "sort_no": sort_no})
    row = add(db, item)
    row.data.update(deepcopy({key: value for key, value in payload.items() if key not in columns}))
    flag_modified(row.owner, "config_json")
    return row
