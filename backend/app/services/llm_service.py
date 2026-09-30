"""规则库工作簿的确定性转换与结果合并。"""

import hashlib
from typing import Any

from pydantic import ValidationError

from ..common.exceptions import BusinessError
from ..domain import node_rulebook, unified_profile
from ..domain.proposals import ProposalBundle, coverage_totals
from ..domain.workbook import WorkbookIR


def convert_node_rulebook(
    ir: WorkbookIR,
    bindings: dict[str, dict[str, Any]],
    *,
    profile: dict[str, Any] | None = None,
) -> tuple[ProposalBundle, dict[str, Any]]:
    """节点版规则库的确定性转换入口。

    返回（候选集合，节点版附加信息）。附加信息是 `build_convert_bundle` 的原始 payload，
    含 `node_notes`（必须留给业务处理的待确认事项与能力缺口）、`coverage`（逐行对账）
    与 `bundle_hash`。消费方按 `node_notes` 取待确认事项，不能把整份 payload 当成列表。

    转换结果统一使用 `ProposalBundle` 契约，下游的引用校验、裁决、编译、发布与
    页面展示不需要第二套代码。
    """
    payload = node_rulebook.build_convert_bundle(ir, bindings, profile=profile)
    try:
        bundle = ProposalBundle.model_validate(
            {"sheets": payload["sheets"], "coverage": payload["coverage"]}
        )
    except ValidationError as exc:
        first_error = exc.errors(include_url=False)[0]
        location = ".".join(str(part) for part in first_error["loc"])
        raise BusinessError(
            "NODE_RULEBOOK_BUNDLE_INVALID",
            f"节点版规则库解析结果与候选契约不一致：{location}（{first_error['type']}）",
            status_code=500,
        ) from exc
    return bundle, payload
def convert_workbook_with_notes(
    ir: WorkbookIR,
    bindings: dict[str, dict[str, Any]],
    *,
    profile: dict[str, Any] | None = None,
) -> tuple[ProposalBundle, dict[str, Any]]:
    """返回（候选集合，转换附加信息）。

    附加信息包含 `node_notes` / `coverage` / `bundle_hash`，消费方必须按键读取，
    不能把整份附加信息当成列表。

    分派依据是**用户绑定的 Sheet 角色**，不是文件名、表名或表头；
    列名和别名由上传时冻结的场景有效 Profile 提供。

    **按 Sheet 独立转换，结果合并**：每张表由负责它的解析器转换（01/02/03/04/05/09/10
    走 `node_rulebook`，06/07/08 计算三表走 `financial_report`），绑定里出现哪几类表就
    转换哪几类，彼此互不干扰。

    角色完全不在当前有效协议里时明确报错。
    """
    if profile is None:
        profile = unified_profile.load_unified_profile()
    index = unified_profile.sheet_index(profile)
    roles = sorted(
        {
            str((bindings.get(sheet.sheet_key) or {}).get("role"))
            for sheet in ir.sheets
            # 命中说明页的元数据 Sheet 由导入侧固定排除；这里只看用户绑定与明确排除，
            # 不看 `sheet.visible`（隐藏的业务表仍可由用户绑定并参与转换）。
            if not (bindings.get(sheet.sheet_key) or {}).get("excluded")
        }
        - {"None"}
    )
    if not roles:
        # 一张业务表都没有绑定：没有可转换的内容，明确报错而不是返回空候选。
        raise BusinessError(
            "IMPORT_NO_BOUND_SHEET",
            "本次导入没有绑定任何业务工作表，没有可转换的内容",
            details={"sheet_count": len(ir.sheets)},
        )
    unknown = [role for role in roles if role not in index]
    if unknown:
        raise BusinessError(
            "SHEET_ROLE_UNSUPPORTED",
            "本次导入绑定的 Sheet 用途不在统一模板协议里："
            + "、".join(f"“{item}”" for item in unknown)
            + "。请确认绑定结果",
            details={"roles": unknown},
        )
    from ..domain import financial_report

    parts: list[tuple[ProposalBundle, dict[str, Any]]] = []
    if set(roles) & node_rulebook.OWNED_ROLES:
        parts.append(convert_node_rulebook(ir, bindings, profile=profile))
    if set(roles) & financial_report.OWNED_ROLES:
        parts.append(convert_financial_report(ir, bindings, profile=profile))
    if not parts:
        raise BusinessError(
            "SHEET_ROLE_UNSUPPORTED",
            "本次导入绑定的 Sheet 用途没有对应的确定性转换路径："
            + "、".join(f"“{item}”" for item in roles),
            details={"roles": roles},
        )
    if len(parts) == 1:
        return parts[0]
    return _merge_bundles(parts)


def _merge_bundles(
    parts: list[tuple[ProposalBundle, dict[str, Any]]],
) -> tuple[ProposalBundle, dict[str, Any]]:
    """合并多个按 Sheet 独立转换的结果。

    合并是**并集**：Sheet 候选、逐行覆盖对账、计算候选与待确认事项各自拼接。
    各解析器只产出自己那几张表的候选，因此不存在「同一张表被两处转换」的情况；
    真出现重复 sheet_key 说明绑定或分派有矛盾，明确报错而不是覆盖。
    """
    sheets: list[Any] = []
    coverage_sheets: list[dict[str, Any]] = []
    calculations: list[Any] = []
    notes: list[dict[str, Any]] = []
    seen_sheets: set[str] = set()
    for bundle, payload in parts:
        for sheet in bundle.sheets:
            if sheet.sheet_key in seen_sheets:
                raise BusinessError(
                    "IMPORT_SHEET_CONVERTED_TWICE",
                    "同一张工作表被转换了两次，绑定或分派存在矛盾",
                    status_code=500,
                    details={"sheet_key": sheet.sheet_key},
                )
            seen_sheets.add(sheet.sheet_key)
            sheets.append(sheet)
        coverage = payload.get("coverage") or {}
        coverage_sheets.extend(coverage.get("sheets") or [])
        calculations.extend(payload.get("calculations") or [])
        notes.extend(payload.get("node_notes") or [])
    coverage = {
        "sheets": coverage_sheets,
        "totals": coverage_totals(coverage_sheets),
    }
    merged = ProposalBundle.model_validate(
        {"sheets": sheets, "coverage": coverage, "calculations": calculations}
    )
    hashes = [str(payload.get("bundle_hash") or "") for _, payload in parts]
    payload = {
        "node_notes": notes,
        "coverage": coverage,
        "calculations": calculations,
        "bundle_hash": hashlib.sha256("|".join(sorted(hashes)).encode("utf-8")).hexdigest(),
        "merged_parts": len(parts),
    }
    return merged, payload


def convert_financial_report(
    ir: WorkbookIR,
    bindings: dict[str, dict[str, Any]],
    *,
    profile: dict[str, Any] | None = None,
) -> tuple[ProposalBundle, dict[str, Any]]:
    """计算类规则库的确定性转换入口。

    与合同审核路径同一收口方式：转换结果共用 `ProposalBundle` 契约，因此下游的
    引用校验、裁决、编译、发布与页面展示不需要第二套代码。差别在于这里的附加信息里
    多了 `calculations`（计算规则候选：定义 + 参数 + 判定区间）。

    列协议由导入链传入的场景有效 Profile 提供。
    """
    from ..domain import financial_report

    if profile is None:
        profile = unified_profile.load_unified_profile()
    payload = financial_report.build_convert_bundle(ir, bindings, profile)
    try:
        bundle = ProposalBundle.model_validate(
            {
                "sheets": payload["sheets"],
                "coverage": payload["coverage"],
                # 计算规则候选跨 Sheet（定义/参数/判定三张表按 rule_code 关联），
                # 因此放在顶层而不是挂在某一张 Sheet 上。
                "calculations": payload["calculations"],
            }
        )
    except ValidationError as exc:
        raise BusinessError(
            "FINANCIAL_REPORT_BUNDLE_INVALID",
            "计算类规则库的确定性解析结果不符合候选契约，请检查填写内容后重新上传",
            status_code=500,
        ) from exc
    return bundle, payload
