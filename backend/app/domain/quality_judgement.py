"""共享领域服务：文件质量检查。

本模块只回答「按这些真实输入，文件质量结论是什么」：不查库、不持有会话、不解析材料角色，
调用方按自己的方式准备好数据。

边界：不为未实现的检查项提供默认结论，缺判定依据时返回 EVIDENCE_INSUFFICIENT 并带上
能力缺口；不代填业务阈值、单位、币种，缺标准值即明确报错。
"""

from __future__ import annotations

from typing import Any

from app.domain import file_quality


def summarize_per_material(per_material: list[dict[str, Any]]) -> dict[str, Any]:
    """把逐份材料的判定汇总成一个结论。

    「不合格优先、其次不可判定、全适用合格才通过」是业务口径，只写在这一处：
    调用方都必须经过它，不能各自写一遍汇总。
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
