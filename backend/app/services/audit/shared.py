"""审核链路的共享取值口径与读取辅助。

只放被多个审核模块共同使用的词汇与读取辅助，不含业务分支：材料类型状态的取值与归一
（见 `domain.audit_vocab`）；日期与金额原文的确定性标准化口径——自动抽取、人工裁决、主动纠错
三条路径必须同口径，否则同一个值会得到不同的规范值；规则的适用条件判定（适用条件自己声明
材料范围时以它为准，否则退回规则的材料范围）；证据引文的比对文本。

这些函数只读取任务与场景的当前状态，不写库、不提交事务。"""

from .. import task_csv_service

import json
import re
from ...domain.audit_vocab import MATERIAL_TYPE_REJECTED, MATERIAL_TYPE_REVIEW
from ...domain import currency, field_strategy, quality_judgement
from ...domain.manifest import EVIDENCE_SEAL_SUBJECT, applicability_view
from ...common.exceptions import BusinessError
from ...entity import AuditTask, DocumentUpload, ScenarioVersion, TaskDocument
from datetime import date
from decimal import Decimal, InvalidOperation
from sqlalchemy import select
from sqlalchemy.orm import Session
from typing import Any


REVIEW_DECISION_LABELS = {
    "CONFIRM_ABNORMAL": "确认异常",
    "MARK_NORMAL": "判定正常",
    "EVIDENCE_INSUFFICIENT": "证据不足",
    "CONFIRM_FIELD": "确认字段",
    "MODIFY_FIELD": "修改字段",
    "MARK_NOT_PRESENT": "判定字段不存在",
}

BASIS_TYPE_LABELS = {
    "HUMAN_STATEMENT": "人工业务判断",
    "DOCUMENT_EVIDENCE": "文档证据",
    "UPLOAD_RECORD": "上传记录",
}

QUOTE_CHECK_LABELS = {
    "FOUND": "已在所属材料中找到该原文",
    "VALUE_FOUND": "已在所属产物中找到字段值，但未找到完整引文",
    "NOT_FOUND": "在所属材料中找不到该原文，字段内容缺少原文依据",
    "NOT_CHECKABLE": "该产物没有可核对的原文文本，未能核对内容依据",
    "PENDING": "该证据写入时尚未按当前口径核对内容依据",
}

_DATE_TEXT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(?P<y>\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$"),
    re.compile(r"^(?P<y>\d{4})年(\d{1,2})月(\d{1,2})日?$"),
)

_CURRENCY_TOKENS: tuple[tuple[str, str], ...] = (
    ("人民币", "人民币"),
    ("CNY", "人民币"),
    ("RMB", "人民币"),
    ("￥", "人民币"),
    ("¥", "人民币"),
    ("美元", "美元"),
    ("USD", "美元"),
    ("欧元", "欧元"),
    ("€", "欧元"),
    ("EUR", "欧元"),
    ("英镑", "英镑"),
    ("£", "英镑"),
    ("GBP", "英镑"),
)

_AMOUNT_UNITS: tuple[str, ...] = ("亿元", "万元", "千元", "元")
_AMOUNT_NUMBER = r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"

# 字段原值与引文的关系不能只靠整段连续子串判断：同一组主体可能在模型原值里用“、”
# 分隔，在 OCR 引文里用“；”分隔。只对明确的列表分隔符拆分，并要求每个非空成员都
# 在引文中出现；普通句子仍按完整连续文本核对，避免把相似文本误当成证据。
_VALUE_LIST_SEPARATOR = re.compile(r"[、;；\n\r]+|(?<!\d)[,，](?!\d)")

def _fullwidth_digits(value: str) -> str:
    """全角数字与全角句点转半角：这是字符编码差异，不改变数值含义。"""
    table = {ord("０") + index: str(index) for index in range(10)}
    table[ord("．")] = "."
    table[ord("，")] = ","
    table[ord("　")] = " "
    return value.translate(table)


def _remove_surrounding_marker(matched: re.Match[str]) -> str:
    """两段数字之间的标记不能删除，否则会把多个片段拼成另一个数。"""
    if re.search(r"\d", matched.string[:matched.start()]) and re.search(r"\d", matched.string[matched.end():]):
        return matched.group(0)
    return ""


def _remove_token(value: str, token: str) -> tuple[str, bool]:
    """移除数值周围的金额标记；ASCII 币种缩写不区分大小写。"""
    flags = re.IGNORECASE if token.isascii() else 0
    replaced, count = re.subn(re.escape(token), _remove_surrounding_marker, value, flags=flags)
    return replaced, count > 0


def _amount_marker_details(value: Any, *, unit: str | None = None) -> tuple[str, set[str], set[str]]:
    """返回去除标记后的文本，以及原文明确写出的币种和单位。"""
    text = _fullwidth_digits(str(value))
    currencies: set[str] = set()
    for token, name in _CURRENCY_TOKENS:
        text, found = _remove_token(text, token)
        if found:
            currencies.add(currency.canonical_currency(name))
    units: set[str] = set()
    declared_unit = (unit or "").strip()
    unit_tokens = {*_AMOUNT_UNITS}
    if declared_unit:
        unit_tokens.add(declared_unit)
    # 数字先作为完整词元保留，避免无量纲单位“1”被从数值“31”里移除。
    # 单位按长度匹配，避免“万元”又被识别成“元”。
    unit_pattern = "|".join(re.escape(token) for token in sorted(unit_tokens, key=lambda item: (-len(item), item)))

    def remove_unit(matched: re.Match[str]) -> str:
        marker = matched.group("unit")
        if marker is None:
            return matched.group(0)
        units.add(next(token for token in unit_tokens if re.fullmatch(re.escape(token), marker, flags=re.IGNORECASE)))
        return _remove_surrounding_marker(matched)

    text = re.sub(r"\d+(?:[.,]\d+)*|(?P<unit>" + unit_pattern + ")", remove_unit, text, flags=re.IGNORECASE)
    return text, currencies, units


def _canonical_currency(value: str | None) -> str:
    """把系统明确支持的币种别名归一；未知写法保持原值，不猜测币种。"""
    return currency.canonical_currency(value)

def _parse_amount_text(value: Any, unit: str | None, currency: str | None) -> str:
    """把金额原文的明确写法解析成数值文本，口径不符时给出具体原因。

    只做确定性处理：核对千分位、去掉外围空白与字段已声明的单位，识别原文里明确写出的
    币种标记。系统不推断单位或币种、不做汇率或单位换算——原文声明的币种/单位与
    字段声明不一致时直接报冲突，由人工确认，绝不按数值相等放行。
    """
    if isinstance(value, bool):
        raise ValueError("金额字段不能使用布尔值")
    text = _fullwidth_digits(str(value)).strip()
    if not text:
        raise ValueError("金额原文为空，无法标准化")
    text, explicit_currencies, explicit_units = _amount_marker_details(text, unit=unit)
    declared_currency = _canonical_currency(currency)
    if explicit_currencies and not declared_currency:
        raise ValueError(
            f"原文写明币种“{'、'.join(sorted(explicit_currencies))}”，但字段没有声明币种，"
            "系统不能丢弃币种后保存数值"
        )
    if explicit_currencies and explicit_currencies != {declared_currency}:
        raise ValueError(
            f"原文写明的币种为“{'、'.join(sorted(explicit_currencies))}”，字段声明的币种为“{declared_currency}”，"
            "系统不做汇率换算，无法按同一口径标准化"
        )
    declared_unit = (unit or "").strip()
    if explicit_units and not declared_unit:
        raise ValueError(
            f"原文写明单位“{'、'.join(sorted(explicit_units))}”，但字段没有声明单位，"
            "系统不能丢弃单位后保存数值"
        )
    if explicit_units and explicit_units != {declared_unit}:
        raise ValueError(
            f"原文写明的单位为“{'、'.join(sorted(explicit_units))}”，字段声明的单位为“{declared_unit}”，"
            "系统不做单位换算，无法按同一口径标准化"
        )
    text = text.strip()
    if not re.fullmatch(_AMOUNT_NUMBER, text):
        raise ValueError(
            f"金额原文“{str(value).strip()}”去掉单位与币种标记后不是有效数值或千分位格式，无法标准化"
        )
    text = text.replace(",", "")
    if text.startswith("+"):
        text = text[1:]
    decimal_value = Decimal(text)
    if not decimal_value.is_finite():
        raise ValueError("金额字段必须是有限数值")
    return format(decimal_value, "f")

def _parse_decimal_text(value: Any, unit: str | None = None) -> str:
    """普通数值只去掉明确声明的外围单位，不套金额标记或做尺度换算。"""
    if isinstance(value, bool):
        raise ValueError("数值字段不能使用布尔值")
    text = _fullwidth_digits(str(value)).strip()
    marker = (unit or "").strip()
    if marker and not re.fullmatch(_AMOUNT_NUMBER, marker):
        if text.endswith(marker):
            text = text[:-len(marker)].strip()
        elif text.startswith(marker):
            text = text[len(marker):].strip()
    if not re.fullmatch(_AMOUNT_NUMBER, text):
        raise ValueError("数值原文不是有效数值或千分位格式，不能标准化")
    return format(Decimal(text.replace(",", "")), "f")


def _parse_date_text(value: Any) -> str:
    """把日期原文的明确写法解析成 ISO 日期，写法不受支持时说明支持范围。"""
    text = _fullwidth_digits(str(value)).strip()
    if not text:
        raise ValueError("日期原文为空，无法标准化")
    for pattern in _DATE_TEXT_PATTERNS:
        matched = pattern.match(text)
        if matched:
            year, month, day = (int(matched.group(index)) for index in (1, 2, 3))
            try:
                return date(year, month, day).isoformat()
            except ValueError as exc:
                raise ValueError(f"日期原文“{text}”不是有效日期：{exc}") from exc
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError(
            f"日期原文“{text}”不是可确定的日期格式；支持 YYYY-MM-DD、YYYY/M/D、YYYY.M.D 与 YYYY年M月D日"
        ) from exc

def _normalize_field_value(
    value_type: str,
    value: Any,
    *,
    unit: str | None = None,
    currency: str | None = None,
    extraction_strategy: str | None = None,
    structure_schema: dict[str, Any] | None = None,
) -> Any:
    """按字段声明的类型（金额另加声明的单位与币种）把原文标准化成可执行的值。

    原文保持原样另行保存；这里只做确定性转换，并在口径冲突时抛出带原因的
    `ValueError`。不推断单位、币种或汇率：原文写法与声明不一致时明确报冲突。
    """
    if value is None:
        raise ValueError("字段缺少可标准化的值")
    if extraction_strategy == field_strategy.STRUCTURED:
        if not isinstance(structure_schema, dict):
            raise ValueError("结构化字段缺少结构 Schema")
        return field_strategy.normalize_structured_value(value, structure_schema)
    if value_type in {"STRING", "CLAUSE"}:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("文本字段必须提供非空文本")
        return value.strip()
    if value_type == "DECIMAL_CN":
        # 大写金额只保留大写原文：换算成数值需要业务先确认单位、币种与换算口径，
        # 系统不在这里替业务做算术，也不按字符串相等冒充大小写金额核对。
        if not isinstance(value, str) or not value.strip():
            raise ValueError("大写金额字段必须提供非空文本")
        return value.strip()
    if value_type == "AMOUNT":
        return _parse_amount_text(value, unit, currency)
    if value_type == "DECIMAL":
        return _parse_decimal_text(value, unit)
    if value_type == "DATE":
        return _parse_date_text(value)
    if value_type == "BOOLEAN":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip() in {"true", "TRUE", "是"}:
            return True
        if isinstance(value, str) and value.strip() in {"false", "FALSE", "否"}:
            return False
        raise ValueError("布尔字段必须是 true、false、是或否")
    raise ValueError("字段类型不受支持")

def _has_complete_extraction_candidate(field: dict[str, Any], result: Any) -> bool:
    """模型是否给出了足够让后端独立定型的候选值和证据。"""
    strategy = field_strategy.strategy_of(field)
    if strategy == field_strategy.STRUCTURED:
        if result.normalized_value is None:
            return False
        try:
            normalized = field_strategy.normalize_structured_value(
                result.normalized_value,
                field["structure_schema"],
            )
        except ValueError:
            return False
        expected_paths = field_strategy.structured_property_keys(normalized)
        return bool(expected_paths) and expected_paths == set(result.property_evidence)
    return bool(result.raw_value and str(result.raw_value).strip() and result.evidence is not None)

def _extraction_fact_value(field: dict[str, Any], result: Any) -> tuple[str, str | None, Any, str | None]:
    """按服务端口径判定一次抽取事实的状态、原文、规范值与不通过原因。

    PRESENT 必须同时具备符合类型的原文与通过核对的证据（证据核对在调用方完成）；
    规范值由字段策略决定：文本类字段直接采用清理后的原文，数值、日期、布尔
    字段由服务端对原文重新计算。模型给的规范值只作抽取候选，不参与最终状态判定。
    即使模型把完整候选标成 EVIDENCE_UNCLEAR / NORMALIZATION_FAILED，后端仍会按同一套
    类型和证据规则重新定型；原文缺失或写法无法确定时才进入人工确认。
    """
    status = result.status
    raw_value = result.raw_value
    strategy = field_strategy.strategy_of(field)
    if status == "NOT_PRESENT":
        return status, None, None, "材料中没有抽取到该字段"
    if status == "EXTRACTION_FAILED":
        return status, raw_value, None, result.failure_reason or "字段抽取服务未能完成该字段的抽取"
    if not _has_complete_extraction_candidate(field, result):
        reason = (
            "模型没有返回可由完整属性证据支持的结构化候选值"
            if strategy == field_strategy.STRUCTURED
            else "模型没有同时返回可核对的候选原文和证据"
        )
        # 模型即使把结果标成 PRESENT，也不能绕过后端的证据门槛。
        # 缺少证据时保留原文供人工核对，但不形成可执行规范值。
        return ("EVIDENCE_UNCLEAR" if status == "PRESENT" else status), raw_value, None, reason
    if strategy == field_strategy.STRUCTURED:
        if result.normalized_value is None:
            return "NORMALIZATION_FAILED", raw_value, None, "结构化抽取结果没有返回业务 JSON"
        try:
            normalized_value = _normalize_field_value(
                field["value_type"],
                result.normalized_value,
                extraction_strategy=strategy,
                structure_schema=field.get("structure_schema"),
            )
        except ValueError as exc:
            return "NORMALIZATION_FAILED", raw_value, None, f"结构化结果不符合字段 Schema：{exc}"
        return "PRESENT", json.dumps(normalized_value, ensure_ascii=False, sort_keys=True), normalized_value, None
    if raw_value is None or not str(raw_value).strip():
        return "EVIDENCE_UNCLEAR", None, None, "抽取结果未给出原文，无法形成可核对的事实"
    try:
        normalized_value = _normalize_field_value(
            field["value_type"],
            raw_value,
            unit=field.get("unit"),
            currency=field.get("currency"),
            extraction_strategy=strategy,
            structure_schema=field.get("structure_schema"),
        )
    except (InvalidOperation, ValueError) as exc:
        return "NORMALIZATION_FAILED", raw_value, None, f"原文无法按字段类型确定性标准化：{exc}"
    # 模型的 normalized_value 可能是摘要、格式变体或错误换算。有效值只取服务端按
    # 原文和字段声明计算出的 normalized_value，模型建议值不反向改变字段状态。
    return "PRESENT", raw_value, normalized_value, None

_OCR_LATEX_WRAP = re.compile(
    r"\\(?:underline|text|textbf|textit|textrm|mathrm|mathit|emph|mbox|hbox)\s*\{([^{}]*)\}"
)

_OCR_HTML_TAG = re.compile(r"<[^>]*>")

_OCR_MARK = re.compile(r"\*+")

_OCR_HEADING = re.compile(r"(?m)^\s*#+\s*")

def _verification_text(value: str) -> str:
    """用于「引文是否有原文依据」的比对文本：保留原始引文，只抵消排版差异。

    OCR 产物是有损文本：行内公式包裹、Markdown 粗体与标题标记、HTML 图片标签、空白与换行
    都是排版产物，不是内容差异——模型引用的可读文本与这些标记逐字不相等，但内容确实来自原文。

    本函数只消除排版差异，不删除文字、不做同义替换、不做相似度匹配：「内容是否有原文依据」
    仍按规范化后的连续子串判定，找不到就是找不到。原始引文按原样保存与展示。
    """
    text = value.replace("\u200b", "").replace("\ufeff", "")
    text = _OCR_HTML_TAG.sub("", text)
    previous = None
    while previous != text:
        previous = text
        text = _OCR_LATEX_WRAP.sub(r"\1", text)
    text = text.replace("$", "")
    text = _OCR_MARK.sub("", text)
    text = _OCR_HEADING.sub("", text)
    return " ".join(text.split())

def _locatable_text(value: str) -> str:
    """原文内容的比对文本：在 `_verification_text` 基础上再去掉空白。

    排版差异在 OCR 产物里会穿行：同一句话可能被拆成多行或被块边界打断，
    引文与产物文本的空白分布因此不同。内容核对按规范化后的连续子串匹配。
    """
    return "".join(_verification_text(value).split())


def _date_candidates(value: str) -> list[date]:
    """取引文中明确写出的日期；只识别与字段标准化相同的几种确定格式。"""
    text = _fullwidth_digits(value)
    tokens = re.findall(
        r"(?<!\d)\d{4}(?:[-/.]\d{1,2}[-/.]\d{1,2}|年\d{1,2}月\d{1,2}日?)(?!\d)",
        text,
    )
    values: list[date] = []
    for token in tokens:
        try:
            values.append(date.fromisoformat(_parse_date_text(token)))
        except ValueError:
            continue
    return values


def _evidence_supports_value(
    raw_value: Any,
    quote_text: str | None,
    *,
    value_type: str | None = None,
    unit: str | None = None,
    currency: str | None = None,
) -> bool:
    """判断一个字段值是否被这段引文自身支持。

    金额、日期按字段类型做确定性等值核对，抵消千分位和日期分隔符差异；结构化属性
    中的数组或对象逐个核对有值叶子。文本仍要求连续子串或明确列表成员全部出现，
    不做相似度、同义词替换或跨引文拼接。
    """
    if raw_value is None or not isinstance(quote_text, str) or not quote_text.strip():
        return False
    if isinstance(raw_value, dict):
        values = [item for item in raw_value.values() if item not in (None, "", [], {})]
        return bool(values) and all(
            _evidence_supports_value(item, quote_text, value_type=None) for item in values
        )
    if isinstance(raw_value, (list, tuple)):
        values = [item for item in raw_value if item not in (None, "", [], {})]
        return bool(values) and all(
            _evidence_supports_value(item, quote_text, value_type=None) for item in values
        )
    normalized_type = str(value_type or "").upper()
    if normalized_type in {"DECIMAL", "AMOUNT", "NUMBER", "INTEGER"} or (
        isinstance(raw_value, (int, float, Decimal)) and not isinstance(raw_value, bool)
    ):
        try:
            expected = Decimal(_parse_amount_text(raw_value, unit, currency) if normalized_type == "AMOUNT" else _parse_decimal_text(raw_value, unit))
        except (InvalidOperation, ValueError):
            try:
                expected = Decimal(str(raw_value))
            except (InvalidOperation, ValueError):
                return False
        declared_unit = (unit or "").strip()
        declared_currency = _canonical_currency(currency)
        normalized_quote = _fullwidth_digits(quote_text)
        for matched in re.finditer(
            r"(?<![\d.])(?<!\d,)" + _AMOUNT_NUMBER + r"(?![\d.]|,\d)",
            normalized_quote,
        ):
            try:
                candidate = Decimal(matched.group(0).replace(",", ""))
            except InvalidOperation:
                continue
            if not candidate.is_finite() or candidate != expected:
                continue
            # 只看该数字附近明确写出的口径。未写标记时使用字段声明；写出了标记时
            # 必须与字段声明完全一致，不能把 100 元当成 100 万元的证据。
            context = normalized_quote[
                max(0, matched.start() - 8): min(len(normalized_quote), matched.end() + 6)
            ]
            _, explicit_currencies, explicit_units = _amount_marker_details(context, unit=unit)
            if explicit_units and explicit_units != ({declared_unit} if declared_unit else set()):
                continue
            if explicit_currencies and explicit_currencies != ({declared_currency} if declared_currency else set()):
                continue
            return True
        return False
    if normalized_type == "DATE":
        try:
            expected_date = date.fromisoformat(_parse_date_text(raw_value))
        except ValueError:
            return False
        return expected_date in _date_candidates(quote_text)
    if normalized_type == "BOOLEAN" or isinstance(raw_value, bool):
        expected = raw_value if isinstance(raw_value, bool) else str(raw_value).strip().lower() in {"true", "是"}
        tokens = {item.lower() for item in re.findall(r"(?i)\btrue\b|\bfalse\b", quote_text)}
        tokens.update(item for item in re.findall(r"(?<![\u4e00-\u9fff])[是否](?![\u4e00-\u9fff])", quote_text))
        # “结果为是 / 字段为否”是明确取值；“是否含税”只是问题描述，不能据此判 true。
        tokens.update(item for item in re.findall(r"(?:为|系)\s*([是否])", quote_text))
        return ("true" in tokens or "是" in tokens) if expected else ("false" in tokens or "否" in tokens)
    text_value = str(raw_value).strip()
    if not text_value:
        return False
    raw_locatable = _locatable_text(text_value)
    quote_locatable = _locatable_text(quote_text)
    if raw_locatable in quote_locatable:
        return True
    parts = [part.strip() for part in _VALUE_LIST_SEPARATOR.split(text_value) if part.strip()]
    return len(parts) > 1 and all(_locatable_text(part) in quote_locatable for part in parts)

def _task_field_definitions(task: AuditTask) -> dict[str, dict[str, Any]]:
    return {item["field_key"]: item for item in task.task_schema_json["fields"]}

def _scenario(db: Session, task: AuditTask) -> ScenarioVersion:
    scenario = db.get(ScenarioVersion, task.scenario_version_id)
    if not scenario:
        raise BusinessError("SCENARIO_VERSION_MISSING", "任务关联的场景版本不存在", status_code=500)
    return scenario


def _task_manifest(task: AuditTask) -> dict[str, Any]:
    """任务创建时冻结的方案配置，是执行期的配置来源。"""
    manifest = task.manifest_json
    if not isinstance(manifest, dict) or not manifest:
        raise BusinessError("TASK_MANIFEST_MISSING", "任务缺少已冻结的执行配置", status_code=500)
    return manifest

def _current_uploads(db: Session, task: AuditTask) -> list[tuple[TaskDocument, DocumentUpload | None]]:
    documents = db.scalars(select(TaskDocument).where(TaskDocument.audit_task_id == task.id).order_by(TaskDocument.id)).all()
    return [(document, db.get(DocumentUpload, document.current_upload_id) if document.current_upload_id else None) for document in documents]

def _material_names(manifest: dict[str, Any]) -> dict[str, str]:
    return {item["material_key"]: item["name"] for item in manifest["materials"]}

def _uploaded_material_keys(db: Session, task: AuditTask) -> set[str]:
    """当前上传版本里真正有文件的材料角色；不按文件名或全局最新记录推断。"""
    return {document.material_key for document, upload in _current_uploads(db, task) if upload is not None}

def _rule_applicability(rule: dict[str, Any]) -> dict[str, Any]:
    """解析规则的适用条件。

    旧版本 manifest 没有 applicability 字段时按“始终适用”处理，保证历史任务仍可执行；
    出现当前不受支持的适用条件时明确报错，不猜测适用范围。
    """
    view = applicability_view(rule)
    if view["kind"] not in {"ALWAYS", "MATERIALS_UPLOADED"}:
        raise BusinessError(
            "RULE_APPLICABILITY_UNSUPPORTED",
            "规则的适用条件不受支持，不能猜测适用范围",
            details={"rule": rule["name"], "applicability": view["kind"]},
        )
    return {
        "kind": view["kind"],
        "label": view["label"],
        # 适用条件自己声明了材料范围时以它为准，否则退回规则的材料范围。
        "material_keys": view["material_keys"] or list(rule.get("materials") or []),
    }

def _applicability_state(rule: dict[str, Any], uploaded_keys: set[str]) -> dict[str, Any]:
    """规则是否适用，以及适用条件引用的未上传材料。材料可选不等于规则不适用。

    判定实现只有一份（`domain.quality_judgement.applicability_state`）：
    Agent 工具对同一份已发布配置得出同一结论，两条路径不会各判一套。
    """
    return quality_judgement.applicability_state(_rule_applicability(rule), uploaded_keys)

def _applicability_json(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": state["kind"],
        "label": state["label"],
        "material_keys": list(state["material_keys"]),
        "missing_material_keys": list(state["missing_material_keys"]),
    }

def _not_applicable_message(state: dict[str, Any], material_names: dict[str, str]) -> str:
    missing = "、".join(material_names.get(key, key) for key in state["missing_material_keys"])
    return f"适用条件“{state['label']}”不成立：尚未上传的材料 {missing}，当前规则不适用"

def _material_input_problem(inputs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """材料依据不可用时的统一判定；全部可用时返回 None。

    与比较算子同一口径：只有人工已确认与材料位不符（TYPE_REJECTED）才算依据不可用；
    材料类型待复核（TYPE_REVIEW）不是对材料内容的否定结论，真实原文依据仍可用于审核，
    类型问题本身作为待确认事项单独暴露（见 `_material_type_review_notice`）。
    材料尚未形成处理产物 → 缺有效输入。
    语义判断（LLM_VALIDATE）不经过算子，必须在这里做同样的判断，否则同一份材料在两条路径上会得出不同结论。
    """
    rejected = sorted({item["material_key"] for item in inputs if item.get("material_state") == MATERIAL_TYPE_REJECTED})
    if rejected:
        return {
            "status": "EVIDENCE_INSUFFICIENT",
            "message": "依赖的材料类型已被人工确认与材料位不符，相关检查缺少有效依据",
            # 无法判断的具体原因：依据被人工否定，不是缺少原文。
            "blocked_reason": "EVIDENCE_REJECTED",
        }
    pending = sorted(
        {
            item["material_key"]
            for item in inputs
            if item.get("material_state") not in {None, "PREPARED", MATERIAL_TYPE_REVIEW}
        }
    )
    if pending:
        return {
            "status": "EVIDENCE_INSUFFICIENT",
            "message": "依赖材料的文件处理尚未完成，规则缺少有效输入",
            "blocked_reason": "MATERIAL_NOT_PREPARED",
        }
    return None

def _material_type_review_notice(inputs: list[dict[str, Any]], manifest: dict[str, Any]) -> str | None:
    """本次检查所依赖材料中存在「类型待复核」时的事实说明；没有时返回 None。

    这类材料位的真实原文依据仍然可用，规则照常判定；但类型本身尚未由人定案，
    必须在结果里说清「结论是照真实内容判的，材料类型这件事仍待确认」，
    既不静默通过，也不把待确认冒充成人工否定。
    """
    pending = sorted(
        {
            item["material_key"]
            for item in inputs
            if item.get("material_state") == MATERIAL_TYPE_REVIEW
        }
    )
    if not pending:
        return None
    names = "、".join(_material_names(manifest).get(key, key) for key in pending)
    return (
        f"本次检查所依据的材料（{names}）类型待复核；检查结论按材料中的真实原文得出，"
        "材料类型本身仍需人工确认。"
    )

def _subject_type_review_notice(
    db: Session,
    task: AuditTask,
    rule: dict[str, Any],
) -> str | None:
    """与本规则材料范围有关的「类型待复核」说明；没有时返回 None。

    规则没有字段输入时（例如风险触发规则）也要能说明该材料位的类型状态，
    因此按规则声明的材料范围直接取一次；范围外的材料位不在此处提及，避免把
    与这条检查无关的材料算进结论里。
    """
    scope = set(rule.get("materials") or []) | {
        item["material_key"] for item in rule.get("inputs") or []
    }
    if not scope:
        return None
    pending = sorted(
        document.material_name
        for document, upload in _current_uploads(db, task)
        if upload is not None
        and document.material_key in scope
        and document.status == MATERIAL_TYPE_REVIEW
    )
    if not pending:
        return None
    return (
        f"本次检查所依据的材料（{'、'.join(pending)}）类型待复核；"
        "材料类型本身仍需人工确认，本项结论不受自动识别影响。"
    )

def _is_subject_rule(rule: dict[str, Any]) -> bool:
    """主体/签章一致性检查。

    判定依据是编译期固化的 route（取证方式=印章主体检索）与 subject_check 标记，
    两者都来自当前场景字段配置的「产物来源=印章识别」声明，不是字段中文名里
    是否含「签章主体」几个字：中文名可以改，靠名字认规则会接错证据源。

    这类检查不做程序判定：检索只返回候选，结论由工作人员确认。
    """
    route = rule.get("route") if isinstance(rule.get("route"), dict) else {}
    if route.get("evidence") == EVIDENCE_SEAL_SUBJECT:
        return True
    if rule.get("operator") not in {"FIELD_EQUAL", "FIELD_NOT_EQUAL"}:
        return False
    # 兼容既有已发布配置：它们在编译时先写 route 后写 subject_check，
    # route 里没有 SEAL_SUBJECT_RETRIEVAL；标记本身是可靠依据。
    return rule.get("subject_check") is True

SUBJECT_ARTIFACT_SOURCE = "SEAL"

def _subject_query(
    rule: dict[str, Any],
    inputs: list[dict[str, Any]],
    *,
    artifact_source_by_field: dict[str, str | None] | None = None,
) -> str:
    """待核对主体的当前有效值。

    主体/签章一致性核对按产物来源区分输入：待核对主体来自材料正文的字段抽取，
    印章主体候选来自印章识别产物。用印章字段去检索印章自身没有意义，因此查询字段
    按「产物来源」声明确定，而不是取第一个有效输入：有且仅有一个输入的产物来源是 SEAL 时，
    待核对主体取另一个输入；全部输入都声明为 SEAL 时无法确定，明确报错；没有声明产物来源时
    退化为原有的「取第一个有有效值的输入」，与旧配置语义一致。

    没有有效值时明确报错：主体检索没有可查的对象，不能退化成用规则名称或备注去搜相似文本。
    """
    sources = artifact_source_by_field or {}
    query_item: dict[str, Any] | None = None
    if inputs:
        seal_flags = [sources.get(str(item.get("field_key") or "")) == SUBJECT_ARTIFACT_SOURCE for item in inputs]
        if any(seal_flags) and not all(seal_flags):
            query_item = next(item for item, is_seal in zip(inputs, seal_flags) if not is_seal)
        elif all(seal_flags):
            raise BusinessError(
                "SUBJECT_QUERY_UNRESOLVED",
                f"规则“{rule['name']}”的输入字段全部取自印章识别产物，"
                "无法确定哪一侧是待核对主体；请为该规则指定一个来自材料正文的待核对主体字段",
                details={"rule": rule["name"], "field_keys": [item.get("field_key") for item in inputs]},
            )
    candidates = [query_item] if query_item is not None else list(inputs)
    for item in candidates:
        if item.get("status") in {"PRESENT", "REVIEWED"} and item.get("normalized_value") is not None:
            value = str(item["normalized_value"]).strip()
            if value:
                return value
    raise BusinessError(
        "SUBJECT_QUERY_UNAVAILABLE",
        f"规则“{rule['name']}”的待核对主体当前没有有效取值，无法检索印章主体；"
        "请先完成字段抽取或人工确认该主体后重试",
        details={"rule": rule["name"]},
    )
