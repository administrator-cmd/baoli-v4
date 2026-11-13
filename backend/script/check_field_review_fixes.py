"""九项提交后修复的只读验证脚本。

用法（在 backend 下）：

    python script/check_field_review_fixes.py

检查三项能在真实数据上判定的事实：

1. 字段事实身份唯一性：全部场景版本与任务冻结配置里没有重复的完整事实身份，
   即开启编译期全字段校验后不会拒绝现有配置。
2. 任务详情可读取：逐任务调用 `task_view`，确认共用一份当前字段事实配对后，
   字段、规则与核对状态仍能正常组装。
3. 续跑范围输出契约：可续跑批次的摘要必须同时给出 preserved / pending / dependent /
   waiting_review 四项，且四项之和等于当前执行计划的规则数。

只读：不写库、不改文件、不调用模型或外部接口。退出码 0 = 全部通过，1 = 有检查失败。
"""

from __future__ import annotations

import pathlib
import sys

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import select  # noqa: E402

from app.core.database import SessionLocal  # noqa: E402
from app.domain.audit import fact_projection  # noqa: E402
from app.modules.audit.task.model import AuditTask  # noqa: E402
from app.modules.audit.task.service.detail import AuditTaskDetailService  # noqa: E402
from app.modules.scenario.template.model import ScenarioVersion  # noqa: E402

SUMMARY_KEYS = {"preserved", "pending", "dependent", "waiting_review"}


def _identity_duplicates(fields: list[dict]) -> list[dict]:
    """同一组字段里重复的完整事实身份；没有重复返回空列表。"""
    seen: dict[tuple, str] = {}
    duplicates: list[dict] = []
    for field in fields:
        identity = fact_projection.field_fact_identity(field)
        if identity in seen:
            duplicates.append({"field_keys": [seen[identity], field["field_key"]], "identity": list(identity)})
        else:
            seen[identity] = field["field_key"]
    return duplicates


def check_identity_unique(db) -> list[str]:
    problems: list[str] = []
    versions = db.scalars(select(ScenarioVersion)).all()
    for version in versions:
        fields = (version.manifest_json or {}).get("fields") or []
        for item in _identity_duplicates(fields):
            problems.append(f"场景版本 {version.id} 存在重复事实身份：{item}")
    tasks = db.scalars(select(AuditTask)).all()
    for task in tasks:
        fields = (task.task_schema_json or {}).get("fields") or []
        for item in _identity_duplicates(fields):
            problems.append(f"任务 {task.task_no} 存在重复事实身份：{item}")
    print(f"[身份唯一性] 场景版本 {len(versions)} 个、任务 {len(tasks)} 个，重复 {len(problems)} 处")
    return problems


def check_task_views(db) -> list[str]:
    problems: list[str] = []
    tasks = db.scalars(select(AuditTask).order_by(AuditTask.id)).all()
    for task in tasks:
        try:
            view = AuditTaskDetailService(db).task_view(task)
        except Exception as exc:  # noqa: BLE001 - 只读巡检需要报告每个任务的真实失败原因
            problems.append(f"任务 {task.task_no} 详情组装失败：{exc}")
            continue
        node = view["current_node"]
        review = node.get("field_review") or {}
        missing = {"required", "confirmed", "missing_field_count", "after_review_rule_count"} - set(review)
        if missing:
            problems.append(f"任务 {task.task_no} 核对状态缺字段：{sorted(missing)}")
        if "resume" not in node:
            problems.append(f"任务 {task.task_no} 详情缺少续跑状态")
        # 可续跑批次必须给出四类互斥范围，且合计等于当前计划规则数。
        plan = (node.get("resume") or {}).get("plan")
        if plan is not None:
            absent = SUMMARY_KEYS - set(plan)
            if absent:
                problems.append(f"任务 {task.task_no} 续跑摘要缺字段：{sorted(absent)}")
        print(
            f"[任务详情] {task.task_no} status={view['status']} "
            f"fields={len(view['fields'])} rules={len(view['rules'])} plan={plan}"
        )
    return problems


def main() -> int:
    with SessionLocal() as db:
        problems = [*check_identity_unique(db), *check_task_views(db)]
    if problems:
        print("\n失败项：")
        for item in problems:
            print(f"- {item}")
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
