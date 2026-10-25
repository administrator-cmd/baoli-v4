"""共享领域服务：文件质量检查、材料存在性、大小写金额一致性。

节点与 Agent 工具共用这些判定，避免同一业务口径出现两套实现。本模块是这三类判定的
唯一实现：节点负责取数、落库、生成审核标准视图，它只回答「按这些真实输入，结论是什么」。

输入是已解析的真实数据，不是标识：本模块不查库、不持有会话、不解析材料角色，
调用方按自己的方式准备好数据（节点从 ORM 取，工具从 `ToolExecutionContext` 的会话取）。

边界：不产生 RuleResult、不写库、不生成页面视图（节点职责）；不为未实现的检查项
提供默认结论，缺判定依据时返回 EVIDENCE_INSUFFICIENT 并带上能力缺口；不代填业务阈值、
单位、币种，缺标准值即明确报错。
"""

from __future__ import annotations

from typing import Any

from . import file_quality


def summarize_per_material(per_material: list[dict[str, Any]]) -> dict[str, Any]:
    """把逐份材料的判定汇总成一个结论。

    「不合格优先、其次不可判定、全适用合格才通过」是业务口径，只写在这一处：
    节点与工具都必须经过它，不能各自写一遍汇总。
    """
    applicable = [item for item in per_material if item.get("status") != "NOT_APPLICABLE"]
    if not applicable:
        return {"status": "NOT_APPLICABLE", "message": "适用材料均未上传，本检查不适用"}
    for wanted in ("ABNORMAL", "EVIDENCE_INSUFFICIENT"):
        hits = [item for item in applicable if item.get("status") == wanted]
        if hits:
            first = hits[0]
            return {
                "status": wanted,
                "message": first.get("message"),
                # 能力缺口随结论带出：它是「为什么判不出来」的依据，不能丢。
                "capability_gap": first.get("capability_gap"),
                "capability_message": first.get("capability_message"),
            }
    return {"status": "PASSED", "message": "文件检查通过"}


def judge_quality_item(
    check_item: str,
    *,
    upload: Any,
    standard_value: str | None,
    unit: str | None,
    peers: list[dict[str, Any]] | None = None,
    gap: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """一个文件检查项目对一份上传文件的判定。

    已实现的检查项目走确定性判定；未实现的（图像清晰度、页码连续、有效签章、
    骑缝章完整）明确返回不可判定并带上能力缺口——不把「处理成功」当成「质量通过」。

    `peers` 是去重比对对象（同任务其它当前上传）；`gap` 是该规则的能力缺口记录。
    """
    if check_item == file_quality.CHECK_FILE_FORMAT:
        return file_quality.check_file_format(upload, standard_value=standard_value)
    if check_item == file_quality.CHECK_FILE_SIZE:
        return file_quality.check_file_size(upload, standard_value=standard_value, unit=unit)
    if check_item == file_quality.CHECK_FILE_DUPLICATE:
        return file_quality.check_file_duplicate(
            upload_id=upload.id,
            sha256=upload.sha256,
            other_uploads=peers or [],
        )
    if check_item in file_quality.KNOWN_QUALITY_ITEMS:
        return {
            "status": "EVIDENCE_INSUFFICIENT",
            "message": (
                f"检查项目“{file_quality.label_of(check_item)}”缺少已确认的判定依据，"
                "本项不可判定，不默认通过"
            ),
            "capability_gap": (gap or {}).get("gap", file_quality.GAP_CRITERION_UNCONFIRMED),
            "capability_message": (gap or {}).get("message"),
        }
    # 原文项目尚无处理器时暴露实际能力缺口；保留原文，不映射成另一项检查。
    return {
        "status": "EVIDENCE_INSUFFICIENT",
        "message": f"文件检查项目“{check_item}”尚无执行能力，本项无法执行",
        "capability_gap": "QUALITY_CAPABILITY_UNAVAILABLE",
    }


def check_materials(
    *,
    required_keys: list[str],
    available_keys: set[str],
    required_by_key: dict[str, bool],
    material_names: dict[str, str],
) -> dict[str, Any]:
    """材料存在性判定：声明的材料位是否都已上传。

    缺可选材料属于缺输入（不可判定），不是通过——「可选」说的是业务上允许没有，
    不是「系统可以据此判通过」。缺必传材料是不合格。

    `required_by_key` 是每个材料位的必传属性；它来自任务绑定的已确认配置。
    """
    if not required_keys:
        return {
            "status": "EVIDENCE_INSUFFICIENT",
            "message": "该材料检查未声明适用材料，无法执行",
            "missing_material_keys": [],
            "missing_material_names": [],
        }
    missing = sorted(set(required_keys) - available_keys)
    names = "、".join(material_names.get(key, key) for key in missing)
    if not missing:
        return {
            "status": "PASSED",
            "message": "所需材料已上传",
            "missing_material_keys": [],
            "missing_material_names": [],
        }
    optional_missing = all(not required_by_key.get(key, False) for key in missing)
    return {
        "status": "EVIDENCE_INSUFFICIENT" if optional_missing else "ABNORMAL",
        "message": (
            f"可选材料尚未上传，材料检查缺少输入，不能判定通过：{names}"
            if optional_missing
            else f"所需材料未上传：{names}"
        ),
        "missing_material_keys": missing,
        "missing_material_names": [material_names.get(key, key) for key in missing],
    }


def applicability_state(applicability: dict[str, Any], uploaded_keys: set[str]) -> dict[str, Any]:
    """规则的适用条件是否成立，以及适用条件引用到的未上传材料。

    材料存在性工具使用此函数读取适用条件；材料可选不等于规则不适用。

    `applicability` 是 `{"kind","label","material_keys"}`，来自已发布配置。
    """
    if applicability.get("kind") == "ALWAYS":
        return {**applicability, "applicable": True, "missing_material_keys": []}
    missing = [
        key for key in (applicability.get("material_keys") or []) if key not in uploaded_keys
    ]
    return {**applicability, "applicable": not missing, "missing_material_keys": missing}


def material_exists_result(
    *,
    required_keys: list[str],
    available_keys: set[str],
    required_by_key: dict[str, bool],
    material_names: dict[str, str],
    applicability: dict[str, Any],
    requirement: str | None = None,
    operator_label: str | None = None,
    check_level: str | None = None,
    status: str | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    """材料存在性检查的完整结果：判定 + 审核标准 + 实际比较 + 适用条件。

    节点（`services.audit.facts._material_check`）与 Agent 工具
    （`services.audit.tools.handlers.check_materials_tool`）都调这一份，两条路径不可能给出
    不同结论，也不会出现「工具目录声明的输出键在 Agent 路径上永远为空」。

    `status` / `message` 显式给出时（规则不适用，节点已判 NOT_APPLICABLE）原样采用，
    为 None 时由 `check_materials` 判定；两种情况返回的键集合完全相同。
    `applicability` 由调用方给出，它是规则的已发布配置事实。
    """
    verdict = (
        {"status": status, "message": message}
        if status is not None and message is not None
        else check_materials(
            required_keys=required_keys,
            available_keys=available_keys,
            required_by_key=required_by_key,
            material_names=material_names,
        )
    )
    declared = sorted(set(required_keys))
    missing = sorted(key for key in declared if key not in available_keys)
    uploaded = sorted(key for key in declared if key in available_keys)
    scope_names = "、".join(material_names.get(key, key) for key in declared)
    missing_names = "、".join(material_names.get(key, key) for key in missing)
    uploaded_names = "、".join(material_names.get(key, key) for key in uploaded)
    return {
        "status": verdict["status"],
        "message": verdict["message"],
        "applicability": {
            "kind": applicability.get("kind"),
            "label": applicability.get("label"),
            "material_keys": list(applicability.get("material_keys") or []),
            "missing_material_keys": list(applicability.get("missing_material_keys") or []),
        },
        "missing_material_keys": missing,
        "missing_material_names": [material_names.get(key, key) for key in missing],
        # 存在性检查也涉及尚未上传的材料位；适用条件不成立时没有执行检查。
        "checked_material_keys": declared if verdict["status"] != "NOT_APPLICABLE" else [],
        "standard": {
            "operator_label": operator_label,
            # 检查口径原文来自配置列；没有填写时如实标缺失，不代填默认条件。
            "requirement": requirement or None,
            # 这条检查声明的材料范围就是它的判定标准：这些材料位齐了才算通过。
            "comparison_label": f"所声明的材料位是否都已上传：{scope_names}" if scope_names else None,
            "check_level": check_level,
            "clear": bool(scope_names),
            **({} if scope_names else {"missing": "该检查没有声明适用材料，判定标准缺少业务依据"}),
        },
        # 实际比较：声明范围与实传情况逐项对照，读得出缺的是哪一份。
        "comparison": {
            "kind": "MATERIAL_EXISTS",
            "left_label": "本次已上传的材料位",
            "left_value": uploaded_names or "无",
            "right_label": "本条检查声明的材料位",
            "right_value": scope_names or "未声明",
            "expression": (
                f"已上传 {len(uploaded)} 项，声明 {len(declared)} 项"
                + (f"，缺少：{missing_names}" if missing else "，无缺少")
            ),
            "mode_label": "所声明的材料位均已上传",
        },
    }


def decimal_cn_equal(inputs: list[dict[str, Any]]) -> dict[str, Any]:
    """大小写金额一致性。

    当前不具备可执行能力：把大写金额换算成可核验数值需要业务先确认金额单位、
    币种与大写金额换算口径，代码不会自行补值、不会按字符串比较、也不让大模型做算术。
    因此明确返回不可判定并说明缺哪条口径——既不默认通过，也不伪装成已执行。

    双方抽取到的原文如实带出：口径缺失是标准不明确，不是这一条内容有问题。
    这是本判定的唯一实现，`operators.registry.decimal_cn_equal` 与工具都调它。
    """
    return {
        "status": "EVIDENCE_INSUFFICIENT",
        "message": (
            "大小写金额一致性检查需要先把大写金额换算成有证据的数值，"
            "而金额单位、币种与大写金额换算口径尚未由业务确认；"
            "本项不可判定，不能按字符串比较，也不能默认通过"
        ),
        "capability_gap": "DECIMAL_CN_CONVERSION_UNDEFINED",
        # 无法判断的具体原因：标准不明确（换算口径未确认），不是缺少原文。
        "blocked_reason": "STANDARD_UNDEFINED",
        "extracted_values": [
            {
                "field_key": item.get("field_key"),
                "material_key": item.get("material_key"),
                "raw_value": item.get("raw_value"),
                "normalized_value": item.get("normalized_value"),
            }
            for item in inputs
        ],
    }
