from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from langchain_core.tools import BaseTool, tool

from ..domain import quality_judgement
from ..domain.currency import canonical_currency
from ..domain.decimal_math import exact_subtract, exact_sum
from ..common.exceptions import BusinessError


def _comparison(
    *,
    kind: str,
    left_index: int,
    right_index: int,
    left_value: Any,
    right_value: Any,
    expression: str,
    mode_label: str,
    threshold_text: str | None = None,
    actual_diff: str | None = None,
    addends: list[str] | None = None,
) -> dict[str, Any]:
    """一条检查的对比过程，供页面与报告直接呈现。

    金额与日期都由服务端按标准化值做确定性计算，这里如实记下实际算式与双方取值：
    只给一个“通过/异常”会让工作人员无法核验系统到底比了什么。
    左右两侧用输入下标引用，中文名由服务端按同一次执行的输入摘要补齐，
    算子层不猜字段中文名、也不从字段名推断业务语义。
    """
    payload: dict[str, Any] = {
        "kind": kind,
        "left_index": left_index,
        "right_index": right_index,
        "left_value": None if left_value is None else str(left_value),
        "right_value": None if right_value is None else str(right_value),
        "expression": expression,
        "mode_label": mode_label,
    }
    if threshold_text is not None:
        payload["threshold"] = threshold_text
    if actual_diff is not None:
        payload["actual_diff"] = actual_diff
    if addends is not None:
        # 加总式必须保留每个加数的真实取值：只给合计无法核对是哪一项对不上。
        payload["addends"] = list(addends)
    return payload


# 材料类型不可用：依赖该材料的检查缺少有效依据。两种来源必须分开：
#   TYPE_REVIEW   = 类型待复核，尚未由人定案；
#   TYPE_REJECTED = 人工已确认与材料位不符。
# 只有 TYPE_REJECTED 是确定的人工结论，才阻断依赖它的检查。TYPE_REVIEW 的识别结论
# 来自已下线为“仅供参考”的自动识别（或人工的“证据不足”），它既不是人的否定结论，
# 也不能把真实原文依据判定成不可用：此时规则按材料里的真实内容照常判定，
# 类型问题本身作为一条待确认事项单独暴露（见 services.audit.rules）。
# 服务端已把历史上由已下线的自动识别写下的旧 `TYPE_MISMATCH` 按「待复核」归一
# （见 domain.audit_vocab.read_material_type_status）。
REVIEW_MATERIAL_STATUS = "TYPE_REVIEW"
REJECTED_MATERIAL_STATUS = "TYPE_REJECTED"

# 金额与日期的比较方式。与编译期 domain.manifest 的集合保持一致：
#   EQ/GTE/LTE 可带非负允许差值；GT/LT 只用于日期严格比较。
DECIMAL_COMPARE_MODES = {"EQ", "GTE", "LTE", "ABS_DIFF_LTE"}
DATE_COMPARE_MODES = {"EQ", "GTE", "LTE", "GT", "LT"}

# 比较方式的中文：只用于把实际算式写进结果，供页面与报告直接呈现。
DECIMAL_MODE_LABELS = {
    "EQ": "数值相等",
    "GTE": "数值不小于",
    "LTE": "数值不大于",
    "ABS_DIFF_LTE": "差额不超过允许差值",
}
DATE_MODE_LABELS = {"EQ": "同一天", "GTE": "不早于", "LTE": "不晚于", "GT": "晚于", "LT": "早于"}
DATE_COMPARISON_SYMBOLS = {"EQ": "=", "GTE": "≥", "LTE": "≤", "GT": ">", "LT": "<"}


def _input_label(item: dict[str, Any]) -> str:
    """输入的稳定业务标识：字段键 + 材料键。中文名由服务端在结果里另行给出。"""
    source = item.get("material_key") or "未知材料"
    return f"{item.get('field_key') or '未知字段'}@{source}"


def _unavailable_status(inputs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """输入不可用的原因；全部可用时返回 None。

    每种不可用都必须区分开：材料类型被人工确认不符、材料尚未形成处理产物、
    字段没有有效值。任何一种都不能当作通过，原因文案也不能互相冒充。

    这里不把「材料类型待复核」当作不可用：该状态来源于已下线为仅供参考的自动识别
    （或人工的“证据不足”），不是对材料内容的否定结论，真实原文依据仍然可用。
    依赖它的规则照常按真实内容判定，类型问题单独作为待确认事项暴露。
    """
    rejected = [item for item in inputs if item.get("material_state") == REJECTED_MATERIAL_STATUS]
    if rejected:
        names = "、".join(sorted(_input_label(item) for item in rejected))
        return {
            "status": "EVIDENCE_INSUFFICIENT",
            "message": f"依赖的材料类型已被人工确认与材料位不符，相关检查缺少有效依据：{names}",
        }
    pending_material = [
        item
        for item in inputs
        if item.get("material_state") not in {None, "PREPARED", REVIEW_MATERIAL_STATUS}
    ]
    if pending_material:
        return {
            "status": "EVIDENCE_INSUFFICIENT",
            "message": "依赖材料的文件处理尚未完成，规则缺少有效输入",
        }
    unavailable = [
        item
        for item in inputs
        if item.get("status") not in {"PRESENT", "REVIEWED"} or item.get("normalized_value") is None
    ]
    if unavailable:
        # 字段级原因必须逐项写清：抽取失败、原文未形成标准化值、尚未抽取是三种不同的
        # 缺什么，统一写成“证据不足”会让人无法判断该去补哪一步。
        reasons = "、".join(f"{_input_label(item)}（{_status_text(item)}）" for item in unavailable)
        return {
            "status": "EVIDENCE_INSUFFICIENT",
            "message": f"参与比较的字段没有可用的标准值，规则未能判断：{reasons}",
            "missing_inputs": [_input_label(item) for item in unavailable],
        }
    return None


def _unit_gap(inputs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """金额/数值比较的口径一致性判断。

    单位或币种不一致时系统不做换算，也不因为数值相等就通过；
    只有一方声明单位时同样无法确认口径，按证据不足处理。
    """
    dimensions = (
        ("单位", "unit"),
        ("币种", "currency"),
    )
    for label, key in dimensions:
        values = [
            canonical_currency(item.get(key)) if key == "currency" else (item.get(key) or "").strip()
            for item in inputs
        ]
        declared = [value for value in values if value]
        if key == "currency" and any(
            str(item.get("value_type") or "").upper() in {"AMOUNT", "DECIMAL_CN"}
            for item in inputs
        ) and not declared:
            return {
                "status": "EVIDENCE_INSUFFICIENT",
                "message": "金额字段没有声明币种，系统不默认人民币，规则未能判断",
            }
        if not declared:
            continue
        if len(set(declared)) > 1:
            return {
                "status": "EVIDENCE_INSUFFICIENT",
                "message": (
                    f"比较双方{label}口径不一致（{' / '.join(values)}），"
                    "系统不做单位或币种换算，规则未能判断"
                ),
            }
        if len(declared) != len(values):
            return {
                "status": "EVIDENCE_INSUFFICIENT",
                "message": (
                    f"只有部分字段声明了{label}（{' / '.join(values)}），"
                    "口径无法确认，规则未能判断"
                ),
            }
    return None


def _require_inputs(inputs: list[dict[str, Any]], count: int) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """取出参与判定的输入；不足或不可用时返回明确状态，绝不返回默认值。"""
    if len(inputs) < count:
        raise BusinessError("RULE_INPUT_MISSING", "规则缺少必要输入字段", details={"required": count, "actual": len(inputs)})
    selected = inputs[:count]
    problem = _unavailable_status(selected)
    if problem:
        return [], problem
    return selected, None


def _decimal_of(value: Any) -> Decimal:
    number = Decimal(str(value))
    if not number.is_finite():
        raise InvalidOperation("规则数值必须是有限十进制数")
    return number


def _threshold_of(
    parameters: dict[str, Any],
    inputs: list[dict[str, Any]],
    *,
    integer_unit: bool = False,
) -> Decimal | None | dict[str, Any]:
    """读取非负「允许差值」。

    返回 Decimal（允许差值）、None（本规则没有配置差值）或一个不可判定的结果字典
    （阈值单位与字段口径不一致时不做换算，按证据不足处理）。
    差值必须是有限数值：非法值直接报错，不退回 0 或忽略阈值——那会静默改变业务条件。
    """
    if "threshold" not in parameters:
        return None
    try:
        threshold = _decimal_of(parameters["threshold"])
    except (InvalidOperation, TypeError) as exc:
        raise BusinessError("RULE_PARAMETER_INVALID", "允许差值不是有效数值") from exc
    if not threshold.is_finite() or threshold < 0:
        raise BusinessError("RULE_PARAMETER_INVALID", "允许差值必须是不小于 0 的有限数值")
    if integer_unit and threshold != threshold.to_integral_value():
        raise BusinessError("RULE_PARAMETER_INVALID", "日期差值以天为单位，必须是非负整数天")
    threshold_unit = (parameters.get("threshold_unit") or "").strip()
    declared = {(item.get("unit") or "").strip() for item in inputs if (item.get("unit") or "").strip()}
    if threshold_unit and declared and declared != {threshold_unit}:
        return {
            "status": "EVIDENCE_INSUFFICIENT",
            "message": f"差值单位（{threshold_unit}）与字段单位（{' / '.join(sorted(declared))}）不一致，系统不做换算",
        }
    return threshold


@tool("FIELD_EQUAL")
def field_equal(parameters: dict[str, Any], inputs: list[dict[str, Any]]) -> dict[str, Any]:
    """字段一致比较：两侧规范化取值必须相同。"""
    selected, problem = _require_inputs(inputs, 2)
    if problem:
        return problem
    left, right = selected
    left_type = left.get("value_type")
    right_type = right.get("value_type")
    if left_type != right_type:
        raise BusinessError("RULE_VALUE_INVALID", "两个字段的类型不一致，不能直接比较")
    left_value = left["normalized_value"]
    right_value = right["normalized_value"]
    if left_type in {"DECIMAL", "AMOUNT"}:
        gap = _unit_gap(selected)
        if gap:
            return gap
        try:
            left_decimal = _decimal_of(left_value)
            right_decimal = _decimal_of(right_value)
        except InvalidOperation as exc:
            raise BusinessError("RULE_VALUE_INVALID", "金额字段无法按数值比较") from exc
        passed = left_decimal == right_decimal
        diff = exact_subtract(left_decimal, right_decimal)
        comparison = _comparison(
            kind="DECIMAL",
            left_index=0,
            right_index=1,
            left_value=left_decimal,
            right_value=right_decimal,
            expression=f"{left_decimal} {'=' if passed else '≠'} {right_decimal}",
            mode_label="金额相等",
            actual_diff=str(diff),
        )
        message = (
            f"两侧金额一致（{left_decimal} = {right_decimal}）"
            if passed
            else f"两侧金额不一致（{left_decimal} ≠ {right_decimal}，差额 {diff}）"
        )
        return {"status": "PASSED" if passed else "ABNORMAL", "message": message, "comparison": comparison}
    if left_type == "DATE":
        try:
            left_date = date.fromisoformat(str(left_value))
            right_date = date.fromisoformat(str(right_value))
        except ValueError as exc:
            raise BusinessError("RULE_VALUE_INVALID", "日期字段不是 ISO 日期格式") from exc
        passed = left_date == right_date
        comparison = _comparison(
            kind="DATE",
            left_index=0,
            right_index=1,
            left_value=left_date.isoformat(),
            right_value=right_date.isoformat(),
            expression=f"{left_date.isoformat()} {'=' if passed else '≠'} {right_date.isoformat()}",
            mode_label="同一天",
            actual_diff=f"{(left_date - right_date).days} 天",
        )
        message = (
            f"两侧日期为同一天（{left_date.isoformat()}）"
            if passed
            else f"两侧日期不是同一天（{left_date.isoformat()} 与 {right_date.isoformat()}，相差 "
            f"{(left_date - right_date).days} 天）"
        )
        return {"status": "PASSED" if passed else "ABNORMAL", "message": message, "comparison": comparison}
    # 文本、条款与布尔值按规范化后的值比较；布尔值不参与单位口径判断。
    passed = left_value == right_value
    comparison = _comparison(
        kind="TEXT",
        left_index=0,
        right_index=1,
        left_value=left_value,
        right_value=right_value,
        expression="两侧取值相等" if passed else "两侧取值不相等",
        mode_label="取值相等",
    )
    return {
        "status": "PASSED" if passed else "ABNORMAL",
        "message": "两侧取值一致" if passed else "两侧取值不一致",
        "comparison": comparison,
    }


@tool("FIELD_NOT_EQUAL")
def field_not_equal(parameters: dict[str, Any], inputs: list[dict[str, Any]]) -> dict[str, Any]:
    """字段应当不同：两侧规范化取值必须不同。"""
    result = field_equal.invoke({"parameters": parameters, "inputs": inputs})
    if result["status"] != "PASSED" and result["status"] != "ABNORMAL":
        return result
    passed = result["status"] == "ABNORMAL"
    return {
        **result,
        "status": "PASSED" if passed else "ABNORMAL",
        "message": "两侧取值不相同" if passed else "两侧取值相同",
    }


@tool("DECIMAL_COMPARE")
def decimal_compare(parameters: dict[str, Any], inputs: list[dict[str, Any]]) -> dict[str, Any]:
    """数值比较：核对单位，金额另核对币种；允许差值来自业务配置。"""
    selected, problem = _require_inputs(inputs, 2)
    if problem:
        return problem
    gap = _unit_gap(selected)
    if gap:
        return gap
    mode = parameters.get("operator")
    if mode not in DECIMAL_COMPARE_MODES:
        raise BusinessError("RULE_PARAMETER_INVALID", "金额比较方式不受支持", details={"operator": mode})
    try:
        left = _decimal_of(selected[0]["normalized_value"])
        right = _decimal_of(selected[1]["normalized_value"])
    except InvalidOperation as exc:
        raise BusinessError("RULE_VALUE_INVALID", "金额字段无法按数值比较") from exc
    # 允许差值是业务配置：相等为 |左−右| ≤ 差值；不大于为 左−右 ≤ 差值；不小于为 右−左 ≤ 差值。
    threshold = _threshold_of(parameters, selected)
    if isinstance(threshold, dict):
        return threshold
    difference = exact_subtract(left, right)
    absolute_difference = difference.copy_abs()
    reverse_difference = difference.copy_negate()
    if mode == "EQ":
        passed = left == right if threshold is None else absolute_difference <= threshold
    elif mode == "GTE":
        passed = left >= right if threshold is None else reverse_difference <= threshold
    elif mode == "LTE":
        passed = left <= right if threshold is None else difference <= threshold
    else:
        # ABS_DIFF_LTE：等价于「相等且差额不超过差值」，旧版语义原样保留。
        if threshold is None:
            raise BusinessError("RULE_PARAMETER_MISSING", "金额比较缺少业务确认阈值")
        passed = absolute_difference <= threshold
    threshold_text = None if threshold is None else str(threshold)
    mode_label = DECIMAL_MODE_LABELS.get(mode, mode)
    # 算式必须与结论一致地读出来：结论为“异常”时，算式里也要写明哪里不满足，
    # 不能出现「100 ≥ 100.03，允许差值 5」这种看起来留有余地、实际判定为异常的文字。
    if mode == "EQ":
        expression = (
            f"两侧差额 {absolute_difference}" + (f"，未超过允许差值 {threshold_text}" if threshold is not None else "")
            if passed
            else f"两侧差额 {absolute_difference}"
            + (f"，超过允许差值 {threshold_text}" if threshold is not None else "，不相等")
        )
    elif mode == "ABS_DIFF_LTE":
        expression = (
            f"两侧差额 {absolute_difference}，未超过允许差值 {threshold_text}"
            if passed
            else f"两侧差额 {absolute_difference}，超过允许差值 {threshold_text}"
        )
    elif mode == "GTE":
        if threshold is not None:
            expression = (
                f"右侧 {right} − 左侧 {left} = {reverse_difference}，"
                f"{'未超过' if passed else '超过'}允许差值 {threshold_text}"
            )
        else:
            expression = (
                f"{left} {'不小于' if passed else '小于'} {right}"
                f"（{left} − {right} = {difference}）"
            )
    else:
        if threshold is not None:
            expression = (
                f"左侧 {left} − 右侧 {right} = {difference}，"
                f"{'未超过' if passed else '超过'}允许差值 {threshold_text}"
            )
        else:
            expression = (
                f"{left} {'不大于' if passed else '大于'} {right}"
                f"（{left} − {right} = {difference}）"
            )
    comparison = _comparison(
        kind="DECIMAL",
        left_index=0,
        right_index=1,
        left_value=left,
        right_value=right,
        expression=expression,
        mode_label=mode_label,
        threshold_text=threshold_text,
        actual_diff=str(difference),
    )
    return {
        "status": "PASSED" if passed else "ABNORMAL",
        "message": (
            f"金额比较通过（{expression}）" if passed else f"金额比较异常（{expression}）"
        ),
        "comparison": comparison,
    }


@tool("DECIMAL_SUM_COMPARE")
def decimal_sum_compare(parameters: dict[str, Any], inputs: list[dict[str, Any]]) -> dict[str, Any]:
    """左侧一个字段与右侧多个加数之和比较，使用 Decimal 计算。

    输入顺序是「左侧待核对总额 + 加数…」，由编译期按 Excel 的「检查字段 / 对照内容」
    确定，执行期不重新排序、不取前两个。任何加数缺有效值都按证据不足处理：
    未知值不能当作 0，也不能因为其余加数齐全就得出结论。
    不使用 eval、Excel 公式或大模型算术。
    """
    if len(inputs) < 2:
        raise BusinessError(
            "RULE_INPUT_MISSING",
            "加总比较至少需要左侧字段与一个加数",
            details={"required": 2, "actual": len(inputs)},
        )
    problem = _unavailable_status(inputs)
    if problem:
        return problem
    gap = _unit_gap(inputs)
    if gap:
        return gap
    mode = parameters.get("operator")
    if mode not in {"EQ", "GTE", "LTE"}:
        raise BusinessError("RULE_PARAMETER_INVALID", "加总比较方式不受支持", details={"operator": mode})
    if "threshold" not in parameters:
        raise BusinessError("RULE_PARAMETER_MISSING", "加总比较缺少业务确认的允许差值")
    try:
        left = _decimal_of(inputs[0]["normalized_value"])
        addend_values = [_decimal_of(item["normalized_value"]) for item in inputs[1:]]
        total = exact_sum(addend_values)
    except (InvalidOperation, TypeError) as exc:
        raise BusinessError("RULE_VALUE_INVALID", "加总比较的字段无法按数值计算") from exc
    threshold = _threshold_of(parameters, inputs)
    if isinstance(threshold, dict):
        return threshold
    if threshold is None:
        raise BusinessError("RULE_PARAMETER_MISSING", "加总比较缺少业务确认的允许差值")
    # 相等为 |左−右| ≤ 差值；不大于为 左−右 ≤ 差值；不小于为 右−左 ≤ 差值。
    difference = exact_subtract(left, total)
    absolute_difference = difference.copy_abs()
    reverse_difference = difference.copy_negate()
    if mode == "EQ":
        passed = absolute_difference <= threshold
    elif mode == "LTE":
        passed = difference <= threshold
    else:
        passed = reverse_difference <= threshold
    addends = " + ".join(str(value) for value in addend_values)
    # 与单值比较同一口径：算式要能读出结论，不满足时写明超出允许差值。
    # 比对关系是「左侧待核对总额」对「加数合计」，措辞必须按这个方向写，
    # 否则会出现「合计未大于左侧」这种与实际判定相反的句子。
    if mode == "EQ":
        expression = (
            f"{addends} = {total}，与左侧 {left} 的差额 {absolute_difference}，未超过允许差值 {threshold}"
            if passed
            else f"{addends} = {total}，与左侧 {left} 的差额 {absolute_difference}，超过允许差值 {threshold}"
        )
    elif mode == "LTE":
        expression = (
            f"{addends} = {total}，左侧 {left} − 合计 {total} = {difference}，"
            f"{'未超过' if passed else '超过'}允许差值 {threshold}"
        )
    else:
        expression = (
            f"{addends} = {total}，合计 {total} − 左侧 {left} = {reverse_difference}，"
            f"{'未超过' if passed else '超过'}允许差值 {threshold}"
        )
    comparison = _comparison(
        kind="DECIMAL_SUM",
        left_index=0,
        right_index=-1,
        left_value=left,
        right_value=total,
        expression=expression,
        mode_label=DECIMAL_MODE_LABELS.get(mode, mode),
        threshold_text=str(threshold),
        actual_diff=str(difference),
        addends=[str(value) for value in addend_values],
    )
    return {
        "status": "PASSED" if passed else "ABNORMAL",
        "message": (
            f"加总比较通过（{expression}）"
            if passed
            else f"加总比较异常（{expression}）"
        ),
        "sum_value": str(total),
        "comparison": comparison,
    }


@tool("FIELD_NOT_EMPTY")
def field_not_empty(parameters: dict[str, Any], inputs: list[dict[str, Any]]) -> dict[str, Any]:
    """单字段非空校验（对照内容为空的检查行）。

    只判断「有效事实是否存在且非空」：抽取失败、证据不足、未执行都不是「为空」，
    也都不等于通过。未知值不能当作 0。
    """
    selected, problem = _require_inputs(inputs, 1)
    if problem:
        return problem
    value = selected[0].get("normalized_value")
    comparison = _comparison(
        kind="EXISTENCE",
        left_index=0,
        right_index=-1,
        left_value=value,
        right_value="非空",
        expression="该字段已取得非空值" if value else "该字段没有取得非空值",
        mode_label="字段非空",
    )
    if value is None or (isinstance(value, str) and not value.strip()):
        return {"status": "ABNORMAL", "message": "该字段没有取得非空值", "comparison": comparison}
    return {"status": "PASSED", "message": "该字段已取得非空值", "comparison": comparison}


@tool("DECIMAL_CN_EQUAL")
def decimal_cn_equal(parameters: dict[str, Any], inputs: list[dict[str, Any]]) -> dict[str, Any]:
    """大小写金额一致性比较。

    判定实现在 `domain/quality_judgement.decimal_cn_equal`：它是本项的唯一实现，
    工具 adapter 调的是同一个函数，避免「算子判不可判定、工具判通过」这类漂移。
    """
    return quality_judgement.decimal_cn_equal(inputs)


@tool("DATE_COMPARE")
def date_compare(parameters: dict[str, Any], inputs: list[dict[str, Any]]) -> dict[str, Any]:
    """日期比较：允许差值以天为单位；不带差值时按严格比较。"""
    selected, problem = _require_inputs(inputs, 2)
    if problem:
        return problem
    try:
        left = date.fromisoformat(str(selected[0]["normalized_value"]))
        right = date.fromisoformat(str(selected[1]["normalized_value"]))
    except ValueError as exc:
        raise BusinessError("RULE_VALUE_INVALID", "日期字段不是 ISO 日期格式") from exc
    mode = parameters.get("operator")
    if mode not in DATE_COMPARE_MODES:
        raise BusinessError("RULE_PARAMETER_INVALID", "日期比较方式不受支持", details={"operator": mode})
    # 日期允许差值以天为单位：不晚于 = 左−右 ≤ 差值；不早于 = 右−左 ≤ 差值。
    # 不带差值时保持旧版严格比较语义，阈值单位由编译期校验，不在运行时猜测。
    threshold = _threshold_of(parameters, selected, integer_unit=True)
    if isinstance(threshold, dict):
        return threshold
    if threshold is None:
        comparisons = {
            "EQ": left == right,
            "GTE": left >= right,
            "LTE": left <= right,
            "GT": left > right,
            "LT": left < right,
        }
        passed = comparisons[mode]
    else:
        days = Decimal((left - right).days)
        if mode == "LTE":
            passed = days <= threshold
        elif mode == "GTE":
            passed = -days <= threshold
        elif mode == "EQ":
            passed = abs(days) <= threshold
        elif mode == "LT":
            passed = days < 0
        else:
            passed = days > 0
    delta_days = (left - right).days
    left_text, right_text = left.isoformat(), right.isoformat()
    symbol = DATE_COMPARISON_SYMBOLS.get(mode, "?")
    # 差值方向必须写明：只给「−416 天」会让人读不出是左比右早还是晚。
    delta_text = (
        "两侧同一天"
        if delta_days == 0
        else (f"左侧比右侧早 {abs(delta_days)} 天" if delta_days < 0 else f"左侧比右侧晚 {delta_days} 天")
    )
    # 算式必须与结论一致地读出来：不满足条件时写成「超出允许差值」，
    # 不能出现「相差 9 天 ≤ 允许差值 0 天」这种看起来满足、实际判定为异常的文字。
    if threshold is None:
        expression = f"{left_text} {symbol} {right_text}（{delta_text}）"
    elif mode == "LTE":
        # 不晚于：左侧不晚于右侧即成立；左侧晚于右侧时，晚的天数不得超过允许差值。
        expression = (
            f"左侧比右侧早 {-delta_days} 天，满足“不晚于”"
            if delta_days <= 0
            else (
                f"左侧比右侧晚 {delta_days} 天，未超过允许差值 {threshold} 天"
                if delta_days <= threshold
                else f"左侧比右侧晚 {delta_days} 天，超出允许差值 {threshold} 天"
            )
        )
    elif mode == "GTE":
        # 不早于：左侧不早于右侧即成立；左侧早于右侧时，早的天数不得超过允许差值。
        expression = (
            f"左侧比右侧晚 {delta_days} 天，满足“不早于”"
            if delta_days >= 0
            else (
                f"左侧比右侧早 {-delta_days} 天，未超过允许差值 {threshold} 天"
                if -delta_days <= threshold
                else f"左侧比右侧早 {-delta_days} 天，超出允许差值 {threshold} 天"
            )
        )
    elif mode == "EQ":
        expression = (
            f"两侧相差 {abs(delta_days)} 天，未超过允许差值 {threshold} 天"
            if abs(delta_days) <= threshold
            else f"两侧相差 {abs(delta_days)} 天，超出允许差值 {threshold} 天"
        )
    else:
        expression = f"{left_text} {symbol} {right_text}（{delta_text}）"
    comparison = _comparison(
        kind="DATE",
        left_index=0,
        right_index=1,
        left_value=left_text,
        right_value=right_text,
        expression=expression,
        mode_label=DATE_MODE_LABELS.get(mode, mode),
        threshold_text=None if threshold is None else f"{threshold} 天",
        actual_diff=delta_text,
    )
    return {
        "status": "PASSED" if passed else "ABNORMAL",
        "message": f"日期比较通过（{expression}）" if passed else f"日期比较异常（{expression}）",
        "comparison": comparison,
    }


@tool("RISK_TRIGGER")
def risk_trigger(parameters: dict[str, Any], inputs: list[dict[str, Any]]) -> dict[str, Any]:
    """按当前场景的完整风险条件，由大模型判断真实来源结果是否构成风险。"""
    from ..services.llm_risk import evaluate_risk

    return evaluate_risk(parameters)


@tool("CLAUSE_ABSENT")
def clause_absent(parameters: dict[str, Any], inputs: list[dict[str, Any]]) -> dict[str, Any]:
    """条款存在性检查：条款不应存在。

    判断的是条款本身是否存在，不是「抽取到的文字是否非空」——按文字非空判断会把「抽取失败」
    与「条款确实不存在」混为一谈。取值来源必须区分：条款事实明确标注不存在（normalized_value
    为「无」/False）判合格；明确存在（「有」/True 或有原文）判不合格；尚未抽取、抽取失败、
    证据不足则无法判断，不默认通过也不默认不合格。
    """
    selected, problem = _require_inputs(inputs, 1)
    if problem:
        return problem
    item = selected[0]
    if item.get("value_type") != "CLAUSE":
        raise BusinessError("RULE_VALUE_INVALID", "条款存在性检查只能引用条款字段")
    value = item.get("normalized_value")
    if isinstance(value, bool):
        exists = value
        exists_basis = "抽取结果明确标注了条款是否存在"
    else:
        text = str(value).strip() if value is not None else ""
        normalized = text.replace(" ", "")
        if normalized in {"无", "否", "false", "False", "不存在", "未约定"}:
            exists = False
            exists_basis = "抽取结果明确标注该条款不存在"
        elif normalized in {"有", "是", "true", "True", "存在"} or text:
            exists = True
            exists_basis = "抽取结果给出了条款原文"
        else:
            return {
                "status": "EVIDENCE_INSUFFICIENT",
                "message": "条款字段没有可用的取值，无法判断该条款是否存在；抽取失败不等于条款不存在",
                "missing_inputs": [_input_label(item)],
            }
    comparison = _comparison(
        kind="CLAUSE_EXISTENCE",
        left_index=0,
        right_index=-1,
        left_value="存在" if exists else "不存在",
        right_value="不应存在",
        expression=f"抽取到「{'存在' if exists else '不存在'}」，本检查要求条款不存在",
        mode_label="条款不应存在",
    )
    comparison["basis"] = exists_basis
    if exists:
        return {
            "status": "ABNORMAL",
            "message": "该条款存在，不符合“条款不应存在”的要求",
            "clause_exists": True,
            "comparison": comparison,
        }
    return {"status": "PASSED", "message": "该条款不存在，符合要求", "clause_exists": False, "comparison": comparison}


REGISTRY: dict[str, BaseTool] = {
    handler.name: handler
    for handler in (
        field_equal,
        field_not_equal,
        date_compare,
        decimal_compare,
        decimal_sum_compare,
        decimal_cn_equal,
        field_not_empty,
        clause_absent,
        risk_trigger,
    )
}


def execute_operator(operator: str, parameters: dict[str, Any], inputs: list[dict[str, Any]]) -> dict[str, Any]:
    """按算子码分发到对应 handler；算子不在注册表时明确报错。"""
    handler = REGISTRY.get(operator)
    if not handler:
        raise BusinessError("OPERATOR_NOT_EXECUTABLE", "当前节点没有可执行的已注册算子", details={"operator": operator})
    return handler.invoke({"parameters": parameters, "inputs": inputs})
