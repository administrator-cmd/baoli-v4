"""动态场景配置容器：业务字段和规则来自规则库，不依赖 Excel 列模板。"""

from typing import Any


def empty_config() -> dict[str, Any]:
    return {"nodes": [], "materials": [], "fields": [], "rules": [], "sections": [], "calculation_json": None, "context": {}}


def config_property(key: str) -> property:
    """已有服务的配置属性映射到同一 JSON 容器；只在新对象构造时初始化。"""
    def get(owner: Any) -> Any:
        return owner.config_json[key]

    def set_value(owner: Any, value: Any) -> None:
        from sqlalchemy.orm.attributes import flag_modified
        if owner.config_json is None:
            owner.config_json = empty_config()
        owner.config_json[key] = value
        flag_modified(owner, "config_json")

    return property(get, set_value)
