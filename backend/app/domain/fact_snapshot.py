"""事实快照：一次规则评估所依据的**输入事实集合**的冻结内容与身份。

为什么需要它
------------
规则评估开始时必须冻结实际输入的事实集合。评估期间如果每次工具调用都去读
「最新事实」，同一条规则的两次工具调用可能基于不同的数——先读到 1000，
中途有人改了字段，后一次读到 1200，两次比较得到的结论无法解释。

因此评估开始时算出**内容指纹**并**保存冻结内容本身**（`evaluation_snapshot`）：

- 指纹落进 `agent_tool_call.fact_snapshot_hash`，恢复时与原记录比对，
  不一致即协议冲突（见 `services/agent_tool_ledger`）；
- 冻结内容落进 `evaluation_snapshot.facts_json`，工具执行只从它解析事实。

**只有指纹是不够的**：指纹对得上而数据已变的情况确实存在（例如指纹只覆盖事实、
而工具额外读了材料记录）。因此快照同时冻结：

- 事实（字段事实 / 统一事实）：评估真正依据的取值；
- 材料位身份：材料位、必传属性、当前上传的 id 与内容哈希、材料状态、修订号。
  工具确实需要读取文件或产物时，必须核对这些引用、hash 与修订号与快照一致。

两个身份必须分开
----------------
「内容指纹」和「快照身份」是两件事，混在一起会让第二次冻结必然撞唯一键：

- `hash` 只由**事实与材料的内容**算出。同一份内容用于两条不同规则时，hash 相同
  是正常的，也是需要的（它回答「这两次评估依据的是不是同一批数」）；
- `ref` 必须同时包含**评估作用域身份**（评估批次、规则实例、输入修订），
  它是 `evaluation_snapshot` 的唯一键与恢复入口。两条规则即使内容一模一样，
  作用域不同就是两份快照，不能互相顶掉。

`ref` 的格式是 `facts:<条数>:<作用域摘要12位>:<内容摘要12位>`。两段摘要各自
截断到 12 位十六进制，因此总长有明确上限（≤ 40 字符），不会因为拼接长字符串
超出数据库字段长度。

指纹怎么算
----------
对「本次参与评估的事实」逐条取 (事实编号, 材料位, 期间口径, 报表口径, 主体范围,
版本号, 规范值, 单位, 币种)，加上材料位身份，按确定性顺序拼接后取 SHA-256。

刻意**不**包含 `updated_at` 之类的时间戳：同一个事实集合在任何时刻都必须得到
同一个指纹，否则恢复时会把「同一次评估」误判成事实已变。

也不包含事实的数据库 id：id 是物理主键，重建数据或迁移后可能变化，
而「哪些事实、什么版本、什么值」才是评估真正依赖的内容。

JSON 安全投影
-------------
冻结内容要落进 JSON 列，而统一事实的取值是 `Decimal` / `date`。**指纹与落库
共用同一份投影**，否则会出现「指纹看到一个值、落库保存另一个值」：

- `Decimal` → `{"kind": "decimal", "value": "<十进制字符串>"}`（绝不转 float：
  float 的二进制误差会让同一个数算出不同指纹）；
- `date` / `datetime` → `{"kind": "date"/"datetime", "value": "<ISO 字符串>"}`；
- 其余标量原样保留（`str` / `bool` / `int` / `None`）。

读取方用 `decode_value` 还原：`decimal` 还原成 `Decimal`，日期还原成 `date` /
`datetime`。取不到的字段统一成 `None`，不用空串代替——空串与「没有这个维度」
是两回事。无法投影的类型**显式报错**，不静默丢弃。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from ..common.exceptions import BusinessError
from . import field_strategy

# 快照口径版本：当前口径冻结统一事实、完整属性证据和规范化金额；
# 拼接字段或语义变化时必须递增，避免旧指纹被按新口径解释。
FACT_SNAPSHOT_VERSION = "8"

# 作用域摘要与内容摘要在 ref 里的截断长度。两者相加 + 固定前缀与分隔符
# 不超过 40 字符，远小于 snapshot_ref 的字段长度上限。
REF_DIGEST_CHARS = 12

# 被显式编码的标量类型。`kind` 取值即这几个，解码时据此还原。
ENCODED_KINDS = ("decimal", "date", "datetime")
_ENVELOPE_KEYS = frozenset({"kind", "value"})

def encode_value(value: Any) -> Any:
    """把一个取值投影成 JSON 可表达、且可无损还原的形式。

    这是**唯一**的投影实现：指纹与落库都走它，解码由 `decode_value` 负责。
    无法投影的类型显式报错——静默丢弃会让「指纹里没有这个维度」与
    「这个维度取不到值」变得无法区分。
    """
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Decimal):
        # 十进制字符串：保留精度与原值，绝不经过 float。
        return {"kind": "decimal", "value": format(value, "f")}
    if isinstance(value, datetime):
        return {"kind": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"kind": "date", "value": value.isoformat()}
    if isinstance(value, (list, tuple)):
        return [encode_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): encode_value(item) for key, item in value.items()}
    raise BusinessError(
        "FACT_SNAPSHOT_VALUE_UNENCODABLE",
        "事实取值无法投影成 JSON 安全形式，冻结快照未写入",
        status_code=500,
        details={"value_type": type(value).__name__},
    )


def decode_value(value: Any) -> Any:
    """`encode_value` 的逆运算：还原成 Python 取值。

    只认「恰好由 kind 与 value 两个键组成、且 kind 是已编码类型」的字典；
    其余字典按业务数据原样返回，不做任何解释。
    """
    if isinstance(value, list):
        return [decode_value(item) for item in value]
    if isinstance(value, dict):
        if set(value) == _ENVELOPE_KEYS and value.get("kind") in ENCODED_KINDS:
            return _decode_scalar(str(value["kind"]), value.get("value"))
        return {key: decode_value(item) for key, item in value.items()}
    return value


def _decode_scalar(kind: str, raw: Any) -> Any:
    """解码一个被显式编码的标量。

    编码内容损坏（不是字符串、或不是合法数值/日期）时显式报错：
    用 `None` 顶替会让「解析失败」看起来像「这次没有取值」。
    """
    if not isinstance(raw, str):
        raise BusinessError(
            "FACT_SNAPSHOT_VALUE_INVALID",
            "冻结快照里的取值不是可解析的文本，无法还原",
            status_code=500,
            details={"kind": kind},
        )
    if kind == "decimal":
        try:
            return Decimal(raw)
        except ArithmeticError as exc:
            raise BusinessError(
                "FACT_SNAPSHOT_VALUE_INVALID",
                "冻结快照里的十进制取值无法还原",
                status_code=500,
                details={"kind": kind},
            ) from exc
    if kind == "date":
        try:
            return date.fromisoformat(raw)
        except ValueError as exc:
            raise BusinessError(
                "FACT_SNAPSHOT_VALUE_INVALID",
                "冻结快照里的日期取值无法还原",
                status_code=500,
                details={"kind": kind},
            ) from exc
    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise BusinessError(
            "FACT_SNAPSHOT_VALUE_INVALID",
            "冻结快照里的时间取值无法还原",
            status_code=500,
            details={"kind": kind},
        ) from exc


def scope_of(*, evaluation_id: str, rule_instance_id: str, input_revision: int) -> dict[str, Any]:
    """快照的作用域身份：哪一次评估、哪一条规则实例、哪一版输入。

    它进入 `ref`，因此同一份事实内容用于不同规则时不会互相顶掉唯一键。
    """
    return {
        "evaluation_id": evaluation_id,
        "rule_instance_id": rule_instance_id,
        "input_revision": int(input_revision),
    }


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fact_identity(item: dict[str, Any]) -> dict[str, Any]:
    """一条事实参与指纹的身份字段。

    只取「评估真正依赖的内容」：编号、口径维度、版本、值与单位币种。
    取值一律来自已经过 JSON 安全投影的字段，指纹与落库因此共用同一份口径。
    """
    return {
        "fact_code": item.get("fact_code"),
        "field_key": item.get("field_key"),
        "fact_kind": item.get("fact_kind"),
        "material_key": item.get("material_key"),
        "period_scope": item.get("period_scope"),
        "statement_scope": item.get("statement_scope"),
        "entity_scope": item.get("entity_scope"),
        "revision_no": item.get("revision_no"),
        "value_type": item.get("value_type"),
        "decimal_value": item.get("decimal_value"),
        "text_value": item.get("text_value"),
        "date_value": item.get("date_value"),
        "boolean_value": item.get("boolean_value"),
        "unit": item.get("unit"),
        "currency": item.get("currency"),
        # 工具和模型实际读取这些字段。值相同但状态或证据不同，属于不同输入快照。
        "status": item.get("status"),
        "raw_value": item.get("raw_value"),
        "normalized_value": item.get("normalized_value"),
        "evidence_id": item.get("evidence_id"),
        "evidence_ids": item.get("evidence_ids") or [],
    }


def _material_identity(item: dict[str, Any]) -> dict[str, Any]:
    """一个材料位参与指纹的身份字段。

    上传引用（id 与内容哈希）必须在指纹里：工具读文件或产物时依据的就是它们，
    换了文件而指纹不变，等于让「这次调用依据的是哪份文件」失去约束。
    """
    return {
        "material_key": item.get("material_key"),
        "required": bool(item.get("required")),
        "document_status": item.get("document_status"),
        "material_state": item.get("material_state"),
        "upload_id": item.get("upload_id"),
        "upload_sha256": item.get("upload_sha256"),
        "upload_revision": item.get("upload_revision"),
    }


def snapshot_of(
    facts: list[dict[str, Any]],
    materials: list[dict[str, Any]] | None = None,
    *,
    scope: dict[str, Any],
) -> dict[str, Any]:
    """一组事实与材料位身份的快照身份。

    返回 `ref`（可读引用，供页面、日志与恢复入口）、`hash`（内容指纹，供恢复比对）、
    `scope_hash`（作用域摘要）与条数。

    `ref` 同时包含作用域摘要与内容摘要：`ref` 的唯一性由作用域保证，
    `hash` 的可比性由内容保证，两者不再互相顶替。
    """
    normalized_facts = sorted(
        (_fact_identity(item) for item in facts),
        key=_canonical,
    )
    normalized_materials = sorted(
        (_material_identity(item) for item in (materials or [])),
        key=_canonical,
    )
    content = json.dumps(
        {
            "version": FACT_SNAPSHOT_VERSION,
            "facts": normalized_facts,
            "materials": normalized_materials,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    scope_digest = hashlib.sha256(_canonical(scope).encode("utf-8")).hexdigest()
    return {
        "ref": (
            f"facts:{len(normalized_facts)}:"
            f"{scope_digest[:REF_DIGEST_CHARS]}:{digest[:REF_DIGEST_CHARS]}"
        ),
        "hash": digest,
        "scope_hash": scope_digest,
        "count": len(normalized_facts),
        "material_count": len(normalized_materials),
        "version": FACT_SNAPSHOT_VERSION,
    }


def snapshot_payload(
    facts: list[dict[str, Any]],
    materials: list[dict[str, Any]],
    *,
    task_no: str,
    node_code: str,
    evaluation_id: str,
    rule_instance_id: str,
    input_revision: int,
) -> dict[str, Any]:
    """写进 `evaluation_snapshot.facts_json` 的冻结内容。

    结构明确分成三段，读取方按段取用，不靠猜键：

    - `facts`：参与评估的事实本身（含取值），工具按身份引用从中解析输入；
    - `materials`：材料位身份与上传引用，工具读文件或产物前必须与它对账；
    - `scope`：这份快照属于哪个任务、哪条规则实例、哪一版输入。
    """
    return {
        "version": FACT_SNAPSHOT_VERSION,
        "scope": {
            "task_no": task_no,
            "node_code": node_code,
            "evaluation_id": evaluation_id,
            "rule_instance_id": rule_instance_id,
            "input_revision": int(input_revision),
        },
        "facts": facts,
        "materials": materials,
    }


def resolve_rule_inputs(
    facts: list[dict[str, Any]],
    rule_inputs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """从冻结事实里按身份引用解析一条规则的输入。

    与 `services/audit/facts._field_inputs` 的**输出形状相同**（这也是两者能互换的前提），
    区别只有取数来源：这里只读冻结快照，不查当前数据。冻结快照里没有的事实
    如实返回 `NOT_EXECUTED` 与空取值——缺输入是事实，不由这里补默认值。

    **两种事实共用这一份解析**，不为合同字段事实与财报统一事实各写一套：

    - 合同字段事实的 `field_key` 就是字段身份（`fld_…`）；
    - 财报统一事实的 `fact_code` 是业务编号（`FEX-…` / 计算结果编号），
      快照已按编译期索引把它翻译成 `field_key`；翻译不到时退回业务编号，
      因此两条口径都能被引用到，且不需要调用方判断走哪条分支。

    `rule_inputs` 是规则声明的 (field_key, material_key) 引用清单，顺序即调用顺序。
    """
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for item in facts:
        key = _identity_of_fact(item)
        if key[0]:
            index.setdefault(key, item)
    resolved: list[dict[str, Any]] = []
    for item in rule_inputs:
        field_key = str(item.get("field_key") or "")
        material_key = item.get("material_key")
        fact = index.get((field_key, material_key))
        common = {
            "field_key": field_key,
            "material_key": material_key,
            "value_type": (fact or {}).get("value_type"),
            "unit": (fact or {}).get("unit"),
            "currency": (fact or {}).get("currency"),
            "material_state": (fact or {}).get("material_state"),
        }
        if fact is None:
            resolved.append(
                {
                    **common,
                    "status": "NOT_EXECUTED",
                    "raw_value": None,
                    "normalized_value": None,
                    "evidence_id": None,
                    "evidence_ids": [],
                }
            )
            continue
        resolved.append(
            {
                **common,
                "status": fact.get("status"),
                # 取值按编码还原成 Python 值：金额是 Decimal（绝不经过 float），
                # 日期是 date。工具与算子读到的是同一形状，不因事实来自哪张表而不同。
                "raw_value": decode_value(fact.get("raw_value")),
                "normalized_value": decode_value(fact.get("normalized_value")),
                "evidence_id": fact.get("evidence_id"),
                "evidence_ids": list(fact.get("evidence_ids") or []),
            }
        )
    return resolved


def _identity_of_fact(item: dict[str, Any]) -> tuple[str, Any]:
    """一条事实在解析输入时的身份键。字段身份优先，缺失时退回业务编号。"""
    return (
        str(item.get("field_key") or item.get("fact_code") or ""),
        item.get("material_key"),
    )


def declared_inputs_of(rule: dict[str, Any]) -> list[dict[str, Any]]:
    """规则声明的输入引用清单。只保留身份键，不把规则里的其它配置带出去。"""
    return [
        {"field_key": item.get("field_key"), "material_key": item.get("material_key")}
        for item in (rule.get("inputs") or [])
        if isinstance(item, dict) and item.get("field_key")
    ]


def facts_of_task_fact_rows(
    rows: list[Any],
    *,
    field_code_index: dict[str, str] | None = None,
    field_definitions: dict[str, dict[str, Any]] | None = None,
    material_states: dict[str, str | None] | None = None,
) -> list[dict[str, Any]]:
    """把 `task_fact` 的 ORM 行投影成快照输入（财报统一事实路径）。

    统一事实把取值按类型分存在 `decimal_value` / `text_value` / `date_value` 三个列里，
    没有 `normalized_value` 这个列。这里按 `value_type` 把三个列归一成
    **同一个 `normalized_value`**：Agent 因此拿得到真实取值，而不是一个恒为空的字段。
    金额仍按 `Decimal` 编码（十进制字符串），不经过 float。

    `field_code_index` 是编译期就确定的「业务编号 → 字段身份」索引：统一事实的
    `fact_code` 是业务编号，而规则输入引用的是字段身份，两者必须在冻结时对上。
    索引里没有的编号（例如计算结果编号）退回业务编号本身，并保留 `fact_kind`
    以便读取方区分抽取事实与派生事实。

    `task_fact` 没有单独的「抽取原文」列：文本类事实的原文就是 `text_value`，
    数值与日期事实只有定型取值，`raw_value` 如实为 `None`——不拿规范值冒充原文。
    """
    index = field_code_index or {}
    states = material_states or {}
    facts: list[dict[str, Any]] = []
    for row in rows:
        executable = (
            row.status in {"PRESENT", "REVIEWED"}
            if row.fact_kind == "EXTRACTED"
            else row.fact_kind == "DERIVED" and row.status == "COMPUTED"
        )
        field_key = index.get(str(row.fact_code), str(row.fact_code))
        normalized = (
            task_fact_value(row, field_definition=(field_definitions or {}).get(field_key))
            if executable
            else None
        )
        decimal_projection = None
        if executable and row.value_type == "DECIMAL":
            if field_definitions and field_key in field_definitions:
                # 统一事实的执行值已经按字段契约归一成十进制文本；快照指纹也必须使用
                # 这个业务口径，不能把数据库 DECIMAL 的固定 scale 当成业务差异。
                decimal_projection = encode_value(Decimal(str(normalized))) if normalized is not None else None
            else:
                decimal_projection = encode_value(row.decimal_value)
        facts.append(
        {
            "fact_code": row.fact_code,
            "field_key": field_key,
            "fact_kind": row.fact_kind,
            "material_key": row.material_key,
            "period_scope": row.period_scope,
            "statement_scope": row.statement_scope,
            "entity_scope": row.entity_scope,
            "revision_no": row.revision_no,
            "value_type": row.value_type,
            "decimal_value": decimal_projection,
            "text_value": encode_value(row.text_value) if executable else None,
            "date_value": encode_value(row.date_value) if executable else None,
            "boolean_value": row.boolean_value if executable else None,
            "unit": row.unit,
            "currency": row.currency,
            # 以下键供工具解析输入，不参与指纹（见 _fact_identity）。
            "status": row.status,
            "material_state": states.get(row.material_key) if row.material_key else None,
            "raw_value": encode_value(row.text_value) if row.text_value is not None else None,
            "normalized_value": encode_value(normalized),
            "evidence_id": None,
            "evidence_ids": [],
        }
        )
    return facts


def task_fact_value(row: Any, *, field_definition: dict[str, Any] | None = None) -> Any:
    """统一事实的可执行值：按字段类型从定型列还原，结构化字段还原为业务 JSON。

    这是规则、计算快照和 Agent 共用的读取口径。三类标量列都取不到时返回
    `None`，不补 0、不补空串；结构化 JSON 损坏或不再符合冻结 Schema 时明确
    报错，不能把损坏的 JSON 文本继续交给规则执行。
    """
    if field_definition is not None and field_strategy.strategy_of(field_definition) == field_strategy.STRUCTURED:
        if row.text_value is None:
            return None
        schema = field_definition.get("structure_schema")
        if not isinstance(schema, dict):
            raise BusinessError(
                "TASK_FACT_STRUCTURE_SCHEMA_MISSING",
                "结构化统一事实缺少字段 Schema，无法还原可执行值",
                status_code=500,
                details={"fact_code": row.fact_code, "field_key": field_definition.get("field_key")},
            )
        try:
            return field_strategy.normalize_structured_value(row.text_value, schema)
        except ValueError as exc:
            raise BusinessError(
                "TASK_FACT_STRUCTURED_VALUE_INVALID",
                "结构化统一事实不符合字段 Schema，无法参与执行",
                status_code=500,
                details={"fact_code": row.fact_code, "field_key": field_definition.get("field_key")},
            ) from exc
    value_type = str(row.value_type or "")
    if value_type == "DECIMAL":
        if row.decimal_value is None:
            return None
        # 字段执行契约一直使用 JSON 安全的十进制字符串；派生事实留在内部快照时
        # 保留 Decimal，避免计算精度丢失。
        return canonical_field_value(row.decimal_value, field_definition) if field_definition is not None else row.decimal_value
    if value_type == "DATE":
        if row.date_value is None:
            return None
        return row.date_value.isoformat() if field_definition is not None else row.date_value
    if value_type == "BOOLEAN":
        return row.boolean_value
    return row.text_value


def canonical_field_value(value: Any, field_definition: dict[str, Any]) -> Any:
    """把候选层与统一事实层的同一字段值归一成可直接比较的业务值。"""
    if value is None:
        return None
    if field_strategy.strategy_of(field_definition) == field_strategy.STRUCTURED:
        schema = field_definition.get("structure_schema")
        if not isinstance(schema, dict):
            raise BusinessError(
                "TASK_FACT_STRUCTURE_SCHEMA_MISSING",
                "结构化字段缺少字段 Schema，无法比较候选值与统一事实",
                status_code=500,
                details={"field_key": field_definition.get("field_key")},
            )
        try:
            return field_strategy.normalize_structured_value(value, schema)
        except ValueError as exc:
            raise BusinessError(
                "TASK_FIELD_VALUE_INVALID",
                "字段候选值不符合冻结的结构 Schema",
                status_code=500,
                details={"field_key": field_definition.get("field_key")},
            ) from exc
    value_type = str(field_definition.get("value_type") or "")
    if value_type == "DECIMAL":
        try:
            decimal_value = value if isinstance(value, Decimal) else Decimal(str(value).strip())
        except (InvalidOperation, ValueError) as exc:
            raise BusinessError(
                "TASK_FIELD_VALUE_INVALID",
                "金额字段候选值不是有效十进制数",
                status_code=500,
                details={"field_key": field_definition.get("field_key"), "value": str(value)},
            ) from exc
        if not decimal_value.is_finite():
            raise BusinessError(
                "TASK_FIELD_VALUE_INVALID",
                "金额字段候选值必须是有限十进制数",
                status_code=500,
                details={"field_key": field_definition.get("field_key"), "value": str(value)},
            )
        decimal_text = format(decimal_value, "f")
        if "." in decimal_text:
            decimal_text = decimal_text.rstrip("0").rstrip(".")
        return "0" if decimal_text == "-0" else decimal_text
    if value_type == "DATE":
        if type(value) is date:
            return value.isoformat()
        try:
            return date.fromisoformat(str(value).strip()).isoformat()
        except ValueError as exc:
            raise BusinessError(
                "TASK_FIELD_VALUE_INVALID",
                "日期字段候选值不是有效 ISO 日期",
                status_code=500,
                details={"field_key": field_definition.get("field_key"), "value": str(value)},
            ) from exc
    if value_type == "BOOLEAN":
        if not isinstance(value, bool):
            raise BusinessError(
                "TASK_FIELD_VALUE_INVALID",
                "布尔字段候选值不是布尔值",
                status_code=500,
                details={"field_key": field_definition.get("field_key"), "value": str(value)},
            )
        return value
    return value.strip() if isinstance(value, str) else value


def facts_of_field_value_rows(
    rows: list[Any],
    *,
    field_definitions: dict[str, dict[str, Any]],
    material_states: dict[str, str | None] | None = None,
    evidence_ids_by_field_value: dict[int, list[int]] | None = None,
) -> list[dict[str, Any]]:
    """把 `task_field_value` 的 ORM 行投影成快照输入（合同审核路径）。

    字段事实没有期间/报表口径：它们按 (字段, 材料位) 定位，期间与报表口径留空。
    这不是「缺维度」，而是这条事实本身不带这些维度。

    投影同时带上工具解析输入所需的字段：冻结内容
    既是指纹的输入，也是运行期解析 `inputs` 的唯一来源，两者不能各投影一份。
    类型、单位和币种取自任务创建时冻结的字段定义，不由事实行推断。
    """
    states = material_states or {}
    evidence_ids = evidence_ids_by_field_value or {}
    facts: list[dict[str, Any]] = []
    for row in rows:
        field = field_definitions.get(row.field_key)
        if field is None or field.get("material_key") != row.material_key:
            raise BusinessError(
                "TASK_SCHEMA_FIELD_MISSING",
                "当前字段事实不在任务冻结的字段清单中，无法生成事实快照",
                status_code=500,
                details={"field_key": row.field_key, "material_key": row.material_key},
            )
        normalized = (
            canonical_field_value(row.normalized_value_json, field)
            if row.status in {"PRESENT", "REVIEWED"}
            else None
        )
        facts.append(
            {
                "fact_code": row.field_key,
                "field_key": row.field_key,
                "fact_kind": "EXTRACTED",
                "material_key": row.material_key,
                "period_scope": None,
                "statement_scope": None,
                "entity_scope": None,
                "revision_no": row.revision_no,
                "value_type": field["value_type"],
                "decimal_value": None,
                # 字段事实的规范值在 normalized_value_json 里；原文与规范值分开保存，
                # 指纹取规范值——判定依据的是规范值。
                "text_value": encode_value(normalized),
                "date_value": None,
                "unit": field.get("unit"),
                "currency": field.get("currency"),
                # 以下键供工具解析输入，不参与指纹（见 _fact_identity）
                "status": row.status,
                "material_state": states.get(row.material_key),
                "raw_value": row.raw_value,
                "normalized_value": encode_value(normalized),
                "evidence_id": row.evidence_id,
                "evidence_ids": sorted(set(evidence_ids.get(row.id) or [])),
            }
        )
    return facts
