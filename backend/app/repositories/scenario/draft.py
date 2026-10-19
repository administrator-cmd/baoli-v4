"""场景草稿配置行与排序序号读取。"""

from sqlalchemy.orm import Session
from ..shared import configuration as config_store
from ...entity import (
    ScenarioDraftField,
    ScenarioDraftNode,
    ScenarioDraftMaterial,
    ScenarioDraftRule,
    ScenarioDraftSection,
)


def draft_rows(
    db: Session, draft_id: int
) -> tuple[
    list[ScenarioDraftNode],
    list[ScenarioDraftMaterial],
    list[ScenarioDraftField],
    list[ScenarioDraftRule],
    list[ScenarioDraftSection],
]:
    """草稿的全部业务行：节点实例、材料、字段、规则、报告章节。

    技术容器只用于持久化关联；规则的显式引用决定执行顺序。
    """
    nodes = sorted(config_store.rows(db, ScenarioDraftNode, draft_id), key=lambda item: (item.sequence_no, item.id,))
    materials = sorted(config_store.rows(db, ScenarioDraftMaterial, draft_id), key=lambda item: (item.sort_no, item.id,))
    fields = sorted(config_store.rows(db, ScenarioDraftField, draft_id), key=lambda item: (item.sort_no, item.id,))
    rules = sorted(config_store.rows(db, ScenarioDraftRule, draft_id), key=lambda item: (item.sort_no, item.id,))
    sections = sorted(config_store.rows(db, ScenarioDraftSection, draft_id), key=lambda item: (item.sort_no, item.id,))
    return list(nodes), list(materials), list(fields), list(rules), list(sections)


def next_sort_no(db: Session, model: type, draft_id: int) -> int:
    return config_store.next_sort_no(db, model, draft_id)
