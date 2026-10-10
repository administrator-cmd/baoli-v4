import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError

from .proposals import ProposalBundle, UnsupportedCapability
from . import calculation as calc
from . import capabilities, field_strategy, file_quality, node_registry, scenario_schema, workflow_dag
from .currency import canonical_currency
from .audit_vocab import SCOPE_NOT_APPLICABLE
from ..common.exceptions import BusinessError
from .identity import stable_key
from . import tool_catalog
# 参数契约常量定义在叶子模块 `parameter_contract`：工具目录要据此生成参数
# JSON Schema，而工具目录不能反向导入本模块（会形成
# manifest → capabilities → tool_catalog → manifest 的环）。
from .parameter_contract import (
    CONFIG_STATUS_DISABLED,
    CONFIG_STATUS_PENDING,
    DATE_COMPARE_OPERATORS,
    DATE_THRESHOLD_UNIT,
    DECIMAL_COMPARE_OPERATORS,
    DECIMAL_SUM_COMPARE_OPERATORS,
    DOCUMENT_ARTIFACT_TYPES,
    REPORT_CONTENT_TYPES,
    REPORT_RESULT_SORTS,
)


NODE_DEFINITIONS = [
    ("MATERIAL_CHECK", "材料检查"),
    ("DOCUMENT_PREPARE", "文件处理"),
    ("FIELD_EXTRACTION", "字段抽取"),
    ("INTERNAL_VALIDATE", "文件内部校验"),
    ("CROSS_VALIDATE", "文件交叉校验"),
    ("MANUAL_REVIEW", "人工复核"),
    ("RISK_RATING", "风险评定"),
    ("REPORT_OUTPUT", "报告输出"),
]

# 节点类型常量：从节点注册表派生，不在本模块另写一份名单。
NODE_TYPE_CALCULATION = node_registry.NODE_TYPE_CALCULATION
NODE_TYPE_MATERIAL_CHECK = node_registry.NODE_TYPE_MATERIAL_CHECK
NODE_TYPE_FIELD_EXTRACTION = node_registry.NODE_TYPE_FIELD_EXTRACTION
NODE_TYPE_INTERNAL_VALIDATE = node_registry.NODE_TYPE_INTERNAL_VALIDATE
NODE_TYPE_CROSS_VALIDATE = node_registry.NODE_TYPE_CROSS_VALIDATE
NODE_TYPE_MANUAL_REVIEW = node_registry.NODE_TYPE_MANUAL_REVIEW
NODE_TYPE_RISK_RATING = node_registry.NODE_TYPE_RISK_RATING
NODE_TYPE_REPORT_OUTPUT = node_registry.NODE_TYPE_REPORT_OUTPUT

# 算子能挂在哪个节点：从能力目录（domain.capabilities）派生，不在这里另维护一份名单。
# 两份名单漂移过一次（后端 12 个算子、页面下拉 7 个），因此这里只做引用。
# 返回类型是 frozenset：调用方只做成员判断，不原地修改。
OPERATOR_NODE_CONSTRAINTS: dict[str, frozenset[str]] = capabilities.NODE_CONSTRAINTS

# 参数契约常量见文件顶部从 `parameter_contract` 的导入（叶子模块，避免成环）。
# 历史条件码仅用于展示标签；当前条件可为完整业务原文，不限制为这两种。
RISK_CONDITION_LABELS = {
    "ANY_ABNORMAL": "任一关联检查不合格",
    "ALL_ABNORMAL": "全部关联检查不合格",
}
# 风险疑点的来源节点：业务必须显式声明这条风险监视的是哪一类审核规则，
# 历史来源能力标签，只与来源规则的实际节点类型核对，不限制来源工作表。
SOURCE_NODE_LABELS = {
    "MATERIAL_QUALITY": "材料质量",
    "INTERNAL_VALIDATE": "文件内部校验",
    "CROSS_VALIDATE": "文件交叉校验",
}
# 来源节点 → 审核节点代码集合：来源规则的实际节点必须落在这个集合里。
# 「材料质量」标签覆盖两类实际节点：
# 「材料是否齐全」= MATERIAL_EXISTS 在材料检查节点，「文件质量检查」= FILE_QUALITY_CHECK 在文件处理节点。
# 因此这里必须是集合，否则文件质量类风险（例如缺页、缺章）无法声明来源。
SOURCE_NODE_RULE_NODES: dict[str, frozenset[str]] = {
    "MATERIAL_QUALITY": frozenset({"MATERIAL_CHECK", "DOCUMENT_PREPARE"}),
    "INTERNAL_VALIDATE": frozenset({"INTERNAL_VALIDATE"}),
    "CROSS_VALIDATE": frozenset({"CROSS_VALIDATE"}),
}
# 允许为空、需要显式跳过并说明原因的业务校验节点。
OPTIONAL_CHECK_NODES = ("INTERNAL_VALIDATE", "CROSS_VALIDATE", "RISK_RATING")

# 算子与比较方式的业务中文口径，供页面展示"实际比较对象和比较方式"。
# 从能力目录派生：中文标签只维护一份，改文案不会让页面与后端各说一套。
OPERATOR_LABELS = capabilities.OPERATOR_LABELS

DATE_COMPARISON_LABELS = {"EQ": "同一天", "GTE": "不早于", "LTE": "不晚于", "GT": "晚于", "LT": "早于"}
DECIMAL_COMPARISON_LABELS = {"EQ": "等于", "GTE": "不小于", "LTE": "不大于", "ABS_DIFF_LTE": "绝对差不超过"}
DECIMAL_SUM_COMPARISON_LABELS = {"EQ": "等于", "GTE": "不小于", "LTE": "不大于"}
APPLICABILITY_LABELS = {"ALWAYS": "始终适用", "MATERIALS_UPLOADED": "所引用材料均已上传"}
# 旧确定性算子的取数能力识别词；具体适用范围由 detect_unsupported_capability_for_operator 决定。
# LLM_VALIDATE 支持基于已提供输入汇总、分组和常量比较，仅保留外部查询缺口识别。
UNSUPPORTED_CAPABILITY_PATTERNS: list[tuple[str, str, str]] = [
    ("SUM_ALL_FILES", r"汇总|合计|求和|总计|总额|累计|加总", "对多份材料做汇总取数（求和/合计）"),
    ("MULTI_FILE_AGGREGATE", r"所有发票|全部发票|所有材料|全部分|多份.*(合计|汇总|相加)|跨文件(合计|汇总|相加)", "跨多个文件的聚合取数"),
    ("CROSS_PERIOD_TOTAL", r"不同期间|跨期|分期|各期.*(合计|汇总)|累计到", "跨期间累计取数"),
    ("MULTI_CONTRACT_GROUP", r"多份合同|全部合同|所有合同|按合同分组", "多合同分组取数"),
    ("CONSTANT_COMPARISON", r"不(得|能)?(超过|大于|高于|少于|低于)\s*[\d一二三四五六七八九十百千万亿]+|(等于|固定(为|值))\s*[\d一二三四五六七八九十百千万亿]+", "与固定常量比较（系统只支持字段之间的比较）"),
    ("EXTERNAL_LOOKUP", r"系统外查询|外部系统|征信|工商|查询第三方|联网核查", "需要系统外查询"),
]
_UNSUPPORTED_COMPILED = [
    (code, re.compile(pattern), description) for code, pattern, description in UNSUPPORTED_CAPABILITY_PATTERNS
]


# 否定/限制语境：这些词出现在关键词所在的同一小句内时，说的是
# 「系统做不了 / 不要这样做」，而不是业务要求系统去做汇总取数。
# 规则备注里大量存在这类说明（例如「金额单位与币种未明确，不能直接比较或加总」），
# 把它们当成业务要求会让整份规则库无法发布。
#
# 判据是「同一小句内出现过否定词」，而不是紧邻关键词：
#   「不能直接比较或加总」里否定词与「加总」之间隔了「直接比较或」；
#   「按所有发票合计金额核对」全句无否定词，照常命中。
_NEGATION_WORDS = ("不能", "不得", "不可", "不应", "不允许", "不支持", "无法", "禁止", "避免", "勿", "不予", "未能", "不做", "无需", "不需要")
# 小句边界：中英文标点与换行。能力关键词的判定只在所在小句内看否定词。
_CLAUSE_SPLIT = re.compile(r"[。；;！!？?\n\r]+")


def _clause_of(text: str, position: int) -> str:
    """关键词所在的小句文本（以句号/分号/换行切分）。"""
    start = 0
    for match in _CLAUSE_SPLIT.finditer(text):
        if match.end() <= position:
            start = match.end()
        else:
            break
    end = len(text)
    tail = _CLAUSE_SPLIT.search(text, position)
    if tail:
        end = tail.start()
    return text[start:end]


def validate_declared_rule_capability(rule_name: str, declaration: Any) -> None:
    """模型或配置显式声明的能力缺口必须报错，不能依赖原文关键词再次命中。"""
    if declaration is None:
        return
    try:
        unsupported = UnsupportedCapability.model_validate(declaration)
    except ValidationError as exc:
        raise BusinessError(
            "RULE_CAPABILITY_DECLARATION_INVALID",
            f"规则“{rule_name}”的能力缺口声明无效",
            details={"rule": rule_name, "errors": exc.errors(include_url=False, include_input=False, include_context=False)},
        ) from exc
    raise BusinessError(
        "RULE_CAPABILITY_UNSUPPORTED",
        f"规则“{rule_name}”包含当前不支持的能力：{unsupported.detail}",
        details={"rule": rule_name, "capability": unsupported.code, "detail": unsupported.detail},
    )


def detect_unsupported_capability(*texts: Any) -> tuple[str, str, str] | None:
    """对任意算子统一识别当前不具备执行能力的规则语义。

    返回 (capability_code, matched_text, description)；没有命中时返回 None。
    判定只依据用户真正写在规则里的文字，不猜测、不自动改写，命中即显式报错。

    否定语境不算命中：业务写「不能直接比较或加总」「无法汇总」是在说明约束或技术
    限制，不是在要求系统做汇总取数。判定只在关键词所在的同一小句内检查否定词，
    因此「按所有发票合计金额核对」这类真实要求仍会被拦截。
    """
    for value in texts:
        if not isinstance(value, str) or not value.strip():
            continue
        text = " ".join(value.split())
        for code, pattern, description in _UNSUPPORTED_COMPILED:
            for matched in pattern.finditer(text):
                clause = _clause_of(text, matched.start())
                if any(word in clause for word in _NEGATION_WORDS):
                    continue
                return code, matched.group(0), description
    return None


def comparison_label(operator: str, comparison: Any) -> str | None:
    """把比较方式译成业务中文，供页面与报告说明「实际是怎么比的」；非比较取值原样返回。"""
    if not isinstance(comparison, str):
        return None
    if operator == "DATE_COMPARE":
        return DATE_COMPARISON_LABELS.get(comparison, comparison)
    if operator == "DECIMAL_COMPARE":
        return DECIMAL_COMPARISON_LABELS.get(comparison, comparison)
    if operator == "DECIMAL_SUM_COMPARE":
        return DECIMAL_SUM_COMPARISON_LABELS.get(comparison, comparison)
    return comparison


def operator_uses_addends(operator: str) -> bool:
    """该算子是否由「对照内容」的加总式展开成多个输入。"""
    return operator == "DECIMAL_SUM_COMPARE"


def detect_unsupported_capability_for_operator(
    operator: Any,
    *texts: Any,
) -> tuple[str, str, str] | None:
    """按算子判定的「不支持的取数能力」识别。

    文字判定对算式类算子会误伤：`DECIMAL_SUM_COMPARE` 的审核要求原文必然含
    「总额 / 合计 / 加总 / 累计」，与 `SUM_ALL_FILES` 的关键词集合重叠，一律拦下
    会让一条系统完全支持的规则存不下来。

    因此只对算式算子跳过措辞重叠的两个码（`SUM_ALL_FILES`、`CROSS_PERIOD_TOTAL`）——
    理由是措辞重叠，不是放宽能力：这两个码真正要拦的越界（无界聚合、跨期自动累计），
    在算式算子上已由结构约束拦下（必须显式列出相加字段，编译期校验至少两个，系统不会
    自行跨文件取数）。用户真要求跨文件聚合时，措辞会落在仍生效的
    `MULTI_FILE_AGGREGATE`、`MULTI_CONTRACT_GROUP` 上。
    其余码照常生效，非算式算子走完整判定。
    """
    capability = capabilities.capability_of(operator)
    if operator == "LLM_VALIDATE":
        # 字段计算、条件及汇总由模型依据真实输入解释；外部查询仍没有数据来源。
        for value in texts:
            if isinstance(value, str):
                for code, pattern, description in _UNSUPPORTED_COMPILED:
                    if code != "EXTERNAL_LOOKUP":
                        continue
                    for matched in pattern.finditer(value):
                        if not any(word in _clause_of(value, matched.start()) for word in _NEGATION_WORDS):
                            return code, matched.group(0), description
        return None
    if capability is None or capability.input_kind != capabilities.INPUT_FIELD_LIST:
        return detect_unsupported_capability(*texts)
    for value in texts:
        if not isinstance(value, str) or not value.strip():
            continue
        text = " ".join(value.split())
        for code, pattern, description in _UNSUPPORTED_COMPILED:
            if code in _ADDEND_OPERATOR_EXEMPT_CODES:
                continue
            for matched in pattern.finditer(text):
                clause = _clause_of(text, matched.start())
                if any(word in clause for word in _NEGATION_WORDS):
                    continue
                return code, matched.group(0), description
    return None


# 算式算子上因关键词与加总措辞重叠而跳过的识别码。
# 只含这两个：它们的关键词（总额/合计/汇总/求和/总计/累计、跨期/分期/各期/累计到）
# 就是加总比较本身的业务措辞。MULTI_FILE_AGGREGATE / MULTI_CONTRACT_GROUP /
# CONSTANT_COMPARISON / EXTERNAL_LOOKUP 不在其中，照常在算式算子上生效。
_ADDEND_OPERATOR_EXEMPT_CODES = frozenset({"SUM_ALL_FILES", "CROSS_PERIOD_TOTAL"})


EVIDENCE_RETRIEVAL = capabilities.EVIDENCE_RETRIEVAL
# 主体检索：只对印章识别产物中的主体名称建向量，用待核对主体查相似印章主体。
# 它不是全文检索：不检索材料正文，也不检索条款。
EVIDENCE_SEAL_SUBJECT = capabilities.EVIDENCE_SEAL_SUBJECT
# 判定方式：需要人工裁决。
JUDGE_HUMAN = capabilities.JUDGE_HUMAN

# 取证与判定的固定路由：从能力目录（domain.capabilities）派生，由规则算子确定性派生，
# 业务人员不填写 RAG/LLM 技术选项，大模型也不在运行时自行路由或扩大材料范围。
ROUTE_BY_OPERATOR: dict[str, tuple[str, str]] = capabilities.ROUTE_BY_OPERATOR

def execution_route(rule: dict[str, Any]) -> dict[str, Any]:
    """规则的取证方式与判定方式；无法归入已支持流程时明确列为未支持。

    路由由能力目录（domain.capabilities）派生：算子存在、但当前 `implemented=false` 时
    同样记为 `supported=false` 并带上真实原因，避免把未实现能力显示成可执行规则。
    """
    operator = rule.get("operator")
    capability = capabilities.capability_of(operator)
    if capability is None:
        return {
            "evidence": None,
            "judge": None,
            "supported": False,
            "capability_id": operator,
            "capability_version": None,
            "message": f"算子“{operator}”当前没有固定的取证与判定流程，不能作为可执行规则发布",
        }
    evidence, judge = capability.evidence, capability.judge
    if operator == "LLM_VALIDATE" and (rule.get("parameters") or {}).get("evidence_source") == "FIELD_FACTS":
        evidence = capabilities.EVIDENCE_DIRECT
    if rule.get("subject_check") is True and operator in {"FIELD_EQUAL", "FIELD_NOT_EQUAL"}:
        # 主体/签章一致性必须走主体检索：待核对主体与印章识别主体比对本就需要印章产物，
        # 因此这条规则自身声明并触发印章索引依赖，不依赖“另外启用一条条款规则”才建索引，
        # 也不需要额外启用某条文件质量检查才取得印章产物。结论由人工确认，不由大模型裁决。
        evidence, judge = EVIDENCE_SEAL_SUBJECT, JUDGE_HUMAN
    payload: dict[str, Any] = {
        "evidence": evidence,
        "judge": judge,
        "supported": capability.implemented,
        "capability_id": capability.capability_id,
        "capability_version": capability.version,
        "message": "",
    }
    if not capability.implemented:
        payload["message"] = capability.unimplemented_reason
    if operator == "FILE_QUALITY_CHECK":
        # 算子本身已实现，但判定能力取决于检查子项：子项未实现时本条规则不可判定。
        # 子项的实现状态同样来自能力目录，不再由 quality_supported 单独承载一份。
        check_item = (rule.get("parameters") or {}).get("check_item")
        payload["capability_id"] = check_item or capability.capability_id
        payload["supported"] = capabilities.is_implemented(check_item)
        if not payload["supported"]:
            sub = capabilities.capability_of(check_item)
            payload["message"] = (
                sub.unimplemented_reason if sub else "该文件检查的检查项目尚无执行能力，本项无法执行"
            )
    return payload


def applicability_view(rule: dict[str, Any]) -> dict[str, Any]:
    """把适用条件统一成结构化形式并附中文口径，供预览与执行使用。"""
    applicability = rule.get("applicability")
    if not isinstance(applicability, dict) or not applicability.get("kind"):
        applicability = {"kind": "ALWAYS"}
    return {
        "kind": applicability["kind"],
        "label": APPLICABILITY_LABELS.get(applicability["kind"], applicability["kind"]),
        "material_keys": list(applicability.get("material_keys", [])),
    }


def _compiled_applicability(
    rule: dict[str, Any],
    inputs: list[dict[str, Any]],
    material_scope: list[str],
) -> dict[str, Any]:
    """把适用条件固化成已发布配置里的结构化字段。

    “所引用材料均已上传”的判定材料来自该规则明确引用的输入字段所属材料与规则声明的材料范围，
    去重后保持稳定顺序；不按文件名或中文名相似度猜材料。
    """
    applicability = applicability_view(rule)
    material_keys: list[str] = []
    for item in inputs:
        if item["material_key"] not in material_keys:
            material_keys.append(item["material_key"])
    for material_key in material_scope:
        if material_key not in material_keys:
            material_keys.append(material_key)
    if applicability["kind"] != "ALWAYS" and not material_keys:
        raise BusinessError(
            "MANIFEST_APPLICABILITY_MATERIAL_MISSING",
            "规则的适用条件为“所引用材料均已上传”，但该规则没有引用任何材料，无法判定何时适用",
            details={"rule": rule["name"]},
        )
    return {"kind": applicability["kind"], "material_keys": material_keys}


# 报告章节的数据来源：内容类型决定章节取哪些结果，章节标题不参与推断。
# 报告输出格式中当前真正实现了文件生成的只有 Markdown。
# 场景报告配置中声明 PDF / Word / Excel，是业务对报告形态的期望，
# 不等于系统已经能产出这三种文件；声明与实现必须分开记录，不能混为一谈。
IMPLEMENTED_REPORT_FORMATS = frozenset({"Markdown"})
REPORT_FIELD_CONTENT_TYPES = {"FIELD_DETAIL", "FIELD_AND_RESULT"}


def _validate_report_sort(section: dict[str, Any]) -> None:
    """核对排序配置结构；缺少业务等级顺序留到实际执行时明确说明。"""
    code = section.get("report_code") or section.get("section_name")
    result_sort = section.get("result_sort")
    if result_sort is not None and result_sort not in REPORT_RESULT_SORTS:
        raise BusinessError(
            "MANIFEST_REPORT_SORT_INVALID",
            f"报告“{code}”的结果排序不受支持",
            details={"report_code": code, "result_sort": result_sort, "allowed": sorted(REPORT_RESULT_SORTS)},
        )
    level_order = section.get("risk_level_order") or []
    if (not isinstance(level_order, list)
            or any(not isinstance(value, str) or not value.strip() for value in level_order)
            or len(set(level_order)) != len(level_order)):
        raise BusinessError("MANIFEST_REPORT_LEVEL_ORDER_INVALID", "报告风险等级顺序必须是无重复的非空等级名称列表", details={"report_code": code})


def _build_report_groups(
    sections: list[dict[str, Any]],
    explicit: bool,
) -> dict[str, Any]:
    """把报告章节按报告编号分组，并保留每份报告的属性与章节顺序。

    节点版里同一报告编号的报告名称、材料范围、输出格式、结果排序必须完全一致；
    这里做发布前最后一道核对，冲突时明确指出是哪一项、在哪两行之间不一致，
    不取第一行、不用空白继承，也不忽略冲突。旧格式没有报告编号，
    没有报告编号时组成一份报告，章节按声明顺序生成正文。
    """
    for section in sections:
        _validate_report_sort(section)
        content_type = section.get("content_type")
        if content_type is not None and content_type not in REPORT_CONTENT_TYPES:
            raise BusinessError(
                "MANIFEST_REPORT_CONTENT_TYPE_INVALID", "报告章节的内容类型不受支持",
                details={"section": section.get("section_name"), "content_type": content_type,
                         "allowed": sorted(REPORT_CONTENT_TYPES)},
            )
        order = section.get("section_order")
        if order is not None and (not isinstance(order, int) or isinstance(order, bool) or order < 1):
            raise BusinessError(
                "MANIFEST_REPORT_SECTION_ORDER_INVALID", "报告章节顺序必须是正整数",
                details={"section": section.get("section_name"), "section_order": order},
            )
    if not explicit:
        ordered = sorted(sections, key=lambda item: item.get("section_order") or 0)
        formats = list(dict.fromkeys(value for section in ordered for value in section.get("formats") or []))
        return {
            "reports": [{
                "report_code": None, "report_name": "审核报告", "material_keys": [],
                "formats": formats or ["Markdown"], "result_sort": None, "risk_level_order": [],
                "format_support": [{
                    "format": value, "supported": value in IMPLEMENTED_REPORT_FORMATS,
                    "message": "" if value in IMPLEMENTED_REPORT_FORMATS else f"输出格式“{value}”尚未实现：当前只生成 Markdown 正文",
                } for value in formats],
                "sections": [{
                    "section_name": section["section_name"],
                    "content_type": section.get("content_type"),
                    "section_order": index, "field_keys": list(section.get("field_keys", [])),
                    "material_keys": list(section.get("material_keys") or []),
                    "result_sort": section.get("result_sort"),
                    "risk_level_order": list(section.get("risk_level_order") or []),
                    "content_instruction": section.get("content_instruction", ""),
                    "config_status": section.get("config_status"),
                } for index, section in enumerate(ordered, start=1)],
            }],
            "source_kind": "SECTIONS",
        }
    groups: dict[str, dict[str, Any]] = {}
    for section in sections:
        code = section.get("report_code")
        if not isinstance(code, str) or not code.strip():
            raise BusinessError(
                "MANIFEST_REPORT_CODE_REQUIRED",
                "报告章节缺少报告编号，无法按报告分组",
                details={"section": section.get("section_name")},
            )
        content_type = section.get("content_type")
        # 内容类型可省略，由章节原文指导 LLM 组织真实来源；缺字段不阻断发布。
        result_sort = section.get("result_sort")
        entry = groups.get(code)
        signature = {
            "report_name": section.get("report_name"),
            "material_keys": list(section.get("material_keys", [])),
            "formats": list(section.get("formats", [])),
            "result_sort": result_sort,
            "risk_level_order": list(section.get("risk_level_order") or []),
        }
        if entry is None:
            if not signature["formats"]:
                raise BusinessError(
                    "MANIFEST_REPORT_FORMAT_REQUIRED",
                    f"报告“{code}”没有声明输出格式",
                    details={"report_code": code},
                )
            # 未声明材料筛选时不补造范围；报告执行按显式字段与任务结果取数。
            groups[code] = {
                "report_code": code,
                **signature,
                "section_orders": {},
                "sections": [],
            }
            entry = groups[code]
        else:
            conflicts = [key for key in signature if entry[key] != signature[key]]
            if conflicts:
                raise BusinessError(
                    "MANIFEST_REPORT_GROUP_CONFLICT",
                    f"报告编号 {code} 的"
                    + "、".join(conflicts)
                    + "在多行章节之间不一致：同一报告的这些属性必须完全一致",
                    details={"report_code": code, "conflicts": conflicts},
                )
        order = section.get("section_order")
        if not isinstance(order, int) or isinstance(order, bool) or order < 1:
            raise BusinessError(
                "MANIFEST_REPORT_SECTION_ORDER_INVALID",
                f"报告“{code}”的章节“{section.get('section_name')}”缺少有效的章节顺序（同报告内唯一的正整数）",
                details={"report_code": code},
            )
        if order in entry["section_orders"]:
            raise BusinessError(
                "MANIFEST_REPORT_SECTION_ORDER_DUPLICATED",
                f"报告“{code}”的章节顺序 {order} 重复，请改为同报告内唯一的正整数",
                details={"report_code": code, "section_order": order},
            )
        entry["section_orders"][order] = section.get("section_name")
        entry["sections"].append(
            {
                "section_name": section.get("section_name"),
                "content_type": content_type,
                "section_order": order,
                "field_keys": list(section.get("field_keys", [])),
                "content_instruction": section.get("content_instruction", ""),
                "config_status": section.get("config_status"),
            }
        )
    reports = []
    name_owner: dict[str, str] = {}
    for code in sorted(groups):
        entry = groups[code]
        entry.pop("section_orders", None)
        entry["sections"].sort(key=lambda item: item["section_order"])
        name = entry["report_name"]
        if name in name_owner and name_owner[name] != code:
            raise BusinessError(
                "MANIFEST_REPORT_NAME_DUPLICATED",
                f"报告名称“{name}”同时出现在报告编号 {name_owner[name]} 与 {code} 上，不同报告编号不能同名",
                details={"report_code": code},
            )
        name_owner[name] = code
        # 输出格式的支持状态必须随报告固化：PDF / Word / Excel 三种真实文件生成尚未实现，
        # 目前只有 Markdown 正文。声明了未实现的格式却只记录 formats、不记录支持状态，
        # 会让发布预览与页面把它显示成“可执行的输出格式”。
        # 这里不阻断发布（格式是业务对报告的声明），但如实标出哪些尚未实现。
        entry["format_support"] = [
            {
                "format": item,
                "supported": item in IMPLEMENTED_REPORT_FORMATS,
                "message": (
                    ""
                    if item in IMPLEMENTED_REPORT_FORMATS
                    else f"输出格式“{item}”尚未实现：当前只生成 Markdown 正文，不会按该格式产出文件，也不能改扩展名冒充"
                ),
            }
            for item in entry["formats"]
        ]
        reports.append(entry)
    return {"reports": reports, "source_kind": "REPORT_CODES"}


def rule_comparison_view(
    rule: dict[str, Any],
    material_names: dict[str, str],
    field_labels: dict[str, str],
    rule_names: dict[str, str],
    field_meta: dict[str, dict[str, Any]] | None = None,
    *,
    calculation_meta: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """把规则的真实比较对象、比较方式、参数和来源组织成业务可读结构。

    预览必须能一次看清四件事：引用原文、实际比较对象、方向与阈值、单位和适用条件，
    否则用户只能凭模型改写后的措辞判断这条规则是否符合业务要求。
    """
    parameters = rule.get("parameters") if isinstance(rule.get("parameters"), dict) else {}
    meta = field_meta or {}
    inputs = [
        {
            "material_name": material_names.get(item.get("material_ref")),
            "field_name": field_labels.get(item.get("field_ref")),
            "field_ref": item.get("field_ref"),
            "value_type": meta.get(item.get("field_ref"), {}).get("value_type"),
            "unit": meta.get(item.get("field_ref"), {}).get("unit"),
            "currency": meta.get(item.get("field_ref"), {}).get("currency"),
        }
        for item in rule.get("inputs", [])
    ]
    applicability = applicability_view(rule)
    source = rule.get("source") if isinstance(rule.get("source"), dict) else {}
    requirement = rule.get("requirement") or None
    # 规则原文是用户实际写下的内容；模型改写后的措辞不能替代它出现在预览里。
    source_texts = source.get("texts") if isinstance(source.get("texts"), dict) else {}
    direction = comparison_label(rule.get("operator", ""), parameters.get("operator"))
    threshold = parameters.get("threshold")
    unit_mismatch = _comparison_unit_mismatch(rule.get("operator"), inputs)
    source_rule_refs = list(dict.fromkeys([
        *(rule.get("source_rule_refs") or []),
        *([rule["source_rule_ref"]] if rule.get("source_rule_ref") else []),
    ]))
    unsupported = detect_unsupported_capability_for_operator(rule.get("operator"), requirement, parameters.get("instruction"))
    if rule.get("unsupported_capability") is not None:
        declaration = UnsupportedCapability.model_validate(rule["unsupported_capability"])
        support_status = "UNSUPPORTED"
        support_message = f"当前系统不支持：{declaration.detail}。请先改为可支持的检查方式。"
    elif unsupported:
        support_status = "UNSUPPORTED"
        support_message = f"当前系统不支持：{unsupported[2]}（命中“{unsupported[1]}”）。请先改为可支持的检查方式。"
    elif unit_mismatch:
        support_status = "UNIT_MISMATCH"
        support_message = unit_mismatch
    else:
        support_status = "SUPPORTED"
        support_message = ""
    return {
        "operator_label": OPERATOR_LABELS.get(rule.get("operator"), rule.get("operator")),
        "left": inputs[0] if inputs else None,
        "right": inputs[1] if len(inputs) > 1 else None,
        # 加总比较的右侧是多个加数：它们必须全部展示。"只显示两个输入"会让预览与执行不一致，
        # 也会让用户以为第三个加数没有被采用。单字段比较保持原有 extra_inputs 语义。
        "addends": inputs[1:] if operator_uses_addends(rule.get("operator", "")) else [],
        "extra_inputs": [] if operator_uses_addends(rule.get("operator", "")) else inputs[2:],
        "comparison_label": direction,
        "parameters": parameters,
        # 审核要求原文：页面展示的必须是用户实际填写的内容，不是模型的改写结果。
        "requirement": requirement,
        # 规则所在行的原始单元格文本（列名 → 原文），供预览逐项对照 Excel。
        "source_texts": source_texts,
        "applicability": {"kind": applicability["kind"], "material_keys": applicability["material_keys"]},
        "applicability_label": applicability["label"],
        "source_rule_name": rule_names.get(rule.get("source_rule_ref")) if rule.get("source_rule_ref") else None,
        "source_rule_refs": source_rule_refs,
        "source_rule_names": [rule_names.get(ref) or ref for ref in source_rule_refs],
        "calculation_sources": [
            {
                "result_code": code,
                "rule_name": calculation_meta.get(code, {}).get("rule_name"),
                "node_name": calculation_meta.get(code, {}).get("node_name"),
            }
            for code in dict.fromkeys(rule.get("source_rule_codes") or [])
        ],
        # 风险疑点的来源节点：页面据此说明这条风险监视的是哪一类审核规则。
        # 非风险规则没有来源节点，两个键显式为 None。
        "source_node": rule.get("source_node"),
        "source_node_label": SOURCE_NODE_LABELS.get(rule.get("source_node")),
        "scope_material_names": [material_names.get(ref) for ref in rule.get("material_refs", [])],
        "scope_material_refs": list(rule.get("material_refs", [])),
        "output_field_names": [field_labels.get(ref) for ref in rule.get("output_field_refs", [])],
        # 方向、阈值、单位与支持状态必须显式可见，不能只靠 operator/parameters 原始编码。
        "direction_label": direction,
        "threshold": threshold,
        "support_status": support_status,
        "support_message": support_message,
        "input_units": [item.get("unit") for item in inputs],
        "input_currencies": [item.get("currency") for item in inputs],
        # 风险成立条件与等级原文：等级差异必须显式可见，不能用较低风险等级覆盖来源异常等级。
        "risk_condition": rule.get("risk_condition"),
        "risk_condition_label": RISK_CONDITION_LABELS.get(rule.get("risk_condition") or "", rule.get("risk_condition")),
        "check_level": rule.get("check_level"),
        "risk_level_value": rule.get("risk_level_value"),
        "config_status": rule.get("config_status"),
    }


def _comparison_unit_mismatch(operator: str, inputs: list[dict[str, Any]]) -> str | None:
    """比较双方单位不一致时给出明确说明，不做任何换算猜测。

    金额/数值比较必须口径一致；只有双方都声明了单位且不同才在这里报出来，
    单边未声明单位的情形在执行期按证据不足处理，不在预览里冒充通过。
    加总比较逐一核对每个加数：任一加数单位不同都不能发布。
    """
    if operator not in {
        "DECIMAL_COMPARE",
        "DECIMAL_SUM_COMPARE",
        "DATE_COMPARE",
        "FIELD_EQUAL",
        "FIELD_NOT_EQUAL",
    }:
        return None
    if len(inputs) < 2:
        return None
    declared = {(item.get("unit") or "").strip() for item in inputs if (item.get("unit") or "").strip()}
    if len(declared) > 1:
        units = " / ".join(sorted(declared))
        return f"比较双方单位不一致（{units}），系统不做单位或币种换算"
    return None


def unconfigured_check_node_types(rules: list[dict[str, Any]]) -> list[str]:
    """当前场景未配置任何启用规则的业务校验能力类型。

    返回的是能力类型（`INTERNAL_VALIDATE` 一类），不是节点实例码：它是「这类检查
    在本次配置里有没有」的汇总口径。实例级口径见 `unconfigured_check_node_codes`，
    两者不能交叉使用——拿实例码去比能力类型列表，在用户改了实例码之后永远为假。
    """
    covered = {
        rule_node_type_of(item)
        for item in rules
        if item.get("enabled", True) and (item.get("node_type") or item.get("node"))
    }
    return [node_type for node_type in OPTIONAL_CHECK_NODES if node_type not in covered]


def unconfigured_check_node_codes(
    rules: list[dict[str, Any]],
    node_instances: list[dict[str, Any]],
) -> list[str]:
    """当前场景未配置这类检查的真实节点实例码。

    页面要把「当前节点是未配置的检查类别」显示出来，比较的必须是实例码：
    节点实例码由用户配置，同一能力类型可以有多个实例。
    """
    unconfigured = set(unconfigured_check_node_types(rules))
    return [
        str(item["node_code"])
        for item in node_instances
        if str(item.get("node_type") or item["node_code"]) in unconfigured
        and bool(item.get("enabled", True))
    ]


def capability_gaps(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """已配置但当前无法执行的检查。

    能力缺口由能力目录（domain.capabilities）派生：一条规则引用的能力 implemented=false
    时即为缺口，包括算子与文件检查子项。文件格式、大小、内容三项已有真实处理器并按
    真实数据判定，不能列为缺口；大小写金额一致性等未实现能力则必须在发布前暴露。

    缺口如实记录让发布预览可见，运行时遇到明确报不可判定，不默认通过。
    """
    gaps: list[dict[str, Any]] = []
    for rule in rules:
        if not rule.get("enabled", True):
            continue
        operator = rule.get("operator")
        capability = capabilities.capability_of(operator)
        if capability is None:
            # 未知算子由参数校验与节点约束在更早的环节报错；走到这里说明规则不是经
            # 导入或表单编译产生的，按未知能力显式报出，不静默跳过。
            raise BusinessError(
                "MANIFEST_OPERATOR_UNKNOWN",
                "规则引用了能力目录中不存在的算子，无法确定它是否可执行",
                details={"rule": rule.get("name"), "operator": operator},
            )
        parameters = rule.get("parameters") if isinstance(rule.get("parameters"), dict) else {}
        # 引用的能力标识：文件检查由「检查项目」子项决定，其余就是算子本身。
        capability_id = operator
        artifact_type = parameters.get("artifact_type")
        if operator == "FILE_QUALITY_CHECK":
            check_item = parameters.get("check_item")
            if not isinstance(check_item, str) or check_item not in capabilities.QUALITY_CAPABILITY_IDS:
                gaps.append({
                    "rule_key": rule.get("rule_key"), "rule_name": rule["name"],
                    "node": rule.get("node"), "operator": operator,
                    "capability_id": check_item, "capability_version": None,
                    "gap": "QUALITY_CAPABILITY_UNAVAILABLE",
                    "message": f"文件检查项目“{check_item}”尚无执行能力，本项无法执行",
                })
                continue
            capability_id = check_item
        sub = capabilities.capability_of(capability_id)
        if sub is None or sub.implemented:
            continue
        gap_code = (
            file_quality.gap_code_for(capability_id)
            if sub.kind == capabilities.KIND_QUALITY_ITEM
            else "CAPABILITY_NOT_IMPLEMENTED"
        )
        if gap_code == file_quality.GAP_SEAL_COUNT_IS_NOT_QUALITY:
            message = (
                "印章识别只返回印章数量与位置，不能证明盖章正确；"
                "该规则只能形成“是否检出印章”的证据，不构成质量通过结论。"
            )
        elif sub.kind == capabilities.KIND_QUALITY_ITEM:
            message = (
                f"当前处理产物（{artifact_type}）没有供应商提供的质量结论字段，"
                "运行时该规则会明确报“质量结论不可判定”，不会默认通过。"
            )
        else:
            message = f"{sub.unimplemented_reason}；运行时该规则会明确报“不可判定”，不会默认通过。"
        gaps.append(
            {
                "rule_key": rule.get("rule_key"),
                "rule_name": rule["name"],
                "node": rule.get("node"),
                "operator": operator,
                "capability_id": capability_id,
                "capability_version": sub.version,
                "gap": gap_code,
                "message": message,
            }
        )
    return gaps


def _normalize_text(value: str) -> str:
    return " ".join(value.split()).casefold()


def _parameter_error(
    rule_name: str,
    operator: str,
    reason: str,
    *,
    details: dict[str, Any] | None = None,
) -> None:
    context = details or {}
    explanation = [reason]
    for key, label in (("missing", "缺少"), ("unexpected", "不允许"), ("parameter", "参数"), ("allowed", "允许值")):
        if key in context:
            value = context[key]
            explanation.append(f"{label}：{'、'.join(str(item) for item in value) if isinstance(value, list) else value}")
    raise BusinessError(
        "MANIFEST_RULE_PARAMETER_INVALID",
        f"规则“{rule_name}”（{operator}）参数无效：{'；'.join(explanation)}",
        details={"rule": rule_name, "operator": operator, "reason": reason, **context},
    )


def _validate_parameter_keys(
    rule_name: str,
    operator: str,
    parameters: dict[str, Any],
    *,
    required: set[str] | None = None,
    optional: set[str] | None = None,
) -> None:
    if not isinstance(parameters, dict):
        _parameter_error(rule_name, operator, "参数必须是对象")
    required = required or set()
    optional = optional or set()
    keys = set(parameters)
    missing = required - keys
    unexpected = keys - required - optional
    if missing:
        _parameter_error(rule_name, operator, "缺少必填参数", details={"missing": sorted(missing)})
    if unexpected:
        _parameter_error(rule_name, operator, "包含不允许的参数", details={"unexpected": sorted(unexpected)})


def _require_non_empty_string(
    rule_name: str,
    operator: str,
    parameters: dict[str, Any],
    key: str,
) -> str:
    value = parameters[key]
    if not isinstance(value, str) or not value.strip():
        _parameter_error(rule_name, operator, "参数必须是非空文本", details={"parameter": key})
    return value


def _require_boolean(
    rule_name: str,
    operator: str,
    parameters: dict[str, Any],
    key: str,
) -> bool:
    value = parameters[key]
    if not isinstance(value, bool):
        _parameter_error(rule_name, operator, "参数必须是布尔值", details={"parameter": key})
    return value


def _require_decimal(
    rule_name: str,
    operator: str,
    parameters: dict[str, Any],
    key: str,
) -> Decimal:
    """读取数值参数，统一按 Decimal 处理并保留精度。

    允许差值是业务配置事实：节点版转换器以十进制字符串保存它（例如 "0.05"），
    页面也可能提交 JSON 数字。这里同时接受十进制字符串与 int/float，
    但一律转成 Decimal 参与比较与执行，绝不先经过 float：0.1+0.2 这类精度损失
    会直接改变「不超过允许差值」的判定结果，也会让 0.05 这类口径失去原值。

    不接受 bool、NaN、Infinity 与空字符串：它们都不是有效的业务数值。
    """
    value = parameters[key]
    if isinstance(value, bool):
        _parameter_error(rule_name, operator, "参数必须是有限数值", details={"parameter": key})
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            _parameter_error(rule_name, operator, "参数必须是有限数值", details={"parameter": key})
        try:
            numeric = Decimal(raw)
        except (InvalidOperation, ValueError):
            _parameter_error(rule_name, operator, "参数必须是有限数值", details={"parameter": key})
    elif isinstance(value, (int, float)):
        # float 只在这里做一次一次性转换；Decimal(str(...)) 取的是它的十进制字面量，
        # 不会把 0.1 展开成 0.1000000000000000055511151231257827。
        numeric = Decimal(str(value))
    else:
        _parameter_error(rule_name, operator, "参数必须是有限数值", details={"parameter": key})
    if not numeric.is_finite():
        _parameter_error(rule_name, operator, "参数必须是有限数值", details={"parameter": key})
    return numeric


def _validate_rule_parameters(rule_name: str, operator: str, parameters: dict[str, Any]) -> None:
    if operator == "MATERIAL_EXISTS":
        _validate_parameter_keys(rule_name, operator, parameters)
        return
    if operator == "FILE_QUALITY_CHECK":
        # 节点版的文件处理检查由「检查项目 + 合格要求 + 标准值/单位 + 检查口径」共同确定，
        # 这些是业务配置事实而不是执行开关，必须随规则一起固化，执行端据此选择判定方式。
        _validate_parameter_keys(
            rule_name,
            operator,
            parameters,
            required={"artifact_type"},
            optional={"check_item", "requirement", "standard_value", "unit", "criterion"},
        )
        artifact_type = _require_non_empty_string(rule_name, operator, parameters, "artifact_type")
        if artifact_type not in DOCUMENT_ARTIFACT_TYPES:
            _parameter_error(
                rule_name,
                operator,
                "处理产物类型不受支持",
                details={"parameter": "artifact_type", "allowed": sorted(DOCUMENT_ARTIFACT_TYPES)},
            )
        for key in ("check_item", "requirement"):
            if key in parameters:
                _require_non_empty_string(rule_name, operator, parameters, key)
        return
    if operator == "LLM_EXTRACT":
        _validate_parameter_keys(rule_name, operator, parameters, required={"instruction"})
        _require_non_empty_string(rule_name, operator, parameters, "instruction")
        return
    if operator == "FIELD_NOT_EMPTY":
        # 非空校验只判断有效事实是否存在且非空，没有可配置参数。
        _validate_parameter_keys(rule_name, operator, parameters, optional={"requires_manual_review"})
        if "requires_manual_review" in parameters:
            _require_boolean(rule_name, operator, parameters, "requires_manual_review")
        return
    if operator == "CLAUSE_ABSENT":
        # 条款存在性检查判断的是条款本身是否存在，没有可配置参数。
        _validate_parameter_keys(rule_name, operator, parameters, optional={"requires_manual_review"})
        if "requires_manual_review" in parameters:
            _require_boolean(rule_name, operator, parameters, "requires_manual_review")
        return
    if operator == "DECIMAL_CN_EQUAL":
        # 大小写金额一致性：允许差值与文本一致性同口径（0 或留空），换算口径另行确认。
        _validate_parameter_keys(
            rule_name,
            operator,
            parameters,
            optional={"threshold", "requires_manual_review"},
        )
        if "threshold" in parameters and _require_decimal(rule_name, operator, parameters, "threshold") != 0:
            _parameter_error(rule_name, operator, "大小写金额比较不做数值差额，差值只能是 0", details={"parameter": "threshold"})
        if "requires_manual_review" in parameters:
            _require_boolean(rule_name, operator, parameters, "requires_manual_review")
        return
    if operator in {"FIELD_EQUAL", "FIELD_NOT_EQUAL"}:
        _validate_parameter_keys(rule_name, operator, parameters, optional={"requires_manual_review"})
        if "requires_manual_review" in parameters:
            _require_boolean(rule_name, operator, parameters, "requires_manual_review")
        return
    if operator == "DATE_COMPARE":
        # 日期比较接受非负「允许差值」，单位是天（填写规范：0 表示原条件不允许偏差）。
        # 旧版规则不带差值，语义保持原样：不带差值时按严格比较执行，不重新解释历史规则。
        _validate_parameter_keys(
            rule_name,
            operator,
            parameters,
            required={"operator"},
            optional={"threshold", "threshold_unit", "requires_manual_review"},
        )
        comparison = _require_non_empty_string(rule_name, operator, parameters, "operator")
        if comparison not in DATE_COMPARE_OPERATORS:
            _parameter_error(
                rule_name,
                operator,
                "日期比较方式不受支持",
                details={"parameter": "operator", "allowed": sorted(DATE_COMPARE_OPERATORS)},
            )
        if "threshold" in parameters:
            threshold = _require_decimal(rule_name, operator, parameters, "threshold")
            if threshold < 0:
                _parameter_error(rule_name, operator, "允许差值不能为负数", details={"parameter": "threshold"})
            if "threshold_unit" not in parameters:
                _parameter_error(rule_name, operator, "填写日期差值时必须声明单位", details={"missing": ["threshold_unit"]})
            unit = _require_non_empty_string(rule_name, operator, parameters, "threshold_unit")
            if unit != DATE_THRESHOLD_UNIT:
                _parameter_error(
                    rule_name,
                    operator,
                    "日期差值单位只能是天",
                    details={"parameter": "threshold_unit", "allowed": [DATE_THRESHOLD_UNIT]},
                )
        elif "threshold_unit" in parameters:
            _parameter_error(rule_name, operator, "未填写允许差值时不能单独填写差值单位", details={"parameter": "threshold_unit"})
        if "requires_manual_review" in parameters:
            _require_boolean(rule_name, operator, parameters, "requires_manual_review")
        return
    if operator == "DECIMAL_COMPARE":
        _validate_parameter_keys(
            rule_name,
            operator,
            parameters,
            required={"operator"},
            # threshold_unit 是「差额阈值」的可选单位（页面可填、执行期用它核对口径），
            # 必须与执行端 registry.decimal_compare 的读取一致，否则填了单位的规则反而无法保存。
            optional={"threshold", "threshold_unit", "requires_manual_review"},
        )
        comparison = _require_non_empty_string(rule_name, operator, parameters, "operator")
        if comparison not in DECIMAL_COMPARE_OPERATORS:
            _parameter_error(
                rule_name,
                operator,
                "金额比较方式不受支持",
                details={"parameter": "operator", "allowed": sorted(DECIMAL_COMPARE_OPERATORS)},
            )
        # 等于/不大于/不小于都可以带非负允许差值（0 表示不允许偏差）。旧版
        # ABS_DIFF_LTE 的语义就是「等于且差额不超过阈值」，与这里一致，不需要改写历史规则。
        if "threshold" in parameters:
            if _require_decimal(rule_name, operator, parameters, "threshold") < 0:
                _parameter_error(rule_name, operator, "允许差值不能为负数", details={"parameter": "threshold"})
        if "threshold_unit" in parameters:
            _require_non_empty_string(rule_name, operator, parameters, "threshold_unit")
        if "requires_manual_review" in parameters:
            _require_boolean(rule_name, operator, parameters, "requires_manual_review")
        return
    if operator == "DECIMAL_SUM_COMPARE":
        # 加总比较：左侧一个字段与右侧多个加数之和比较。允许差值必须显式配置，
        # 0 表示不允许偏差；缺省不能当作 0，否则会静默放宽业务条件。
        _validate_parameter_keys(
            rule_name,
            operator,
            parameters,
            required={"operator", "threshold"},
            optional={"threshold_unit", "requires_manual_review"},
        )
        comparison = _require_non_empty_string(rule_name, operator, parameters, "operator")
        if comparison not in DECIMAL_SUM_COMPARE_OPERATORS:
            _parameter_error(
                rule_name,
                operator,
                "加总比较方式不受支持",
                details={"parameter": "operator", "allowed": sorted(DECIMAL_SUM_COMPARE_OPERATORS)},
            )
        if _require_decimal(rule_name, operator, parameters, "threshold") < 0:
            _parameter_error(rule_name, operator, "允许差值不能为负数", details={"parameter": "threshold"})
        if "threshold_unit" in parameters:
            _require_non_empty_string(rule_name, operator, parameters, "threshold_unit")
        if "requires_manual_review" in parameters:
            _require_boolean(rule_name, operator, parameters, "requires_manual_review")
        return
    if operator == "LLM_VALIDATE":
        _validate_parameter_keys(
            rule_name,
            operator,
            parameters,
            required={"instruction"},
            optional={"requires_manual_review", "evidence_source"},
        )
        _require_non_empty_string(rule_name, operator, parameters, "instruction")
        if "evidence_source" in parameters and parameters["evidence_source"] not in {"FIELD_FACTS", "RETRIEVAL"}:
            _parameter_error(rule_name, operator, "智能校验的依据来源不受支持", details={"parameter": "evidence_source", "allowed": ["FIELD_FACTS", "RETRIEVAL"]})
        if "requires_manual_review" in parameters:
            _require_boolean(rule_name, operator, parameters, "requires_manual_review")
        return
    if operator == "RISK_TRIGGER":
        # 风险成立条件不属于算子参数：它由检查行的「关联风险」反向确定，
        # 不允许在风险表重复维护第二份来源列表。
        _validate_parameter_keys(rule_name, operator, parameters, required={"risk_level", "action"})
        _require_non_empty_string(rule_name, operator, parameters, "risk_level")
        _require_non_empty_string(rule_name, operator, parameters, "action")
        return
    if operator == "REPORT_RENDER":
        _validate_parameter_keys(rule_name, operator, parameters)
        return
    _parameter_error(rule_name, operator, "算子不受支持")


def validate_rule_parameters(rule_name: str, operator: str, parameters: dict[str, Any]) -> None:
    """算子参数契约的唯一入口。

    导入阶段（llm_service）与编译阶段共用同一份校验，避免两处各写一套参数规则；
    参数不完整时抛出带中文原因的 BusinessError，不返回默认值。
    """
    _validate_rule_parameters(rule_name, operator, parameters)


def _validate_proposal_identity(proposals: dict[str, Any]) -> None:
    calculation_decision_aliases(proposals)
    candidate_refs: dict[str, dict[str, str]] = {"MATERIAL": {}, "FIELD": {}, "RULE": {}}
    source_refs: dict[tuple[str, str, str], str] = {}
    for sheet in proposals["sheets"]:
        sheet_key = sheet["sheet_key"]
        for property_name, target_type in (("materials", "MATERIAL"), ("fields", "FIELD"), ("rules", "RULE")):
            # 同一行业务记录按材料展开出的多条候选共用同一个业务编号（source_id）：
            # 内部校验写了三种材料就是一行业务记录，展开只是执行需要。
            # 这些兄弟候选共享编号是配置事实，不是「两个对象撞了编号」。
            # 材料来源编号仅用于追溯，可跨行共享；字段和规则仍按原引用契约核对。
            by_source: dict[str, set[int]] = {}
            for item in sheet.get(property_name, []):
                source_id = item.get("source_id")
                if not source_id:
                    continue
                by_source.setdefault(source_id, set()).add(int(item["source"].get("row_no") or 0))
            for item in sheet.get(property_name, []):
                candidate_ref = item["candidate_ref"]
                if candidate_ref in candidate_refs[target_type]:
                    raise BusinessError(
                        "MANIFEST_DUPLICATE_CANDIDATE_REF",
                        "同类候选引用不能重复",
                        details={"target_type": target_type, "candidate_ref": candidate_ref},
                    )
                candidate_refs[target_type][candidate_ref] = candidate_ref
                if item["source"]["sheet_key"] != sheet_key:
                    raise BusinessError(
                        "MANIFEST_SOURCE_SCOPE_INVALID",
                        "候选来源工作表与所属工作表不一致",
                        details={"target_type": target_type, "candidate_ref": candidate_ref},
                    )
                source_id = item.get("source_id")
                if not source_id:
                    continue
                if target_type != "MATERIAL" and len(by_source.get(source_id, set())) > 1:
                    # 同一编号出现在不同业务行：这是真正的编号冲突，必须报出。
                    raise BusinessError(
                        "MANIFEST_DUPLICATE_SOURCE_ID",
                        "同一工作表内的同类来源编码不能重复：同一编号出现在多行业务记录上",
                        details={
                            "target_type": target_type,
                            "sheet_key": sheet_key,
                            "source_ref": source_id,
                            "row_nos": sorted(by_source[source_id]),
                        },
                    )
                scope = (target_type, sheet_key, source_id)
                source_refs.setdefault(scope, candidate_ref)
        for section in sheet.get("report_sections", []):
            if section["source"]["sheet_key"] != sheet_key:
                raise BusinessError(
                    "MANIFEST_SOURCE_SCOPE_INVALID",
                    "报告章节来源工作表与所属工作表不一致",
                    details={"section": section["section_name"], "candidate_ref": section["candidate_ref"]},
                )
    for (target_type, sheet_key, source_id), owner_ref in source_refs.items():
        candidate_owner = candidate_refs[target_type].get(source_id)
        if candidate_owner is not None and candidate_owner != owner_ref:
            raise BusinessError(
                "MANIFEST_CANDIDATE_SOURCE_REF_CONFLICT",
                "候选引用不能与另一候选的来源编码冲突",
                details={
                    "target_type": target_type,
                    "sheet_key": sheet_key,
                    "source_ref": source_id,
                    "candidate_ref": candidate_owner,
                    "source_owner": owner_ref,
                },
            )


def label_resolver(column: dict[str, Any]) -> dict[str, str]:
    """把一列的取值配置解析成「单元格原文 → 契约值」。

    既有 profile 里 `value_labels` 的书写方向不统一，必须按列自适应：v2 是
    「契约值 → 界面标签」（{"SEED": "种子"}），v3 是「界面标签 → 契约值」
    （{"一级风险": "一级"}）。

    `allowed_values` 是这一列的契约取值集合，同时是判定依据：只有落在里面的映射才被接受，
    否则「一级」会被解析成并不存在的「一级风险」。
    """
    allowed = [str(item).strip() for item in (column.get("allowed_values") or []) if str(item).strip()]
    allowed_set = set(allowed)
    resolved: dict[str, str] = {text: text for text in allowed}
    if not allowed_set:
        # 没有声明契约取值集合的列（自由文本或清单列）不需要别名解析。
        return resolved
    for canonical, label in (column.get("value_labels") or {}).items():
        canonical_text = str(canonical).strip()
        label_text = str(label).strip()
        if not label_text:
            continue
        if canonical_text in allowed_set:
            # 「契约值 → 标签」写法：标签解析回该契约值。
            resolved[label_text] = canonical_text
        elif label_text in allowed_set:
            # 「标签 → 契约值」写法：键是显示文本，值是契约取值。
            resolved[canonical_text] = label_text
    return {key: value for key, value in resolved.items() if key}
def _ensure_unique_materials(materials: list[dict[str, Any]]) -> None:
    seen: dict[str, dict[str, Any]] = {}
    for material in materials:
        # 「是不是同一个材料位」只由材料名称决定：材料位没有其它可供判定的业务身份。
        signature = _normalize_text(material["name"])
        existing = seen.get(signature)
        if existing is not None:
            raise BusinessError(
                "MANIFEST_DUPLICATE_MATERIAL",
                "场景中存在重复材料位",
                details={
                    "name": material["name"],
                    "candidate_refs": [existing["candidate_ref"], material["candidate_ref"]],
                },
            )
        seen[signature] = material


def _node_position_of(
    node_positions: dict[str, int],
    node_code: Any,
    *,
    rule_name: str,
    role: str = "规则",
) -> int:
    """节点在用户配置的节点计划里的顺序位次；节点不在计划里时明确报业务错误。

    节点计划只含用户真实配置的节点实例，而规则可能仍指向已被删除或改名的实例。
    直接下标取值会抛裸 KeyError，接口返回 500 而真实原因（规则挂在不存在的节点上）
    完全不可见；这里把它变成可读的业务错误。
    """
    code = str(node_code or "")
    if code not in node_positions:
        raise BusinessError(
            "MANIFEST_RULE_NODE_MISSING",
            f"{role}“{rule_name}”指向的节点“{code}”不在本场景配置的节点清单里："
            "请先在场景配置里建立该节点，或把规则改挂到已配置的节点上",
            details={"rule": rule_name, "node_code": code, "configured_nodes": sorted(node_positions)},
        )
    return node_positions[code]


def _rule_semantic_signature(rule: dict[str, Any]) -> str:
    """规则的业务身份签名，用于识别真正的重复规则。

    签名口径是「节点 + 算子 + 输入字段集合（字段与所属材料）+ 输出字段 + 材料范围 +
    参数 + 适用条件」：同一条业务规则按材料拆成多行时，行与行之间的输入材料或材料范围
    不同，签名自然不同，因此不会被误判成重复规则；反之，只在中文名或审核要求措辞上
    有差别的两行，其可执行语义完全相同，仍然判为重复，不放宽唯一性。
    source_node 不进入签名：它是来源规则实际节点的派生结果（编译期已与来源规则的实际
    节点比对），同一条来源规则不可能同时落在两个来源节点里。
    """
    inputs = [
        {
            "field": item.get("field_key", item.get("field_ref")),
            "material": item.get("material_key", item.get("material_ref")),
        }
        for item in rule.get("inputs", [])
    ]
    comparison = rule.get("parameters", {}).get("operator")
    if rule["operator"] in {"FIELD_EQUAL", "FIELD_NOT_EQUAL"} or (
        rule["operator"] == "DATE_COMPARE" and comparison == "EQ"
    ) or (
        rule["operator"] == "DECIMAL_COMPARE" and comparison in {"EQ", "ABS_DIFF_LTE"}
    ):
        # 加总比较不参与排序：它的第一个输入是左侧待核对总额，其余是加数，
        # 打乱顺序等于把左侧总额混进加数里。
        inputs = sorted(inputs, key=lambda item: (str(item["field"]), str(item["material"])))
    value = {
        "node": rule["node"],
        "operator": rule["operator"],
        "inputs": inputs,
        "output_field_refs": sorted(rule.get("outputs", rule.get("output_field_refs", []))),
        "source_rule_ref": rule.get("source_rule_key", rule.get("source_rule_ref")),
        # 风险来源的执行编号必须进入签名，否则两条来源不同的风险
        # 会被判成同一条重复规则——而它们监视的是完全不同的上游结果。
        "source_rule_codes": sorted(rule.get("source_rule_codes") or []),
        "source_rule_refs": sorted(rule.get("source_rule_keys") or rule.get("source_rule_refs") or []),
        "risk_condition": rule.get("risk_condition"),
        "material_refs": sorted(rule.get("materials", rule.get("material_refs", []))),
        "parameters": rule.get("parameters", {}),
        # 适用条件不同的两条规则不是重复规则（例如一条始终适用、一条仅材料齐备时适用）。
        "applicability": rule.get("applicability"),
    }
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# 系统派生规则的算子：字段抽取与报告生成由编译期无条件补齐，业务不能手工创建。
SYSTEM_OPERATORS: frozenset[str] = frozenset({"LLM_EXTRACT", "REPORT_RENDER"})
# 系统派生规则的编号前缀。字段抽取、报告生成与材料检查的派生规则都带它，
# 但业务来源编号可能同形，分类时还须先看 `source_ref`；派生的材料检查规则算子
# 是 `MATERIAL_EXISTS`，业务也可能手工建一条同算子的规则。
SYSTEM_RULE_CODE_PREFIX = "SYS-"


def is_system_rule(rule: dict[str, Any]) -> bool:
    """这条规则是不是系统派生的（字段抽取 / 报告生成 / 材料检查）。

    显式来源编号 `source_ref` 是业务规则身份，即使它与系统编号前缀同形也不能丢弃。
    没有业务来源编号时，系统派生规则才由编号前缀识别。
    没有编号的历史字段抽取规则与报告生成规则仍按算子识别；
    前缀也覆盖派生材料检查（算子可与业务规则重合）。派生规则不进入场景草稿与业务规则计数；
    方案可以保存它们的启停选择，编译时仍按同一确定性规则重新派生——
    把它们当业务规则带进草稿会在发布时再次派生，造成重复规则。
    """
    source_ref = str(rule.get("source_ref") or "").strip()
    if source_ref:
        return False
    rule_code = str(rule.get("rule_code") or "")
    if rule_code.startswith(SYSTEM_RULE_CODE_PREFIX):
        return True
    if rule_code and rule.get("operator") == "LLM_EXTRACT":
        return False
    return str(rule.get("operator") or "") in SYSTEM_OPERATORS


def _material_check_rules(
    compiled_materials: list[dict[str, Any]],
    compiled_rules: list[dict[str, Any]],
    node_instances: list[dict[str, Any]],
    namespace: str,
    system_rule_enabled: dict[str, bool] | None = None,
) -> list[dict[str, Any]]:
    """从「是否必传」派生材料检查规则：每个必传材料一条。

    与「每个字段一条抽取规则」同一口径：当前场景材料配置的 required 是业务声明，
    材料检查节点据此逐份判定该材料是否已上传。

    不派生的话该节点会因为没有任何规则而返回 skipped=True「当前场景未配置材料检查规则」，
    而它其实已经按必传清单检查过了，页面却显示成「这类检查没配」，与「已跳过 ≠ 通过」冲突。

    场景没有配置材料检查节点时不派生（没有可挂的实例），不报错；同类节点有多个时
    显式报错，不静默挑一个。
    """
    # 业务已经显式声明过某个材料的检查时不再派生：两者语义完全相同，
    # 同时存在会被 `_ensure_unique_rules` 判成重复规则（MANIFEST_DUPLICATE_RULE），
    # 而报错只说「场景中存在重复规则」，看不出是派生规则与手工规则撞了。
    covered_by_business = {
        str(material_key)
        for rule in compiled_rules
        if rule.get("operator") == "MATERIAL_EXISTS"
        and not is_system_rule(rule)
        and rule.get("enabled", True)
        for material_key in (rule.get("materials") or [])
    }
    required_materials = [
        item
        for item in compiled_materials
        if item.get("required") and item["material_key"] not in covered_by_business
    ]
    if not required_materials:
        return []
    codes = sorted(
        str(item["node_code"])
        for item in node_instances
        if str(item.get("node_type") or item["node_code"]) == NODE_TYPE_MATERIAL_CHECK
    )
    if not codes:
        return []
    if len(codes) > 1:
        raise BusinessError(
            "MANIFEST_NODE_AMBIGUOUS",
            "本场景配置了多个「材料检查」节点，系统无法确定材料检查规则应挂在哪一个上："
            + "、".join(f"“{code}”" for code in codes),
            details={"node_type": NODE_TYPE_MATERIAL_CHECK, "node_codes": codes},
        )
    node_code = codes[0]
    requirement = "该材料必须上传"
    rules: list[dict[str, Any]] = []
    for material in required_materials:
        rule_key = stable_key("rule", namespace, f"material:{material['material_key']}")
        rules.append(
            {
                "rule_key": rule_key,
                "legacy_rule_key": rule_key,
                # 系统派生规则显式带 SYS- 前缀：它不是业务维护的规则编号。
                "rule_code": f"SYS-MATERIAL-{material['material_key']}",
                "name": f"材料齐全-{material['name']}",
                "node": node_code,
                "node_type": NODE_TYPE_MATERIAL_CHECK,
                "operator": "MATERIAL_EXISTS",
                "enabled": (system_rule_enabled or {}).get(f"SYS-MATERIAL-{material['material_key']}", True),
                "inputs": [],
                "outputs": [],
                "materials": [material["material_key"]],
                "source_rule_key": None,
                "source_rule_keys": [],
                "source_node": None,
                "source_nodes": [],
                "parameters": {},
                "applicability": {"kind": "ALWAYS", "material_keys": []},
                "requirement": requirement,
                "description": "",
                "source_ref": None,
                "source": dict(FORM_SOURCE),
            }
        )
    return rules


def _ensure_unique_rules(rules: list[dict[str, Any]]) -> None:
    """执行身份不能重复；业务内容相似不代表应拒收规则。"""
    seen: dict[str, dict[str, Any]] = {}
    for rule in rules:
        if not rule.get("enabled", True):
            continue
        signature = rule["rule_key"]
        existing = seen.get(signature)
        if existing is not None:
            raise BusinessError(
                "MANIFEST_DUPLICATE_RULE",
                "场景中存在重复规则执行身份",
                details={
                    "rule_refs": [
                        existing.get("candidate_ref", existing.get("rule_key")),
                        rule.get("candidate_ref", rule.get("rule_key")),
                    ]
                },
            )
        seen[signature] = rule


def internal_rule_branch_material(rule: dict[str, Any]) -> str | None:
    """内部校验的一条执行分支必须只读一份材料。"""
    materials = rule.get("materials") or []
    inputs = rule.get("inputs") or []
    if len(materials) != 1 or not inputs:
        return None
    material_key = materials[0]
    if not isinstance(material_key, str) or not material_key:
        return None
    if any(not isinstance(item, dict) or item.get("material_key") != material_key for item in inputs):
        return None
    return material_key


def validation_rule_branch_materials(
    rule: dict[str, Any], *, node_type: str | None = None,
) -> tuple[str, ...] | None:
    """校验规则按实际输入材料区分执行分支，业务编号仍属于原规则。"""
    node_type = node_type or rule_node_type_of(rule)
    if node_type == NODE_TYPE_INTERNAL_VALIDATE:
        material = internal_rule_branch_material(rule)
        return (material,) if material is not None else None
    if node_type != NODE_TYPE_CROSS_VALIDATE:
        return None
    materials = rule.get("materials") or []
    inputs = rule.get("inputs") or []
    if not materials or not inputs or any(not isinstance(key, str) or not key for key in materials):
        return None
    if any(not isinstance(item, dict) or item.get("material_key") not in materials for item in inputs):
        return None
    input_materials = {item["material_key"] for item in inputs}
    return tuple(sorted(input_materials)) if input_materials == set(materials) else None


def business_rule_key(
    namespace: str, node_type: str, rule_code: str, *, material_code: str | None = None,
    material_codes: list[str] | None = None,
) -> str:
    """导入与场景复制共用业务规则的稳定身份。"""
    identity = [node_type, rule_code]
    if node_type == NODE_TYPE_INTERNAL_VALIDATE:
        if material_code:
            identity.append(material_code)
        elif material_codes is not None:
            # LLM 可读取本规则声明的多个材料或仅按原文检索；不强制拆成单材料规则。
            identity.append(sorted(set(material_codes)))
        else:
            raise BusinessError(
                "IMPORT_RULE_BRANCH_INVALID",
                "内部校验规则缺少材料身份声明，无法派生稳定键",
                details={"rule_code": rule_code},
            )
    elif node_type == NODE_TYPE_CROSS_VALIDATE:
        if material_codes is None:
            raise BusinessError(
                "IMPORT_RULE_BRANCH_INVALID",
                "跨材料校验分支缺少参与材料编号，无法派生稳定键",
                details={"rule_code": rule_code},
            )
        identity.append(sorted(set(material_codes)))
    return stable_key("rule", namespace, json.dumps(identity, ensure_ascii=False, separators=(",", ":")))


def ensure_unique_rule_codes(rules: list[dict[str, Any]]) -> None:
    seen: dict[str, list[dict[str, Any]]] = {}
    for rule in rules:
        code = rule.get("rule_code")
        if not isinstance(code, str) or not code.strip():
            continue
        code = code.strip()
        owner = str(rule.get("rule_key") or rule.get("candidate_ref") or "")
        siblings = seen.setdefault(code, [])
        branch_materials = validation_rule_branch_materials(rule)
        same_business_rule = (
            siblings
            and branch_materials is not None
            and all(
                rule_node_type_of(other) == rule_node_type_of(rule)
                and other.get("node") == rule.get("node")
                and validation_rule_branch_materials(other) not in {None, branch_materials}
                for other in siblings
            )
        )
        if siblings and not same_business_rule:
            raise BusinessError(
                "MANIFEST_DUPLICATE_RULE_CODE",
                "业务规则编号重复；只有同一校验规则的不同材料分支可共用编号",
                details={"rule_code": code, "rule_refs": [
                    *(str(item.get("rule_key") or item.get("candidate_ref") or "") for item in siblings), owner
                ]},
            )
        siblings.append(rule)


def rule_node_type_of(item: dict[str, Any]) -> str:
    """一条规则归属节点的能力类型。

    规则的 `node` 是节点实例码（用户配置的 `scenario_draft_node.node_code`），
    能力类型在 `node_type` 里。旧格式（节点码即能力类型）没有 `node_type`，
    那时两者是同一个值，直接用它是旧格式的确定性语义，不是兜底。

    凡判断「这条规则属于哪一类节点」都必须用本函数，不能用 `node`：同一场景可以有两个
    同类节点，按实例码判断会让其中一条规则的这类校验永远不成立。
    """
    node_type = item.get("node_type")
    return str(node_type) if node_type else str(item["node"])


def instance_code_of_type(
    node_instances: list[dict[str, Any]],
    node_type: str,
    *,
    purpose: str,
) -> str:
    """系统派生规则要挂到用户配置的节点实例上，这里按能力类型取实例码。

    字段抽取规则、报告生成规则由后端确定性补齐，它们必须落在用户真实建立的实例上：
    用能力类型当实例码会让规则挂到一个并不存在的节点上，发布期以
    「规则指向不存在的节点」报出，看不出真实原因。

    同类节点有多个时显式报错：系统派生规则没有依据在其中挑一个，
    静默取第一个会让一条规则的归属随机取决于用户配置顺序。
    """
    matched = sorted(str(item["node_code"]) for item in node_instances if item["node_type"] == node_type)
    if not matched:
        raise BusinessError(
            "MANIFEST_NODE_REQUIRED",
            f"本场景没有配置「{node_type}」节点，无法放置{purpose}规则：请先在场景配置里建立该节点",
            details={"node_type": node_type, "purpose": purpose},
        )
    if len(matched) > 1:
        raise BusinessError(
            "MANIFEST_NODE_AMBIGUOUS",
            f"本场景配置了多个「{node_type}」节点，系统无法确定{purpose}规则应挂在哪一个上："
            + "、".join(f"“{code}”" for code in matched),
            details={"node_type": node_type, "purpose": purpose, "node_codes": matched},
        )
    return matched[0]


def _risk_source_node(rule_name: str, value: Any) -> str:
    """风险疑点的来源节点必须是契约枚举之一。

    缺失与取值非法都显式报错，绝不回落到“按来源规则猜节点”或默认节点。
    """
    if not isinstance(value, str) or value not in SOURCE_NODE_RULE_NODES:
        raise BusinessError(
            "MANIFEST_RISK_SOURCE_NODE_REQUIRED",
            "风险疑点必须指定来源节点",
            details={"rule": rule_name, "source_node": value, "allowed": sorted(SOURCE_NODE_RULE_NODES)},
        )
    return value


def _ensure_manifest_node_coverage(
    rules: list[dict[str, Any]],
    node_instances: list[dict[str, Any]],
    *,
    require_report_rule: bool = True,
) -> None:
    """发布前的节点覆盖校验。

    节点清单由用户配置，因此校验的是「已配置的节点里该有的规则有没有」，而不是
    「固定八个节点齐不齐」：配了报告输出节点则它必须有启用的报告生成规则（方案可显式
    关闭）；配了校验/风险节点时它们的规则可以为空——「本场景不配这类检查」是配置事实。

    附加报告章节是可选配置：只要固定事实报告仍能生成，就不把「没有附加章节」当成阻断项。
    """
    if not require_report_rule:
        return
    covered_nodes = {item["node"] for item in rules if item.get("enabled", True)}
    report_nodes = {
        str(item["node_code"]) for item in node_instances if item["node_type"] == "REPORT_OUTPUT"
    }
    missing_nodes = sorted(report_nodes - covered_nodes)
    if missing_nodes:
        raise BusinessError(
            "MANIFEST_NODE_RULE_MISSING",
            "已配置的报告输出节点缺少报告生成规则，不能发布",
            details={"node_codes": missing_nodes},
        )
    if report_nodes and not any(
        item["node"] in report_nodes and item["operator"] == "REPORT_RENDER" for item in rules
    ):        # 编译期已确定性补齐报告规则；走到这里说明规则被外部裁剪或被禁用。
        raise BusinessError("MANIFEST_REPORT_RULE_MISSING", "报告输出节点缺少报告生成规则")


def _proposal_reference_index(
    proposals: dict[str, Any],
    property_name: str,
) -> tuple[set[str], dict[str, dict[str, str]]]:
    candidate_refs: set[str] = set()
    source_refs: dict[str, dict[str, str]] = {}
    for sheet in proposals["sheets"]:
        sheet_key = sheet["sheet_key"]
        for item in sheet.get(property_name, []):
            candidate_refs.add(item["candidate_ref"])
            source_id = item.get("source_id")
            if source_id:
                source_refs.setdefault(sheet_key, {})[source_id] = item["candidate_ref"]
    return candidate_refs, source_refs


def _proposal_reference_exists(
    reference: str,
    owner_sheet_key: str,
    candidate_refs: set[str],
    source_refs: dict[str, dict[str, str]],
) -> bool:
    if reference in candidate_refs or reference in source_refs.get(owner_sheet_key, {}):
        return True
    cross_sheet_matches = {
        candidate_ref
        for references in source_refs.values()
        if (candidate_ref := references.get(reference)) is not None
    }
    if len(cross_sheet_matches) == 1:
        return True
    if cross_sheet_matches:
        raise BusinessError(
            "MANIFEST_SOURCE_SCOPE_INVALID",
            "来源编码在多个工作表中重复，无法跨工作表唯一解析，请使用候选引用关联",
            details={"reference": reference, "owner_sheet_key": owner_sheet_key},
        )
    return False


def _accepted(decisions: list[dict[str, Any]], target_type: str, target_ref: str) -> bool:
    matches = [
        item for item in decisions if item["target_type"] == target_type and item["target_ref"] == target_ref
    ]
    return not matches or matches[-1]["action"] != "EXCLUDE"


RESOLVABLE_PROPERTIES = {
    "MATERIAL": {"name", "required", "description", "config_status"},
    # unit / currency 是节点版的金额口径：缺少它们时业务可以通过既有裁决入口补齐，
    # 系统不代填默认单位或默认币种。
    "FIELD": {
        "name",
        "value_type",
        "unit",
        "currency",
        "material_ref",
        "required",
        "instruction",
        "extraction_strategy",
        "structure_schema",
        # 产物来源：主体核对需要它确定字段取自哪种处理产物，业务可通过裁决入口补齐。
        "artifact_source",
        "period_scope",
        "statement_scope",
        "config_status",
    },
    # source_node 与 source_rule_ref 是风险配置属性，不依赖特定工作表或列，
    # 人工裁决可以显式指定来源节点；它仍会在编译期与来源规则的实际节点比对。
    # check_level 保留当前场景的检查等级原文，不映射为固定等级。
    "RULE": {
        "name",
        "node",
        "operator",
        "parameters",
        "source_rule_ref",
        "source_rule_refs",
        "source_node",
        "material_refs",
        "check_level",
        # risk_condition 保存完整风险成立条件，缺失时由业务明确，不代选组合条件。
        "risk_condition",
        "config_status",
    },
    # 场景报告章节：非停用章节标记为待确认时该报告仍是未完成配置，
    # 业务可以在这里确认该章节可以执行，或明确停用（停用章节不进入报告）。
    "REPORT_SECTION": {
        "config_status", "risk_level_order", "result_sort", "section_name", "content_instruction",
        "report_code", "report_name", "material_refs", "field_refs", "formats", "section_order", "content_type",
    },
    "SHEET": {"suggested_role"},
    # 计算规则候选可修正类型和业务口径；结果身份不随修正变化。
    # 结果编号（result_code）不在可裁决属性里：它是下游引用的身份，
    # 改它会让已引用它的风险规则与计算规则指向一个不存在的编号。
    "CALC_RULE": {"status", "execution_mode", "result_type", "parameters", "bands", "formula", "judgement_instruction", "period_policy", "statement_scope", "unit", "currency"},
}


def calculation_decision_aliases(proposals: dict[str, Any]) -> dict[str, str]:
    """计算候选引用和业务编号指向同一裁决身份；歧义身份不能代选。"""
    aliases: dict[str, str] = {}
    for item in proposals.get("calculations", []):
        candidate_ref = item["candidate_ref"]
        for reference in (candidate_ref, item["rule_code"]):
            owner = aliases.get(reference)
            if owner is not None and owner != candidate_ref:
                raise BusinessError(
                    "MANIFEST_CALC_DECISION_TARGET_AMBIGUOUS",
                    "计算规则的候选引用与业务编号不能唯一定位裁决对象",
                    details={"target_ref": reference, "candidate_refs": [owner, candidate_ref]},
                )
            aliases[reference] = candidate_ref
    return aliases


def normalize_decision_targets(
    proposals: dict[str, Any], decisions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """只读统一计算裁决身份，按已保存顺序叠加属性及最后一次动作。"""
    aliases = calculation_decision_aliases(proposals)
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for decision in decisions:
        target_type = decision["target_type"]
        target_ref = decision["target_ref"]
        if target_type == "CALC_RULE":
            target_ref = aliases.get(target_ref, target_ref)
        item = {**decision, "target_ref": target_ref}
        key = (target_type, target_ref)
        previous = merged.get(key)
        if previous is not None and previous["action"] == item["action"] == "RESOLVE":
            item["value"] = {**previous["value"], **item["value"]}
        merged[key] = item
    return list(merged.values())


def refresh_calculation_references(calculations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """使用显式参数清单刷新计算依赖，不解析业务方法。"""
    refreshed = []
    for item in calculations:
        if item.get("status") == CONFIG_STATUS_DISABLED:
            refreshed.append(deepcopy(item))
            continue
        calc.assert_llm_engine(str(item.get("rule_code") or ""), item.get("execution_engine"))
        parameters = [_calculation_parameter(parameter) for parameter in item.get("parameters") or []]
        refreshed.append({
            **item,
            "depends_on_result_codes": sorted({
                parameter.source_code for parameter in parameters
                if parameter.source_type == "CALC_RESULT" and parameter.source_code
            }),
        })
    return refreshed


def normalize_resolution_value(target_type: str, property_name: str, value: Any) -> Any:
    """统一裁决文本转配置的既有约定，供作答合并与候选投影共同使用。"""
    if target_type == "REPORT_SECTION" and property_name == "risk_level_order" and isinstance(value, str):
        return [item.strip() for item in value.splitlines() if item.strip()]
    if (isinstance(value, str)
            and (target_type, property_name) in {
                ("FIELD", "structure_schema"), ("RULE", "parameters"),
                ("CALC_RULE", "parameters"), ("CALC_RULE", "bands"),
                ("REPORT_SECTION", "material_refs"), ("REPORT_SECTION", "field_refs"),
                ("REPORT_SECTION", "formats"),
            }):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise BusinessError(
                "MANIFEST_DECISION_VALUE_INVALID", "该配置属性需要填写完整 JSON",
                details={"target_type": target_type, "property": property_name, "reason": exc.msg},
            ) from exc
    return value


def _archive_disabled_proposals(proposals: dict[str, Any]) -> None:
    """历史提案和手动停用也只保留原文，不让停用配置进入执行结构检查。"""
    sheets = {sheet["sheet_key"]: sheet for sheet in proposals["sheets"]}

    def archive(item: dict[str, Any], sheet: dict[str, Any]) -> None:
        source = item["source"]
        sheet.setdefault("disabled_rows", []).append({
            "source": deepcopy(source), "sheet_name": sheet["sheet_key"],
            "source_id": item.get("source_id") or item.get("rule_code"),
            "columns": [{"key": cell, "label": cell, "value": text, "cell": cell}
                        for cell, text in (source.get("texts") or {}).items()],
            "raw_configuration": deepcopy(item),
        })

    for sheet in sheets.values():
        # 旧模型业务问题仍在原始提案中留存，不进入有效配置和作答集合。
        sheet["questions"] = []
        for collection in ("materials", "fields", "rules", "report_sections"):
            active = []
            for item in sheet.get(collection, []):
                if item.get("config_status") == CONFIG_STATUS_DISABLED:
                    archive(item, sheet)
                else:
                    active.append(item)
            sheet[collection] = active
    active_calculations = []
    for item in proposals.get("calculations", []):
        if item.get("status") == CONFIG_STATUS_DISABLED:
            archive(item, sheets[item["source"]["sheet_key"]])
        else:
            active_calculations.append(item)
    proposals["calculations"] = active_calculations


def _apply_resolutions(
    proposals: dict[str, Any],
    decisions: list[dict[str, Any]],
    *,
    validate_references: bool = True,
    node_types: dict[str, str] | None = None,
) -> dict[str, Any]:
    decisions = normalize_decision_targets(proposals, decisions)
    updated = deepcopy(proposals)
    indexes: dict[str, dict[str, dict[str, Any]]] = {
        "MATERIAL": {},
        "FIELD": {},
        "RULE": {},
        "REPORT_SECTION": {},
        "SHEET": {},
        # 计算规则候选挂在 bundle 顶层，来源可跨表，因此单独建立引用索引。
        "CALC_RULE": {},
    }
    for sheet in updated["sheets"]:
        indexes["SHEET"][sheet["sheet_key"]] = sheet
        for property_name, target_type in (
            ("materials", "MATERIAL"),
            ("fields", "FIELD"),
            ("rules", "RULE"),
            ("report_sections", "REPORT_SECTION"),
        ):
            for item in sheet.get(property_name, []):
                indexes[target_type][item["candidate_ref"]] = item
    for item in updated.get("calculations", []):
        # 裁决入口已把业务编号统一为此候选引用。
        indexes["CALC_RULE"][item["candidate_ref"]] = item
    for decision in decisions:
        if decision["action"] != "RESOLVE":
            continue
        target_type = decision["target_type"]
        target = indexes.get(target_type, {}).get(decision["target_ref"])
        if target is None:
            raise BusinessError("MANIFEST_DECISION_TARGET_MISSING", "裁决引用的对象不存在")
        for property_name, value in decision.get("value", {}).items():
            if property_name not in RESOLVABLE_PROPERTIES[target_type]:
                raise BusinessError("MANIFEST_DECISION_PROPERTY_INVALID", "裁决尝试修改不允许的属性", details={"property": property_name})
            target[property_name] = normalize_resolution_value(target_type, property_name, value)
        if target_type == "RULE" and "node" in decision.get("value", {}):
            node = target["node"]
            if not isinstance(node, str) or node_types is None or node not in node_types:
                raise BusinessError(
                    "MANIFEST_DECISION_NODE_INVALID", "修正的规则节点不在当前场景中",
                    details={"candidate_ref": target["candidate_ref"], "node": node},
                )
            # 实例和类型来自同一个真实节点，不能沿用候选原来的节点类型。
            target["node_type"] = node_types[node]
    _archive_disabled_proposals(updated)
    try:
        validated = ProposalBundle.model_validate(updated).model_dump(mode="json")
    except ValidationError as exc:
        mismatches = [
            {"field": ".".join(str(part) for part in error["loc"]) or "根对象",
             "code": error["type"], "reason": error["msg"]}
            for error in exc.errors(include_url=False, include_input=False, include_context=False)
        ]
        raise BusinessError(
            "MANIFEST_DECISION_VALUE_INVALID",
            "裁决值不符合候选对象的业务类型：" + "；".join(
                f"{error['field']}：{error['reason']}" for error in mismatches
            ),
            details={"mismatches": mismatches},
        ) from exc
    validated["calculations"] = refresh_calculation_references(validated["calculations"])
    if not validate_references:
        return validated
    # 排除项仍留在返回值中供工作台追溯；引用和编号只按最终保留的候选复核。
    active_sheets: list[dict[str, Any]] = []
    for sheet in validated["sheets"]:
        if not _accepted(decisions, "SHEET", sheet["sheet_key"]):
            continue
        active_sheet = dict(sheet)
        for property_name, target_type in (
            ("materials", "MATERIAL"),
            ("fields", "FIELD"),
            ("rules", "RULE"),
            ("report_sections", "REPORT_SECTION"),
        ):
            active_sheet[property_name] = [
                item for item in sheet.get(property_name, [])
                if _accepted(decisions, target_type, item["candidate_ref"])
            ]
        active_sheets.append(active_sheet)
    active = {"sheets": active_sheets}
    _validate_proposal_identity(active)
    material_refs, material_source_refs = _proposal_reference_index(active, "materials")
    field_refs, field_source_refs = _proposal_reference_index(active, "fields")
    active_rules = [rule for sheet in active_sheets for rule in sheet.get("rules", [])]
    rules_by_ref = {rule["candidate_ref"]: rule for rule in active_rules}
    rule_aliases, rule_sources = _alias_map(
        active_rules, {candidate_ref: candidate_ref for candidate_ref in rules_by_ref}
    )
    for sheet in active["sheets"]:
        sheet_key = sheet["sheet_key"]
        for field in sheet.get("fields", []):
            if not _proposal_reference_exists(
                field["material_ref"],
                sheet_key,
                material_refs,
                material_source_refs,
            ):
                raise BusinessError("MANIFEST_MATERIAL_REF_MISSING", "裁决后的字段引用了不存在的材料", details={"field": field["name"]})
        for rule in sheet.get("rules", []):
            if any(
                not _proposal_reference_exists(
                    item["material_ref"], sheet_key, material_refs, material_source_refs
                )
                or not _proposal_reference_exists(item["field_ref"], sheet_key, field_refs, field_source_refs)
                for item in rule.get("inputs", [])
            ):
                raise BusinessError("MANIFEST_RULE_INPUT_MISSING", "裁决后的规则输入引用不存在", details={"rule": rule["name"]})
            if any(
                not _proposal_reference_exists(reference, sheet_key, field_refs, field_source_refs)
                for reference in rule.get("output_field_refs", [])
            ):
                raise BusinessError("MANIFEST_RULE_OUTPUT_MISSING", "裁决后的规则输出引用不存在", details={"rule": rule["name"]})
            if any(
                not _proposal_reference_exists(reference, sheet_key, material_refs, material_source_refs)
                for reference in rule.get("material_refs", [])
            ):
                raise BusinessError("MANIFEST_RULE_MATERIAL_MISSING", "裁决后的规则材料范围引用不存在", details={"rule": rule["name"]})
            source_references = list(dict.fromkeys([
                *(rule.get("source_rule_refs") or []),
                *([rule["source_rule_ref"]] if rule.get("source_rule_ref") else []),
            ]))
            for source_ref in source_references:
                resolved_refs = _resolve_aliases(
                    source_ref,
                    sheet_key,
                    rule_aliases,
                    rule_sources,
                    target_type="RULE",
                    error_code="MANIFEST_RISK_SOURCE_MISSING",
                    error_message="裁决后的风险规则来源不存在",
                    details={"rule": rule["name"], "source_rule_ref": source_ref},
                )
                if rule["operator"] == "RISK_TRIGGER":
                    if any(rules_by_ref[ref]["operator"] == "RISK_TRIGGER" for ref in resolved_refs):
                        raise BusinessError(
                            "MANIFEST_RISK_SOURCE_INVALID",
                            "风险规则不能引用自身或另一条风险规则",
                            details={"rule": rule["name"], "source_rule_ref": source_ref},
                        )
        for section in sheet.get("report_sections", []):
            if any(
                not _proposal_reference_exists(reference, sheet_key, field_refs, field_source_refs)
                for reference in section.get("field_refs", [])
            ):
                raise BusinessError(
                    "MANIFEST_REPORT_FIELD_MISSING",
                    "裁决后的报告章节引用了不存在的字段",
                    details={"section": section["section_name"]},
                )
    return validated


def resolve_saved_decisions(
    proposals: dict[str, Any], decisions: list[dict[str, Any]], *, node_types: dict[str, str] | None = None,
) -> dict[str, Any]:
    """只读叠加已保存裁决；未完成的跨表修正可展示，写草稿时再复核引用。"""
    if not decisions:
        return deepcopy(proposals)
    return _apply_resolutions(proposals, decisions, validate_references=False, node_types=node_types)


def excluded_targets(decisions: list[dict[str, Any]]) -> tuple[set[str], set[tuple[str, str]]]:
    """返回被排除的工作表 key，以及被排除的 (target_type, candidate_ref)。"""
    excluded_sheets = {
        item["target_ref"]
        for item in decisions
        if item.get("target_type") == "SHEET" and item.get("action") == "EXCLUDE"
    }
    excluded_refs = {
        (item["target_type"], item["target_ref"])
        for item in decisions
        if item.get("action") == "EXCLUDE"
        and item.get("target_type") in {"MATERIAL", "FIELD", "RULE", "REPORT_SECTION", "CALC_RULE"}
    }
    return excluded_sheets, excluded_refs


def resolved_targets(decisions: list[dict[str, Any]]) -> set[tuple[str, str]]:
    """返回已由人工裁决过的 (target_type, candidate_ref)。"""
    return {
        (item["target_type"], item["target_ref"])
        for item in decisions
        if item.get("action") == "RESOLVE"
        and item.get("target_type") in {"MATERIAL", "FIELD", "RULE", "REPORT_SECTION", "CALC_RULE"}
    }


def _alias_map(
    items: list[dict[str, Any]],
    key_map: dict[str, str],
    *,
    shared_source_ids: bool = False,
) -> tuple[dict[str, str], dict[tuple[str, str], list[str]]]:
    candidate_aliases: dict[str, str] = {}
    source_aliases: dict[tuple[str, str], list[str]] = {}
    # 同一行展开的候选可共用来源编号；材料还允许跨行共享。具体引用优先使用
    # candidate_ref，共享编号解析为多项时由调用方明确处理，不任选一项。
    rows_by_source: dict[tuple[str, str], set[int]] = {}
    for item in items:
        source_id = item.get("source_id")
        if not source_id:
            continue
        rows_by_source.setdefault((item["source"]["sheet_key"], source_id), set()).add(
            int(item["source"].get("row_no") or 0)
        )
    for item in items:
        key = key_map[item["candidate_ref"]]
        candidate_aliases[item["candidate_ref"]] = key
        source_id = item.get("source_id")
        if not source_id:
            continue
        scope = (item["source"]["sheet_key"], source_id)
        if not shared_source_ids and len(rows_by_source.get(scope, set())) > 1:
            raise BusinessError(
                "MANIFEST_DUPLICATE_SOURCE_ID",
                "同一工作表内的来源编码重复：同一编号出现在多行业务记录上，无法生成场景",
                details={
                    "sheet_key": scope[0],
                    "source_ref": source_id,
                    "row_nos": sorted(rows_by_source[scope]),
                },
            )
        source_aliases.setdefault(scope, []).append(key)
    return candidate_aliases, source_aliases


def _resolve_aliases(
    reference: str,
    owner_sheet_key: str,
    candidate_aliases: dict[str, str],
    source_aliases: dict[tuple[str, str], list[str]],
    *,
    target_type: str,
    error_code: str,
    error_message: str,
    details: dict[str, Any],
) -> list[str]:
    candidate = candidate_aliases.get(reference)
    if candidate is not None:
        return [candidate]
    source = source_aliases.get((owner_sheet_key, reference))
    if source is not None:
        return source
    cross_sheet_matches = [keys for (_, source_ref), keys in source_aliases.items() if source_ref == reference]
    if len(cross_sheet_matches) == 1:
        return cross_sheet_matches[0]
    if cross_sheet_matches:
        raise BusinessError(
            "MANIFEST_SOURCE_SCOPE_INVALID",
            "来源编码在多个工作表中重复，无法跨工作表唯一解析，请使用候选引用关联",
            details={"target_type": target_type, "reference": reference, "owner_sheet_key": owner_sheet_key, **details},
        )
    raise BusinessError(error_code, error_message, details=details)


def _resolve_alias(
    reference: str,
    owner_sheet_key: str,
    candidate_aliases: dict[str, str],
    source_aliases: dict[tuple[str, str], list[str]],
    *,
    target_type: str,
    error_code: str,
    error_message: str,
    details: dict[str, Any],
) -> str:
    matches = _resolve_aliases(
        reference, owner_sheet_key, candidate_aliases, source_aliases,
        target_type=target_type, error_code=error_code, error_message=error_message, details=details,
    )
    if len(matches) != 1:
        raise BusinessError(
            "MANIFEST_SOURCE_SCOPE_INVALID",
            "来源编号对应同一业务行展开的多个候选，请使用明确的候选引用",
            details={"target_type": target_type, "reference": reference, "matches": matches, **details},
        )
    return matches[0]


def import_origin_of(
    item: dict[str, Any], *, sheet_names: dict[str, str], import_job_no: str
) -> dict[str, Any]:
    source = item["source"]
    sheet_key = source["sheet_key"]
    if sheet_key not in sheet_names:
        raise BusinessError(
            "IMPORT_SOURCE_SHEET_MISSING",
            "导入候选的来源工作表不在本次导入范围内",
            details={"sheet_key": sheet_key, "import_job_no": import_job_no},
        )
    return {
        "import_job_no": import_job_no,
        "sheet_key": sheet_key,
        "sheet_name": sheet_names[sheet_key],
        "row_no": source["row_no"],
        "cells": list(source["cells"]),
        "texts": dict(source.get("texts") or {}),
    }


def draft_config_of_import(
    *,
    proposals: dict[str, Any],
    decisions: list[dict[str, Any]],
    namespace: str,
    selected_sheets: list[dict[str, Any]],
    import_job_no: str,
    existing_config: dict[str, Any] | None = None,
    node_types: dict[str, str] | None = None,
) -> dict[str, Any]:
    """把已裁决候选投影为可编辑配置；增量模式允许引用只读现有身份。

    增量提案须先经 assemble_bundle 的现有引用校验。这里只投影新对象，
    不把上下文重新写入配置；实际引用仍逐项通过 resolve 核对。
    """
    decisions = normalize_decision_targets(proposals, decisions)
    resolved = _apply_resolutions(
        proposals, decisions, validate_references=existing_config is None, node_types=node_types,
    )
    if existing_config is not None:
        _validate_proposal_identity(resolved)
    excluded_sheets = {
        item["target_ref"]
        for item in decisions
        if item["target_type"] == "SHEET" and item["action"] == "EXCLUDE"
    }
    included = [sheet for sheet in resolved["sheets"] if sheet["sheet_key"] not in excluded_sheets]
    if not included:
        raise BusinessError(
            "SCENARIO_SHEET_REQUIRED",
            "裁决后没有参与本次导入的工作表，不能覆盖场景草稿",
            status_code=409,
            details={"excluded_sheet_keys": sorted(excluded_sheets)},
        )
    selected_sheet_keys = {item["sheet_key"] for item in selected_sheets}
    for sheet in included:
        if sheet["sheet_key"] not in selected_sheet_keys:
            raise BusinessError(
                "IMPORT_SHEET_SELECTION_CHANGED",
                "候选来源不在本次选入的工作表中，请重新选择工作表并解析规则库",
                status_code=409,
                details={
                    "sheet_key": sheet["sheet_key"],
                },
            )
    included_keys = {sheet["sheet_key"] for sheet in included}
    sheet_names = {sheet["sheet_key"]: sheet["sheet_name"] for sheet in selected_sheets}

    def original_source(item: dict[str, Any]) -> dict[str, Any]:
        return import_origin_of(item, sheet_names=sheet_names, import_job_no=import_job_no)

    def with_nested_origins(
        rows: list[dict[str, Any]], original_rows: list[dict[str, Any]], key: str
    ) -> list[dict[str, Any]]:
        originals = {row[key]: row for row in original_rows}
        return [
            {
                **row,
                **({"origin": original_source(originals[row[key]])}
                   if row[key] in originals and originals[row[key]].get("source") else {}),
            }
            for row in rows
        ]

    materials = [
        item for sheet in included for item in sheet.get("materials", [])
        if _accepted(decisions, "MATERIAL", item["candidate_ref"])
    ]
    fields = [
        item for sheet in included for item in sheet.get("fields", [])
        if _accepted(decisions, "FIELD", item["candidate_ref"])
    ]
    rules = [
        item for sheet in included for item in sheet.get("rules", [])
        if _accepted(decisions, "RULE", item["candidate_ref"])
    ]
    sections = [
        item for sheet in included for item in sheet.get("report_sections", [])
        if _accepted(decisions, "REPORT_SECTION", item["candidate_ref"])
    ]
    calculations = [
        item for item in resolved.get("calculations", [])
        if _accepted(decisions, "CALC_RULE", item["candidate_ref"])
        and item["source"]["sheet_key"] in included_keys
    ]
    # 按每个参数与判定区间的真实来源核对排除范围，不按工作表角色推断。
    for calculation in calculations:
        excluded_sources = sorted({
            entry["source"]["sheet_key"]
            for entry in [*(calculation.get("parameters") or []), *(calculation.get("bands") or [])]
            if entry.get("source") and entry["source"]["sheet_key"] in excluded_sheets
        })
        if excluded_sources:
            raise BusinessError(
                "MANIFEST_CALC_SHEET_EXCLUDED",
                "保留的计算候选仍使用被排除工作表中的参数或判定区间，请先修正计算配置",
                details={"rule_code": calculation["rule_code"], "excluded_sheet_keys": excluded_sources},
            )
    original_calculations = {item["candidate_ref"]: item for item in proposals.get("calculations", [])}
    calculations = [
        {
            **item,
            "origin": original_source(original_calculations[item["candidate_ref"]]),
            "parameters": with_nested_origins(
                item.get("parameters") or [],
                original_calculations[item["candidate_ref"]].get("parameters") or [],
                "parameter_code",
            ),
            "bands": with_nested_origins(
                item.get("bands") or [],
                original_calculations[item["candidate_ref"]].get("bands") or [],
                "condition_code",
            ),
        }
        for item in calculations
    ]
    _ensure_unique_materials(materials)

    def business_code(item: dict[str, Any], target_type: str) -> str:
        value = item.get("source_id")
        if target_type == "RULE" and item.get("rule_code"):
            value = item["rule_code"]
        if isinstance(value, str) and value.strip():
            return value.strip()
        # 内部候选身份与 Excel 业务编号分开；没有编号也可设计场景。
        return item["candidate_ref"]

    # 同一 Excel 业务行可以拆出多个材料位，共享 source_id。内部身份使用候选引用，
    # source_id 仅保留为来源编号，不能让资产负债表、利润表等不同材料生成同一个键。
    material_keys = {
        item["candidate_ref"]: stable_key("mat", namespace, item["candidate_ref"])
        for item in materials
    }
    # 规则执行分支也按实际材料身份区分，不能再次通过来源编号把多个材料合并。
    material_codes = dict(material_keys)
    if existing_config is not None:
        material_codes.update({
            item["material_key"]: item["material_key"]
            for item in existing_config["materials"]
        })
    field_keys = {
        item["candidate_ref"]: stable_key("fld", namespace, business_code(item, "FIELD")) for item in fields
    }
    field_source_codes = {item["candidate_ref"]: field_keys[item["candidate_ref"]] for item in fields if not item.get("source_id")}
    if existing_config is not None:
        # 增量提案用已有 field_key 引用；计算编译按 source_ref 或无编号字段键定位。
        field_source_codes.update({
            item["field_key"]: str(item.get("source_ref") or item["field_key"])
            for item in existing_config["fields"]
        })
    for calculation in calculations:
        for parameter in calculation["parameters"]:
            if parameter["source_type"] == "FIELD" and parameter["source_code"] in field_source_codes:
                parameter["source_code"] = field_source_codes[parameter["source_code"]]
    rule_keys: dict[str, str] = {}
    for item in rules:
        node_type = rule_node_type_of(item)
        material_code: str | None = None
        if node_type == NODE_TYPE_INTERNAL_VALIDATE:
            branch_refs = {entry["material_ref"] for entry in item.get("inputs", [])}
            if len(branch_refs) == 1:
                material_ref = next(iter(branch_refs))
                material_code = material_codes.get(material_ref)
                if material_code is None:
                    raise BusinessError(
                        "MANIFEST_MATERIAL_REF_MISSING",
                        "内部校验规则分支引用的材料不存在或已被排除",
                        details={"candidate_ref": item["candidate_ref"], "material_ref": material_ref},
                    )
        rule_keys[item["candidate_ref"]] = business_rule_key(
            namespace, node_type, business_code(item, "RULE"), material_code=material_code,
            material_codes=[material_codes[entry["material_ref"]] for entry in item.get("inputs", [])],
        )
    for target_type, keys in (("MATERIAL", material_keys), ("FIELD", field_keys), ("RULE", rule_keys)):
        if len(set(keys.values())) != len(keys):
            owners: dict[str, list[str]] = {}
            for reference, key in keys.items():
                owners.setdefault(key, []).append(reference)
            raise BusinessError(
                "IMPORT_BUSINESS_CODE_DUPLICATED",
                "业务编号在当前场景内无法唯一确定配置对象",
                details={"target_type": target_type, "conflicting_refs": [
                    refs for refs in owners.values() if len(refs) > 1
                ]},
            )
    material_aliases, material_sources = _alias_map(materials, material_keys, shared_source_ids=True)
    field_aliases, field_sources = _alias_map(fields, field_keys)
    rule_aliases, rule_sources = _alias_map(rules, rule_keys)
    if existing_config is not None:
        for section, key, aliases in (
            ("materials", "material_key", material_aliases),
            ("fields", "field_key", field_aliases),
            ("rules", "rule_key", rule_aliases),
        ):
            for existing in existing_config[section]:
                aliases[existing[key]] = existing[key]

    def resolve(
        reference: str, owner: dict[str, Any], target: str, *, multiple: bool = False
    ) -> str | list[str]:
        aliases, sources = {
            "MATERIAL": (material_aliases, material_sources),
            "FIELD": (field_aliases, field_sources),
            "RULE": (rule_aliases, rule_sources),
        }[target]
        options = {
            "MATERIAL": ("MANIFEST_MATERIAL_REF_MISSING", "引用的材料不存在或已被排除"),
            "FIELD": ("MANIFEST_RULE_INPUT_MISSING", "引用的字段不存在或已被排除"),
            "RULE": ("MANIFEST_RISK_SOURCE_MISSING", "引用的来源规则不存在或已被排除"),
        }
        code, message = options[target]
        args = (
            reference, owner["source"]["sheet_key"], aliases, sources,
        )
        kwargs = {
            "target_type": target,
            "error_code": code,
            "error_message": message,
            "details": {"candidate_ref": owner["candidate_ref"], "reference": reference},
        }
        if multiple:
            return _resolve_aliases(*args, **kwargs)
        return _resolve_alias(*args, **kwargs)

    manifest_like: dict[str, Any] = {
        "selected_sheets": [
            {"sheet_role": item["sheet_role"], "node_code": item["node_code"],
             "sheet_key": item["sheet_key"], "sheet_name": item["sheet_name"]}
            for item in selected_sheets if item["sheet_key"] in included_keys
        ],
        "materials": [
            {
                "material_key": material_keys[item["candidate_ref"]],
                "name": item["name"],
                "required": item["required"],
                "description": item.get("description", ""),
                "source_ref": item.get("source_id"),
                "origin": original_source(item),
            }
            for item in materials
        ],
        "fields": [],
        "rules": [],
        "report_template": [],
        "calculation_inputs": calculations,
    }
    field_materials: dict[str, str] = {
        item["field_key"]: item["material_key"]
        for item in (existing_config["fields"] if existing_config is not None else [])
    }
    for item in fields:
        material_key = resolve(item["material_ref"], item, "MATERIAL")
        field_key = field_keys[item["candidate_ref"]]
        field_materials[field_key] = material_key
        manifest_like["fields"].append({
            "field_key": field_key,
            "material_key": material_key,
            "name": item["name"],
            "value_type": item["value_type"],
            "node_code": item.get("node_code"),
            "unit": item.get("unit"),
            "currency": item.get("currency"),
            "artifact_source": item.get("artifact_source"),
            "extraction_strategy": item.get("extraction_strategy"),
            "structure_schema": item.get("structure_schema"),
            "period_scope": item.get("period_scope") or SCOPE_NOT_APPLICABLE,
            "statement_scope": item.get("statement_scope") or SCOPE_NOT_APPLICABLE,
            "required": item["required"],
            "instruction": item["instruction"],
            "source_ref": item.get("source_id"),
            "origin": original_source(item),
        })
    for item in rules:
        inputs = [
            {
                "field_key": resolve(entry["field_ref"], item, "FIELD"),
                "material_key": resolve(entry["material_ref"], item, "MATERIAL"),
            }
            for entry in item.get("inputs", [])
        ]
        for entry in inputs:
            if field_materials[entry["field_key"]] != entry["material_key"]:
                raise BusinessError(
                    "MANIFEST_RULE_INPUT_MATERIAL_MISMATCH",
                    "规则输入材料与字段所属材料不一致",
                    details={"rule": item["name"], "candidate_ref": item["candidate_ref"]},
                )
        source_keys: list[str] = []
        for reference in dict.fromkeys([
            *(item.get("source_rule_refs") or []),
            *([item["source_rule_ref"]] if item.get("source_rule_ref") else []),
        ]):
            for key in resolve(reference, item, "RULE", multiple=True):
                if key not in source_keys:
                    source_keys.append(key)
        material_scope = [resolve(ref, item, "MATERIAL") for ref in item.get("material_refs", [])]
        output_keys = [resolve(ref, item, "FIELD") for ref in item.get("output_field_refs", [])]
        manifest_like["rules"].append({
            "rule_key": rule_keys[item["candidate_ref"]],
            "rule_code": item.get("rule_code") or item.get("source_id"),
            "name": item["name"],
            "node": item["node"],
            "operator": item["operator"],
            "enabled": item.get("config_status") != CONFIG_STATUS_DISABLED,
            "inputs": inputs,
            "outputs": output_keys,
            "materials": material_scope,
            "parameters": item.get("parameters") or {},
            "requirement": item.get("requirement") or "",
            "description": (item.get("requirement") or "") if item.get("operator") == "RISK_TRIGGER" else "",
            "applicability": _compiled_applicability(item, inputs, material_scope),
            "source_node": item.get("source_node"),
            "source_rule_key": source_keys[0] if source_keys else None,
            "source_rule_keys": source_keys,
            "source_rule_codes": list(item.get("source_rule_codes") or []),
            "check_level": item.get("check_level"),
            "check_operator": item.get("check_operator"),
            "risk_level_value": item.get("risk_level_value"),
            "risk_condition": item.get("risk_condition"),
            "config_status": item.get("config_status"),
            "quality_supported": item.get("quality_supported"),
            "unsupported_capability": item.get("unsupported_capability"),
            "source_ref": item.get("rule_code") or item.get("source_id"),
            "origin": original_source(item),
        })
    for item in sections:
        field_scope = [resolve(ref, item, "FIELD") for ref in item.get("field_refs", [])]
        material_scope = [resolve(ref, item, "MATERIAL") for ref in item.get("material_refs", [])]
        for field_key in field_scope:
            if material_scope and field_materials[field_key] not in material_scope:
                raise BusinessError(
                    "MANIFEST_REPORT_FIELD_OUT_OF_SCOPE",
                    "报告章节的展示字段不属于该报告的材料范围",
                    details={"section": item["section_name"], "field_key": field_key},
                )
        report_code = item.get("report_code")
        section_order = item.get("section_order")
        section_identity = (json.dumps([report_code.strip(), section_order], ensure_ascii=False, separators=(",", ":"))
                            if isinstance(report_code, str) and report_code.strip() and type(section_order) is int
                            else item["candidate_ref"])
        manifest_like["report_template"].append({
            "section_key": stable_key("sec", namespace, section_identity),
            "section_name": item["section_name"],
            "content_instruction": item["content_instruction"],
            "field_keys": field_scope,
            "material_keys": material_scope,
            "report_code": item.get("report_code"),
            "report_name": item.get("report_name"),
            "formats": list(item.get("formats") or []),
            "section_order": item.get("section_order"),
            "content_type": item.get("content_type"),
            "result_sort": item.get("result_sort"),
            "risk_level_order": list(item.get("risk_level_order") or []),
            "config_status": item.get("config_status"),
            "origin": original_source(item),
        })
    manifest_like["disabled_source_rows"] = [
        {
            "row_key": stable_key(
                "disabled", namespace,
                json.dumps(
                    [sheet["suggested_role"], row["source"]["sheet_key"], row["source"]["row_no"]],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ),
            "sheet_role": sheet["suggested_role"],
            "import_job_no": import_job_no,
            **row,
            "sheet_name": sheet_names[row["source"]["sheet_key"]],
        }
        for sheet in included
        for row in sheet.get("disabled_rows", [])
    ]
    return editable_config_of(manifest_like, namespace=namespace)


# 字段「取数期间 / 报表口径」两个列的业务中文名。发布编译、运行期写事实、
# 草稿表单校验三处都要用它组报错文案，只维护一份。
FIELD_SCOPE_LABELS = {"period_scope": "取数期间", "statement_scope": "报表口径"}


def required_field_scope(
    field: dict[str, Any],
    key: str,
    *,
    code: str = "MANIFEST_FIELD_SCOPE_MISSING",
    status_code: int = 400,
) -> str:
    """字段口径的内部归一化。

    业务字段不必属于财报期间或报表口径体系；缺省只表示该口径不适用，
    不猜测本期、上期、合并或母公司。数据库事实定位仍使用显式 sentinel。
    `code` 与 `status_code` 保留兼容参数，缺省时不再抛出缺项错误。
    """
    value = field.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return SCOPE_NOT_APPLICABLE
    if not isinstance(value, str):
        raise BusinessError(
            "MANIFEST_FIELD_SCOPE_INVALID",
            f"字段“{field.get('name')}”的{FIELD_SCOPE_LABELS.get(key, key)}取值无效",
            status_code=status_code,
            details={"field_key": field.get("field_key"), "column": key, "value": value},
        )
    return value.strip()


def editable_config_of(manifest: dict[str, Any], *, namespace: str) -> dict[str, Any]:
    """已发布 manifest → 可编辑配置行的唯一映射口径。

    两个调用方都从这里取：场景缺草稿时用最新已发布版本初始化、创建方案时把场景版本
    复制成方案自己的配置。只允许一处口径——各写一份必然漂移，表现是同一个版本经两条
    路径回来的配置不一样，而且不报错。

    manifest 有 `section_key` 时直接保留；历史版本没有该字段时按名称、序号与 `namespace`
    派生，避免旧版本无法初始化草稿。

    返回的每项都是不含归属 id 的字典；`rules` 不跳过系统派生规则，是否保留由调用方决定。
    """
    applicability_of = lambda rule: rule.get("applicability") if isinstance(rule.get("applicability"), dict) else {}
    optional = lambda key, value: {key: value} if value is not None else {}

    nodes = [
        {
            "node_code": str(node["node_code"]),
            "node_type": str(node.get("node_type") or node["node_code"]),
            "name": str(node.get("name") or node["node_code"]),
            "sequence_no": int(node.get("sequence_no") or 1),
            "depends_on": [str(code) for code in (node.get("depends_on") or [])],
            "config_json": node.get("config_json"),
            "enabled": bool(node.get("enabled", True)),
        }
        for node in manifest.get("nodes", [])
    ]
    sheets = [
        {"sheet_role": str(sheet["sheet_role"]), "node_code": sheet.get("node_code"),
         **{key: sheet[key] for key in ("sheet_key", "sheet_name") if key in sheet}}
        for sheet in manifest.get("selected_sheets", [])
    ]
    materials = [
        {
            "material_key": material["material_key"],
            "name": material["name"],
            "required": bool(material.get("required")),
            "description": str(material.get("description") or ""),
            # 来源业务编号：导出时要按它写回 Excel 的「材料编号」，丢掉会让导出缺必填编号。
            "source_ref": material.get("source_ref"),
            "origin_json": material.get("origin"),
        }
        for material in manifest.get("materials", [])
    ]
    fields = [
        {
            "field_key": field["field_key"],
            "material_key": field["material_key"],
            "name": field["name"],
            "value_type": field["value_type"],
            "node_code": field.get("node_code"),
            "extraction_strategy": field_strategy.strategy_of(field),
            "structure_schema_json": field.get("structure_schema"),
            "unit": field.get("unit"),
            # 币种是金额比较与加总的口径之一，不能只保留单位。
            "currency": field.get("currency"),
            # 产物来源：主体核对据此确定字段取自哪种产物，必须保留。
            "artifact_source": field.get("artifact_source"),
            # 取数期间与报表口径：计算参数按它们定位字段事实。
            "period_scope": required_field_scope(field, "period_scope", code="MANIFEST_FIELD_SCOPE_MISSING"),
            "statement_scope": required_field_scope(field, "statement_scope", code="MANIFEST_FIELD_SCOPE_MISSING"),
            "required": bool(field.get("required")),
            "instruction": str(field.get("instruction") or ""),
            "source_ref": field.get("source_ref"),
            "origin_json": field.get("origin"),
        }
        for field in manifest.get("fields", [])
    ]
    rules = []
    for rule in manifest.get("rules", []):
        applicability = applicability_of(rule)
        rules.append(
            {
                "rule_key": rule["rule_key"],
                "name": rule["name"],
                "node_code": rule["node"],
                "operator_code": rule["operator"],
                "enabled": bool(rule.get("enabled", True)),
                # 是不是系统派生规则。判据只有一处（`is_system_rule`），调用方据此决定去留：
                # 场景草稿丢掉它们（发布时重新派生），方案保留它们（用户可逐条取消）。
                "is_system": is_system_rule(rule),
                "rule_json": {
                    "rule_code": rule.get("rule_code"),
                    "name": rule["name"],
                    "node": rule["node"],
                    "operator": rule["operator"],
                    "enabled": bool(rule.get("enabled", True)),
                    "inputs": [
                        {"field_key": item["field_key"], "material_key": item["material_key"]}
                        for item in rule.get("inputs", [])
                    ],
                    "outputs": list(rule.get("outputs") or []),
                    "materials": list(rule.get("materials", [])),
                    "parameters": dict(rule.get("parameters") or {}),
                    "requirement": str(rule.get("requirement") or ""),
                    "description": str(rule.get("description") or ""),
                    "applicability": {
                        "kind": applicability.get("kind", "ALWAYS"),
                        **(
                            {"material_keys": list(applicability.get("material_keys") or [])}
                            if applicability.get("material_keys")
                            else {}
                        ),
                    },
                    "source_node": rule.get("source_node"),
                    "source_rule_key": rule.get("source_rule_key"),
                    # 以下业务配置事实是人工维护的审核口径，页面表单不一定每一项都能编辑，
                    # 整块覆盖 rule_json 时不能丢：检查等级与「合格要求」原文、风险等级原文与
                    # 成立条件、风险的多来源、业务配置状态与能力缺口标记。
                    **optional("check_level", rule.get("check_level")),
                    **optional("check_operator", rule.get("check_operator")),
                    **optional("risk_level_value", rule.get("risk_level_value")),
                    **optional("risk_condition", rule.get("risk_condition")),
                    **(
                        {"source_rule_keys": list(rule["source_rule_keys"])}
                        if rule.get("source_rule_keys")
                        else {}
                    ),
                    **(
                        {"source_rule_codes": list(rule["source_rule_codes"])}
                        if rule.get("source_rule_codes")
                        else {}
                    ),
                    **optional("config_status", rule.get("config_status")),
                    **optional("quality_supported", rule.get("quality_supported")),
                    **optional("unsupported_capability", rule.get("unsupported_capability")),
                },
                "source_ref": rule.get("source_ref"),
                "origin_json": rule.get("origin"),
            }
        )
    sections = []
    for index, section in enumerate(manifest.get("report_template", []), start=1):
        section_key = section.get("section_key")
        if "section_key" in section:
            if not isinstance(section_key, str) or not section_key.strip():
                raise BusinessError(
                    "MANIFEST_SECTION_KEY_INVALID",
                    "已发布报告章节的稳定键无效",
                    details={"section_name": section.get("section_name"), "section_key": section_key},
                )
        else:
            section_key = stable_key(
                "sec", namespace, section.get("section_name", f"section-{index}") + f"-{index}"
            )
        sections.append({
            "section_key": section_key,
            "title": section.get("section_name", f"章节{index}"),
            "content_instruction": str(section.get("content_instruction") or ""),
            "field_keys": list(section.get("field_keys", [])),
            # 节点版的报告分组属性：少了它们，页面保存一次就会把按编号分组的报告
            # 退回成旧的「附加展示章节」。
            "report_code": section.get("report_code"),
            "report_name": section.get("report_name"),
            "material_keys": list(section.get("material_keys") or []) or None,
            "formats": list(section.get("formats") or []) or None,
            "section_order": section.get("section_order"),
            "content_type": section.get("content_type"),
            "result_sort": section.get("result_sort"),
            "risk_level_order": list(section.get("risk_level_order") or []),
            "config_status": section.get("config_status"),
            "origin_json": section.get("origin"),
        })
    inputs = manifest.get("calculation_inputs")
    compiled_calculations = manifest.get("calculations")
    if isinstance(inputs, list) and inputs:
        calculation_json: Any = inputs
    elif isinstance(compiled_calculations, list) and compiled_calculations:
        # 历史版本只有编译产物：保留显式业务定义，后续按当前字段重新编译。
        calculation_json = {"frozen": compiled_calculations}
    else:
        calculation_json = []
    return {
        "nodes": nodes,
        "sheets": sheets,
        "materials": materials,
        "fields": fields,
        "rules": rules,
        "sections": sections,
        "disabled_source_rows": list(manifest.get("disabled_source_rows") or []),
        "calculation_json": calculation_json,
    }


def _config_value(item: Any, key: str, default: Any = None) -> Any:
    """从配置行取一个列值。行可以是 ORM 行，也可以是字典。

    方案的节点与选用 Sheet 以只读 JSON 冻结（不是表行），场景的则是 `scenario_draft_*` 行；
    两者的列名完全一致，因此同一份映射能服务两边。
    """
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def compile_inputs_of_config(
    *,
    materials: list[Any],
    fields: list[Any],
    rules: list[Any],
    sections: list[Any],
    nodes: list[Any],
    sheets: list[Any],
    profile_version: str | None,
    scenario_schema_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """可编辑配置行 → `compile_form_manifest` 的入参。唯一映射口径。

    场景草稿发布与方案保存都从这里取 —— 各写一份必然漂移，表现是「同一个版本经两条
    路径编译出的产物不一样」，而且不会报错。行的类型允许混用（ORM 行 / 字典），
    见 `_config_value`。
    """
    # 规则的节点实例码 → 能力类型：编译期比的是能力类型，实例码是用户自己起的名字。
    node_type_by_code = {
        str(_config_value(node, "node_code")): _config_value(node, "node_type") for node in nodes
    }
    rule_items = [
        {
            **(_config_value(item, "rule_json") or {}),
            "rule_key": _config_value(item, "rule_key"),
            "name": _config_value(item, "name"),
            "node": _config_value(item, "node_code"),
            "node_type": node_type_by_code.get(_config_value(item, "node_code")),
            "operator": _config_value(item, "operator_code"),
            "enabled": bool(_config_value(item, "enabled")),
            "source_ref": _config_value(item, "source_ref"),
            "origin": _config_value(item, "origin_json"),
        }
        for item in rules
    ]
    # 风险规则通过来源规则触发，不直接维护材料范围。编译前把来源规则的材料、
    # 输入字段所属材料和输出字段所属材料汇总进风险规则，保证展示与执行口径一致。
    field_materials = {
        str(_config_value(item, "field_key")): str(_config_value(item, "material_key"))
        for item in fields
    }
    rules_by_key = {str(item["rule_key"]): item for item in rule_items}
    for rule in rule_items:
        if not rule.get("enabled", True) or rule.get("config_status") == CONFIG_STATUS_DISABLED or rule.get("operator") != "RISK_TRIGGER":
            continue
        material_keys = list(dict.fromkeys(str(key) for key in (rule.get("materials") or []) if str(key)))
        source_keys = list(dict.fromkeys([
            *(rule.get("source_rule_keys") or []),
            *([rule["source_rule_key"]] if rule.get("source_rule_key") else []),
        ]))
        for source_key in source_keys:
            source = rules_by_key.get(str(source_key))
            if source is None:
                continue
            for material_key in source.get("materials") or []:
                if material_key not in material_keys:
                    material_keys.append(material_key)
            for source_input in source.get("inputs") or []:
                material_key = source_input.get("material_key")
                if material_key and material_key not in material_keys:
                    material_keys.append(material_key)
            for field_key in source.get("outputs") or []:
                material_key = field_materials.get(str(field_key))
                if material_key and material_key not in material_keys:
                    material_keys.append(material_key)
        rule["materials"] = material_keys
    return {
        "materials": [
            {
                "material_key": _config_value(item, "material_key"),
                "name": _config_value(item, "name"),
                "required": bool(_config_value(item, "required")),
                "description": str(_config_value(item, "description") or ""),
                # 来源业务编号进入编译结果，导出时按它写回 Excel 对应列。
                "source_ref": _config_value(item, "source_ref"),
                "origin": _config_value(item, "origin_json"),
            }
            for item in materials
        ],
        "fields": [
            {
                "field_key": _config_value(item, "field_key"),
                "material_key": _config_value(item, "material_key"),
                "name": _config_value(item, "name"),
                "value_type": _config_value(item, "value_type"),
                "node_code": _config_value(item, "node_code"),
                "extraction_strategy": _config_value(item, "extraction_strategy"),
                "structure_schema": _config_value(item, "structure_schema_json"),
                "unit": _config_value(item, "unit"),
                # 币种是金额比较与加总的口径之一；产物来源决定主体核对从哪种产物取值。
                "currency": _config_value(item, "currency"),
                "artifact_source": _config_value(item, "artifact_source"),
                # 口径只在业务实际涉及时由配置声明；缺省在进入 manifest 前归一为不适用。
                "period_scope": _config_value(item, "period_scope") or SCOPE_NOT_APPLICABLE,
                "statement_scope": _config_value(item, "statement_scope") or SCOPE_NOT_APPLICABLE,
                "required": bool(_config_value(item, "required")),
                "instruction": str(_config_value(item, "instruction") or ""),
                "source_ref": _config_value(item, "source_ref"),
                "origin": _config_value(item, "origin_json"),
            }
            for item in fields
        ],
        "rules": rule_items,
        "sections": [
            {
                "section_key": _config_value(item, "section_key"),
                "section_name": _config_value(item, "title"),
                "content_instruction": _config_value(item, "content_instruction"),
                "field_keys": list(_config_value(item, "field_keys") or []),
                "report_code": _config_value(item, "report_code"),
                "report_name": _config_value(item, "report_name"),
                "material_keys": list(_config_value(item, "material_keys") or []),
                "formats": list(_config_value(item, "formats") or []),
                "section_order": _config_value(item, "section_order"),
                "content_type": _config_value(item, "content_type"),
                "result_sort": _config_value(item, "result_sort"),
                "risk_level_order": list(_config_value(item, "risk_level_order") or []),
                "config_status": _config_value(item, "config_status"),
                "origin": _config_value(item, "origin_json"),
            }
            for item in sections
        ],
        # 节点实例是用户配置的流程：原样固化，不由系统按规则内容生成。
        "node_instances": [
            {
                "node_code": _config_value(node, "node_code"),
                "node_type": _config_value(node, "node_type"),
                "name": _config_value(node, "name"),
                "sequence_no": _config_value(node, "sequence_no"),
                "depends_on": list(_config_value(node, "depends_on") or []),
                "config_json": _config_value(node, "config_json"),
                "enabled": bool(_config_value(node, "enabled")),
            }
            for node in nodes
        ],
        # 保留真实来源表身份；自由布局工作表不与单个节点绑定。
        "selected_sheets": [
            {"sheet_role": _config_value(item, "sheet_role"), "node_code": _config_value(item, "node_code"),
             **{key: _config_value(item, key) for key in ("sheet_key", "sheet_name")
                if _config_value(item, key) is not None}}
            for item in sheets
        ],
        # 节点能力目录的版本身份，记录生成配置时使用的能力版本；
        # 由调用方解析（没记来源时用当前版本），不在这里默认——那会让本函数反向依赖协议模块。
        "profile_version": profile_version,
        "scenario_schema_config": scenario_schema_config,
    }


def calc_field_scope(field: dict[str, Any], key: str) -> str:
    """计算引用字段自身声明的口径；不涉及时使用内部不适用标记。"""
    return required_field_scope(field, key)



def compile_calculations(
    calculations: list[dict[str, Any]],
    *,
    field_code_index: dict[str, dict[str, Any]],
    node_instances: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把计算规则候选编译成已发布配置里的计算段。

    保留完整业务方法与自然语言判断标准，核对真实输入引用、取值定位、节点依赖与循环。
    产物随 manifest 冻结，业务计算与判断由 LLM 执行。

    `field_code_index` 把业务字段编号映射到字段定义：计算参数按业务编号引用字段，而
    已发布配置里的字段身份是 field_key，两者必须在这里对上，运行期不再转换。

    计算规则必须落在用户配置的计算节点实例上（候选自带 `node_code`）。缺失时按能力类型
    取唯一实例——同类多个时无法确定归属，明确报错而不是静默挑一个。
    """
    calculations = [item for item in calculations if item.get("status") != CONFIG_STATUS_DISABLED]
    if not calculations:
        return []
    calculations = refresh_calculation_references(calculations)
    # 原文缺项随定义保留，是否能完成由 LLM 在运行时说明。
    definitions: list[calc.CalculationDefinition] = []
    parameters_by_rule: dict[str, list[calc.CalculationParameter]] = {}
    bands_by_rule: dict[str, list[calc.JudgementBand]] = {}
    for item in calculations:
        rule_code = str(item["rule_code"])
        parameters_by_rule.setdefault(rule_code, [])
        bands_by_rule.setdefault(rule_code, [])
    field_catalog: dict[str, calc.FieldReference] = {}
    for item in calculations:
        definitions.append(_calculation_definition(item))
        rule_code = str(item["rule_code"])
        for parameter in item.get("parameters") or []:
            parameters_by_rule[rule_code].append(_calculation_parameter(parameter))
            if parameter.get("source_type") != "FIELD":
                continue
            field_code = str(parameter.get("source_code") or "")
            field = field_code_index.get(field_code)
            if field is None:
                # 计算参数引用的字段编号在字段集合里不存在：这是配置错误，
                # 必须在发布期报出，不能留到运行期变成「字段事实取不到」。
                raise BusinessError(
                    "MANIFEST_CALC_FIELD_MISSING",
                    f"计算规则“{rule_code}”的参数“{parameter.get('parameter_code')}”"
                    f"引用的字段编号“{field_code}”不在本场景的字段集合里",
                    details={"rule_code": rule_code, "field_code": field_code},
                )
            field_catalog[field_code] = calc.FieldReference(
                field_code=field_code,
                field_key=str(field["field_key"]),
                name=str(field["name"]),
                value_type=str(field.get("value_type") or "DECIMAL"),
                # 单位与币种来自字段配置，执行时连同参数声明交给模型解释，不代填。
                unit=field.get("unit"),
                currency=field.get("currency"),
                material_key=str(field.get("material_key") or ""),
                material_name="",
                # 取数期间与报表口径来自字段配置；不涉及相应口径时使用不适用标记。
                period_scope=calc_field_scope(field, "period_scope"),
                statement_scope=calc_field_scope(field, "statement_scope"),
            )
        for band in item.get("bands") or []:
            bands_by_rule[rule_code].append(_judgement_band(band))
    # 全量编译保留跨节点依赖校验，每条计算使用自身配置的节点实例。
    node_codes = {
        str(item["rule_code"]): (
            str(item["node_code"]) if item.get("node_code") else instance_code_of_type(
                node_instances, NODE_TYPE_CALCULATION, purpose="计算"
            )
        )
        for item in calculations
    }
    outcome = calc.compile_calculation_rules(
        definitions=definitions,
        parameters_by_rule=parameters_by_rule,
        bands_by_rule=bands_by_rule,
        field_catalog=field_catalog,
        node_instance_code=node_codes,
    )
    # 保留显式用途与原始判断标准；缺项由运行时说明。
    payload: list[dict[str, Any]] = []
    sources_by_rule = {str(item["rule_code"]): item for item in calculations}
    for rule in outcome.rules:
        source = sources_by_rule[rule.definition.rule_code]
        payload.append(
            {
                "rule_code": rule.definition.rule_code,
                "rule_name": rule.definition.rule_name,
                "calculation_type": rule.definition.calculation_type,
                "execution_engine": rule.definition.execution_engine,
                "judgement_instruction": rule.definition.judgement_instruction,
                "execution_mode": rule.definition.execution_mode,
                "formula": rule.definition.formula,
                "result_code": rule.definition.result_code,
                "result_type": rule.definition.result_type,
                "unit": rule.definition.unit,
                "currency": rule.definition.currency,
                "period_policy": rule.definition.period_policy,
                # 报表口径必须随编译产物一起投影：派生事实按它落库，导出也按它写回
                # 「报表口径」列。漏了它，导出的该列为空，重新导入直接报缺列。
                "statement_scope": rule.definition.statement_scope,
                "status": rule.definition.status,
                "remark": rule.definition.remark,
                "parameters": [item.model_dump(mode="json") for item in rule.parameters],
                # 每个参数解析后的定位口径（期间 + 报表口径）：执行期按它建立字段事实的
                # 定位键。漏了它，运行期取不到任何事实，报「输入缺失」而看不出原因。
                "parameter_scopes": dict(rule.parameter_scopes),
                "bands": [item.model_dump(mode="json") for item in rule.bands],
                "depends_on_result_codes": rule.depends_on_result_codes,
                "depends_on_field_codes": rule.depends_on_field_codes,
                "node_instance_code": rule.node_instance_code,
                # 业务说明随计算规则冻结，供 LLM 使用并保留在导出配置中。
                "category": str(rule.definition.category),
                "description": str(rule.definition.description),
                "risk_level_text": str(rule.definition.risk_level_text),
                "bands_text": str(rule.definition.bands_text),
                "verification_logic": str(rule.definition.verification_logic),
                "review_advice": str(rule.definition.review_advice),
                **({"origin": source["origin"]} if source.get("origin") else {}),
                "parameter_origins": {
                    **(source.get("parameter_origins") or {}),
                    **{
                        item["parameter_code"]: item["origin"]
                        for item in source.get("parameters") or [] if item.get("origin")
                    },
                },
                "band_origins": {
                    **(source.get("band_origins") or {}),
                    **{
                        item["condition_code"]: item["origin"]
                        for item in source.get("bands") or [] if item.get("origin")
                    },
                },
            }
        )
    return payload


def _calculation_definition(item: dict[str, Any]) -> Any:
    calc.assert_llm_engine(str(item.get("rule_code") or ""), item.get("execution_engine"))
    try:
        return calc.CalculationDefinition.model_validate(
            {
                "rule_code": item["rule_code"],
                "rule_name": item["rule_name"],
                "calculation_type": item["calculation_type"],
                "execution_engine": item["execution_engine"],
                "judgement_instruction": item.get("judgement_instruction", ""),
                "execution_mode": item.get("execution_mode", "CHECK"),
                "formula": item["formula"],
                "result_code": item["result_code"],
                "result_type": item["result_type"],
                "unit": item.get("unit"),
                "currency": item.get("currency"),
                "period_policy": item.get("period_policy") or SCOPE_NOT_APPLICABLE,
                # 计算结果的报表口径：派生事实按它落库，下游引用与报告都读它；
                # 规则不涉及报表口径时由编译层归一为不适用。
                "statement_scope": item.get("statement_scope") or SCOPE_NOT_APPLICABLE,
                "status": item.get("status") or "CLEAR",
                "remark": item.get("remark") or "",
                # 业务说明类配置事实：随定义一起进入已发布配置，导出时原样写回。
                "category": item.get("category") or "",
                "description": item.get("description") or "",
                "risk_level_text": item.get("risk_level_text") or "",
                "bands_text": item.get("bands_text") or "",
                "verification_logic": item.get("verification_logic") or "",
                "review_advice": item.get("review_advice") or "",
            }
        )
    except Exception as exc:  # noqa: BLE001 - 契约错误转成带定位的业务错误
        raise BusinessError(
            "MANIFEST_CALC_DEFINITION_INVALID",
            f"计算规则“{item.get('rule_code')}”的定义不符合计算契约：{exc}",
            details={"rule_code": item.get("rule_code")},
        ) from exc


def _calculation_parameter(item: dict[str, Any]) -> Any:
    try:
        return calc.CalculationParameter.model_validate(
            {key: value for key, value in item.items() if key not in {"source", "origin"}}
        )
    except Exception as exc:  # noqa: BLE001
        raise BusinessError(
            "MANIFEST_CALC_PARAMETER_INVALID",
            f"计算参数“{item.get('parameter_code')}”不符合计算契约：{exc}",
            details={"parameter_code": item.get("parameter_code")},
        ) from exc


def _judgement_band(item: dict[str, Any]) -> Any:
    try:
        return calc.JudgementBand.model_validate(
            {key: value for key, value in item.items() if key not in {"source", "origin"}}
        )
    except Exception as exc:  # noqa: BLE001
        raise BusinessError(
            "MANIFEST_CALC_BAND_INVALID",
            f"计算判定“{item.get('condition_code')}”不符合判定契约：{exc}",
            details={"condition_code": item.get("condition_code")},
        ) from exc


def build_node_plan(
    rules: list[dict[str, Any]],
    calculations: list[dict[str, Any]] | None = None,
    node_instances: list[dict[str, Any]] | None = None,
    fields: list[dict[str, Any]] | None = None,
    materials: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """把用户配置的节点实例编译成 manifest 的节点计划。

    节点的存在、顺序与依赖完全来自用户保存的节点实例：`node_code` 是实例码（同一场景
    可以有两个同类节点），`node_type` 才是能力类型；`sequence_no` 只用于展示，
    `depends_on` 是执行依据；缺失依赖、自依赖、循环依赖与顺序冲突在发布期校验。

    一条规则指向不存在的节点实例时显式报错——静默丢掉它会让一条已声明的检查在执行时消失。
    """
    instances = list(node_instances or [])
    if not instances:
        raise BusinessError(
            "MANIFEST_NODE_REQUIRED",
            "场景没有配置任何业务节点，无法发布：节点与节点顺序必须由用户配置",
            details={"rule_count": len(rules)},
        )
    known_codes = {str(item["node_code"]) for item in instances}
    # 规则与计算规则都必须落在已配置的节点实例上：计算规则也携带 `node_code`，
    # 只校验普通规则会让一条计算规则挂到不存在的实例上，直到运行期才暴露。
    unknown_rules = sorted(
        {
            str(rule.get("node"))
            for rule in rules
            if rule.get("enabled", True) and str(rule.get("node")) not in known_codes
        }
        | {
            # 编译后的计算规则归属键是 `node_instance_code`（实例码）；
            # 编译候选用的是 `node_code`。本函数拿到的是编译产物，
            # 读错键会抛 KeyError 而不是报出「规则指向不存在的节点」。
            str(item.get("node_instance_code") or item.get("node_code"))
            for item in (calculations or [])
            if str(item.get("node_instance_code") or item.get("node_code")) not in known_codes
        }
    )
    if unknown_rules:
        raise BusinessError(
            "MANIFEST_RULE_NODE_MISSING",
            "以下规则指向的节点不在本场景配置的节点清单里："
            + "、".join(f"“{item}”" for item in unknown_rules)
            + "。请先在场景配置里建立对应节点，或把这些规则改挂到已配置的节点上",
            details={"node_codes": unknown_rules, "configured_nodes": sorted(known_codes)},
        )
    calculation_codes_by_node: dict[str, list[str]] = {}
    for calculation in calculations or []:
        if calculation.get("status") == CONFIG_STATUS_DISABLED:
            continue
        node_code = str(calculation.get("node_instance_code") or calculation.get("node_code"))
        calculation_codes_by_node.setdefault(node_code, []).append(str(calculation["rule_code"]))
    plans: list[dict[str, Any]] = []
    for item in sorted(instances, key=lambda row: (int(row.get("sequence_no") or 0), str(row["node_code"]))):
        config = dict(item.get("config_json") or {})
        if item["node_type"] == NODE_TYPE_CALCULATION:
            config["calculation_rule_codes"] = sorted(calculation_codes_by_node.get(str(item["node_code"]), []))
        plans.append(
            {
                "node_code": str(item["node_code"]),
                "node_type": str(item["node_type"]),
                "name": str(item.get("name") or item["node_code"]),
                "sequence_no": int(item.get("sequence_no") or 0),
                "depends_on": [str(code) for code in (item.get("depends_on") or [])],
                "config_json": config or None,
                "enabled": bool(item.get("enabled", True)),
            }
        )
    # 依赖必须指向真实存在的实例，且不得自依赖/成环/依赖停用节点。
    workflow_dag.validate_dag(workflow_dag.plans_of({"nodes": plans}))
    _assert_sequence_consistent(plans)
    _assert_rule_input_dependencies(rules, plans)
    _assert_calculation_dependencies(rules, calculations or [], fields or [], plans)
    _assert_risk_source_dependencies(rules, calculations or [], fields or [], plans)
    _assert_review_dependencies(rules, calculations or [], materials or [], plans)
    _assert_report_dependencies(plans)
    return plans


def _assert_rule_input_dependencies(
    rules: list[dict[str, Any]], nodes: list[dict[str, Any]],
) -> None:
    """读取字段的规则须依赖该字段实际启用的抽取节点，不用展示顺序代替数据依赖。"""
    by_node = {item["node_code"]: item for item in nodes}
    owners = {
        field_key: rule["node"]
        for rule in rules
        if rule["operator"] == "LLM_EXTRACT" and rule.get("enabled", True)
        and by_node[rule["node"]]["enabled"]
        for field_key in rule.get("outputs", [])
    }
    for rule in rules:
        if not rule.get("enabled", True):
            continue
        node = by_node[rule["node"]]
        if not node["enabled"]:
            continue
        ancestors: set[str] = set()
        pending = list(node["depends_on"])
        while pending:
            dependency = pending.pop()
            if dependency not in ancestors:
                ancestors.add(dependency)
                pending.extend(by_node[dependency]["depends_on"])
        missing_fields = [
            entry["field_key"] for entry in rule.get("inputs", [])
            if entry["field_key"] in owners
            and owners[entry["field_key"]] != rule["node"]
            and owners[entry["field_key"]] not in ancestors
        ]
        if missing_fields:
            sources = sorted({owners[key] for key in missing_fields})
            raise BusinessError(
                "MANIFEST_RULE_INPUT_DEPENDENCY_MISSING",
                f"规则“{rule['name']}”所在节点没有依赖输入字段的抽取节点："
                + "、".join(by_node[code]["name"] for code in sources),
                details={"rule_key": rule["rule_key"], "rule_name": rule["name"],
                         "node_code": rule["node"], "field_keys": sorted(set(missing_fields)),
                         "missing_source_nodes": sources},
            )


def _assert_calculation_dependencies(
    rules: list[dict[str, Any]], calculations: list[dict[str, Any]],
    fields: list[dict[str, Any]], nodes: list[dict[str, Any]],
) -> None:
    """计算必须在输入抽取和跨节点的上游计算完成后执行。"""
    by_node = {item["node_code"]: item for item in nodes}
    result_owners = {item["result_code"]: item["node_instance_code"] for item in calculations}
    field_keys = {(item.get("source_ref") or item["field_key"]): item["field_key"] for item in fields}
    extraction_owners = {
        key: rule["node"] for rule in rules
        if rule["operator"] == "LLM_EXTRACT" and rule.get("enabled", True)
        and by_node[rule["node"]]["enabled"]
        for key in rule["outputs"]
    }
    # 派生抽取规则在调用本函数前已生成。字段归属不代表启用取数；关闭抽取时
    # 仍允许消费已有有效事实，没有事实则由执行器明确返回输入缺失。
    for calculation in calculations:
        code = calculation["node_instance_code"]
        node = by_node[code]
        if not node["enabled"] or node["node_type"] != NODE_TYPE_CALCULATION:
            raise BusinessError("MANIFEST_CALC_NODE_INVALID", "计算规则引用的节点未启用或不是计算节点", details={"rule_code": calculation["rule_code"], "node_code": code})
        ancestors: set[str] = set()
        pending = list(node["depends_on"])
        while pending:
            dependency = pending.pop()
            if dependency not in ancestors:
                ancestors.add(dependency)
                pending.extend(by_node[dependency]["depends_on"])
        sources = {extraction_owners[field_keys[source]] for source in calculation["depends_on_field_codes"]
                   if field_keys[source] in extraction_owners}
        sources.update(result_owners[source] for source in calculation["depends_on_result_codes"]
                       if result_owners[source] != code)
        missing = sorted(sources - ancestors)
        if missing:
            raise BusinessError("MANIFEST_CALC_DEPENDENCY_MISSING", "计算节点没有依赖全部输入来源节点", details={"rule_code": calculation["rule_code"], "node_code": code, "missing_source_nodes": missing})


def _assert_review_dependencies(
    rules: list[dict[str, Any]],
    calculations: list[dict[str, Any]],
    materials: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
) -> None:
    by_code = {item["node_code"]: item for item in nodes}
    enabled = {code: item for code, item in by_code.items() if item["enabled"]}
    review_nodes = [item for item in enabled.values() if item["node_type"] == NODE_TYPE_MANUAL_REVIEW]
    if len(review_nodes) > 1:
        raise BusinessError(
            "MANIFEST_REVIEW_NODE_AMBIGUOUS",
            "流程只能启用一个人工复核节点，否则待复核项没有唯一等待点",
            details={"node_codes": sorted(item["node_code"] for item in review_nodes)},
        )

    review_sources = {
        code for code, item in enabled.items()
        if item["node_type"] == node_registry.NODE_TYPE_DOCUMENT_PREPARE and materials
    }
    agent_sources: set[str] = set()
    for rule in rules:
        code = rule.get("node")
        if not rule.get("enabled", True) or code not in enabled:
            continue
        node_type = enabled[code]["node_type"]
        if node_type == NODE_TYPE_FIELD_EXTRACTION and rule["operator"] == "LLM_EXTRACT":
            review_sources.add(code)
        if node_type not in {NODE_TYPE_INTERNAL_VALIDATE, NODE_TYPE_CROSS_VALIDATE, NODE_TYPE_RISK_RATING}:
            continue
        capability = capabilities.capability_of(capabilities.capability_id_of_rule(rule))
        spec = tool_catalog.tool_spec(capability.capability_id if capability else None)
        if spec is not None and spec.agent_selectable:
            agent_sources.add(code)
        if node_type == NODE_TYPE_RISK_RATING:
            continue
        if (
            rule["operator"] in {"LLM_VALIDATE", "DECIMAL_CN_EQUAL", "FIELD_EQUAL"}
            or (capability is not None and capability.execution == capabilities.EXEC_SUBJECT_RETRIEVAL)
            or (rule.get("parameters") or {}).get("requires_manual_review") is True
        ):
            review_sources.add(code)
    review_sources.update(code for code in agent_sources if enabled[code]["node_type"] != NODE_TYPE_RISK_RATING)
    review_sources.update(
        str(item.get("node_instance_code") or item.get("node_code"))
        for item in calculations
        if item.get("status") != CONFIG_STATUS_DISABLED
        and (item.get("bands") or (item.get("execution_engine") == "LLM" and item.get("execution_mode") == "CHECK"))
        and str(item.get("node_instance_code") or item.get("node_code")) in enabled
    )
    if not review_sources and not agent_sources:
        return
    if not review_nodes:
        raise BusinessError(
            "MANIFEST_REVIEW_NODE_MISSING",
            "流程中的字段、规则或材料处理可能产生待复核项，但没有启用人工复核节点",
            details={"source_nodes": sorted(review_sources | agent_sources)},
        )

    review = review_nodes[0]
    ancestors: set[str] = set()
    pending = list(review["depends_on"])
    while pending:
        code = pending.pop()
        if code in ancestors:
            continue
        ancestors.add(code)
        pending.extend(by_code[code]["depends_on"])
    missing = sorted(review_sources - ancestors)
    forbidden = sorted(
        code for code in ancestors
        if by_code[code]["node_type"] in {NODE_TYPE_RISK_RATING, NODE_TYPE_REPORT_OUTPUT}
    )
    if missing or forbidden:
        raise BusinessError(
            "MANIFEST_REVIEW_DEPENDENCY_INVALID",
            "人工复核节点必须依赖所有可能产生待办的节点，且到达它的路径不能经过风险或报告节点",
            details={"review_node": review["node_code"], "missing_source_nodes": missing, "forbidden_nodes": forbidden},
        )


def _assert_report_dependencies(nodes: list[dict[str, Any]]) -> None:
    by_code = {item["node_code"]: item for item in nodes}
    enabled_codes = {
        code for code, item in by_code.items()
        if item["enabled"] and item["node_type"] != NODE_TYPE_REPORT_OUTPUT
    }
    for report in nodes:
        if report["node_type"] != NODE_TYPE_REPORT_OUTPUT or not report["enabled"]:
            continue
        ancestors: set[str] = set()
        pending = list(report["depends_on"])
        while pending:
            code = pending.pop()
            if code in ancestors:
                continue
            ancestors.add(code)
            pending.extend(by_code[code]["depends_on"])
        missing = sorted(enabled_codes - ancestors)
        if missing:
            raise BusinessError(
                "MANIFEST_REPORT_DEPENDENCY_MISSING",
                "报告输出节点必须在全部已启用审核节点完成后执行",
                details={"report_node": report["node_code"], "missing_node_codes": missing},
            )


def attach_execution_contracts(
    nodes: list[dict[str, Any]],
    rules: list[dict[str, Any]],
    calculations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Freeze each node's rule and field scope into the published workflow."""
    for node in nodes:
        node_code = node["node_code"]
        node_rules = [rule for rule in rules if rule["node"] == node_code]
        node_calculations = [
            item for item in calculations if item["node_instance_code"] == node_code
        ]
        input_fields = {
            item["field_key"]
            for rule in node_rules
            for item in rule.get("inputs", [])
        }
        output_fields = {
            field_key for rule in node_rules for field_key in rule.get("outputs", [])
        }
        node["execution_contract"] = {
            "field_keys": sorted(output_fields) if node["node_type"] == NODE_TYPE_FIELD_EXTRACTION else [],
            "rule_keys": sorted(rule["rule_key"] for rule in node_rules),
            "calculation_rule_codes": sorted(item["rule_code"] for item in node_calculations),
            "input_field_keys": sorted(input_fields),
            "output_field_keys": sorted(output_fields),
        }
    return nodes


def _assert_risk_source_dependencies(
    rules: list[dict[str, Any]],
    calculations: list[dict[str, Any]],
    fields: list[dict[str, Any]],
    nodes: list[dict[str, Any]],
) -> None:
    node_dependencies = {item["node_code"]: item["depends_on"] for item in nodes}
    node_types = {item["node_code"]: item["node_type"] for item in nodes}
    rules_by_key = {item["rule_key"]: item for item in rules}

    def ancestors_of(node_code: str) -> set[str]:
        ancestors: set[str] = set()
        pending = list(node_dependencies[node_code])
        while pending:
            dependency = pending.pop()
            if dependency in ancestors:
                continue
            ancestors.add(dependency)
            pending.extend(node_dependencies[dependency])
        return ancestors

    for rule in rules:
        if rule["operator"] != "RISK_TRIGGER" or not rule.get("enabled", True):
            continue
        source_nodes: set[str] = set()
        source_keys = list(dict.fromkeys(rule.get("source_rule_keys") or []))
        if rule.get("source_rule_key") and rule["source_rule_key"] not in source_keys:
            source_keys.insert(0, rule["source_rule_key"])
        for key in source_keys:
            source = rules_by_key.get(key)
            if source is None or not source.get("enabled", True):
                raise BusinessError("MANIFEST_RISK_SOURCE_MISSING", "风险来源规则不存在或已停用", details={"rule": rule["name"], "source_rule_key": key})
            if source["operator"] == "RISK_TRIGGER":
                raise BusinessError("MANIFEST_RISK_SOURCE_INVALID", "风险规则不能引用自身或另一条风险规则", details={"rule": rule["name"], "source_rule_key": key})
            source_nodes.add(source["node"])
        for code in rule.get("source_rule_codes") or []:
            result_matches = [
                item for item in calculations
                if item.get("result_code") == code and item.get("status") != CONFIG_STATUS_DISABLED
            ]
            field_matches = [item for item in fields if (item.get("source_ref") or item["field_key"]) == code]
            if len(result_matches) + len(field_matches) != 1:
                raise BusinessError("MANIFEST_RISK_SOURCE_MISSING", "风险来源编号不能唯一定位已启用的计算结果或字段", details={"rule": rule["name"], "source_code": code})
            if result_matches:
                source_nodes.add(str(result_matches[0]["node_instance_code"]))
                continue
            raise BusinessError("MANIFEST_RISK_FIELD_SOURCE_UNJUDGED", "风险来源是字段编号，但字段抽取成功不等于检查合格：请关联有判定标准的检查规则", details={"rule": rule["name"], "source_code": code})
        allowed_types = SOURCE_NODE_RULE_NODES.get(rule.get("source_node") or "")
        if allowed_types is not None:
            mismatched = sorted(code for code in source_nodes if node_types[code] not in allowed_types)
            if mismatched:
                raise BusinessError("MANIFEST_RISK_SOURCE_NODE_MISMATCH", "风险来源与声明的来源节点不一致", details={"rule": rule["name"], "source_nodes": mismatched, "declared_source_node": rule["source_node"]})
        available = ancestors_of(rule["node"])
        missing = sorted(source_nodes - available)
        if missing:
            raise BusinessError(
                "MANIFEST_RISK_DEPENDENCY_MISSING",
                "风险节点没有依赖全部来源节点，执行时可能先于来源产生结果",
                details={"rule": rule["name"], "risk_node": rule["node"], "missing_source_nodes": missing},
            )
        rule["source_nodes"] = sorted(set(rule.get("source_nodes") or []) | {node_types[code] for code in source_nodes})


def _assert_sequence_consistent(plans: list[dict[str, Any]]) -> None:
    """顺序冲突校验：依赖必须排在被依赖节点之后。

    `sequence_no` 只用于展示，`depends_on` 才是执行依据；两者不一致时页面显示的顺序
    与真实执行顺序相反，而依赖图仍然正确——这种不一致最难发现，因此必须在发布期拦下。
    """
    position = {str(item["node_code"]): index for index, item in enumerate(plans)}
    conflicts: list[dict[str, Any]] = []
    for item in plans:
        for dependency in item["depends_on"]:
            if position.get(str(dependency), -1) >= position[str(item["node_code"])]:
                conflicts.append(
                    {"node_code": item["node_code"], "depends_on": dependency}
                )
    if conflicts:
        raise BusinessError(
            "MANIFEST_NODE_SEQUENCE_CONFLICT",
            "节点顺序与依赖关系冲突：被依赖的节点排在了依赖它的节点之后——"
            + "、".join(f"“{item['node_code']}”依赖“{item['depends_on']}”" for item in conflicts),
            details={"conflicts": conflicts},
        )


def enabled_extraction_field_keys(
    manifest: dict[str, Any], enabled_rule_keys: list[str],
) -> set[str]:
    """实际自动抽取范围仅来自启用节点上已选中的抽取规则输出。"""
    selected_keys = set(enabled_rule_keys)
    enabled_nodes = {
        node["node_code"] for node in manifest["nodes"] if node.get("enabled", True)
    }
    return {
        field_key
        for rule in manifest["rules"]
        if rule["rule_key"] in selected_keys
        and rule.get("enabled", True)
        and rule["node"] in enabled_nodes
        and rule_node_type_of(rule) == "FIELD_EXTRACTION"
        and rule["operator"] == "LLM_EXTRACT"
        for field_key in rule.get("outputs", [])
    }


def build_task_schema(
    manifest: dict[str, Any],
    enabled_rule_keys: list[str] | None,
    *,
    allow_empty: bool = False,
) -> dict[str, Any]:
    """从已发布 manifest 算出某个任务的执行范围：就是用户勾选的那些规则。

    enabled_rule_keys=None 表示未选择过（视为全部启用）；[] 表示显式一条业务规则都没选；
    两者不能混：把 [] 当成 None 会让用户取消掉的规则重新生效。

    系统不替用户补入任何规则。 方案与任务是用户自己的范围选择：勾了什么就跑什么，
    没勾的就不跑。缺规则造成的后果由各节点如实表达（拿不到输入就报
    「无法判断」，没有规则就记 SKIPPED 并写明原因），绝不写成通过。
    """
    known_rules = {item["rule_key"]: item for item in manifest["rules"]}
    enabled_node_codes = {
        item["node_code"] for item in manifest["nodes"] if item.get("enabled", True)
    }
    available_keys = {
        key for key, rule in known_rules.items()
        if rule.get("enabled", True) and rule["node"] in enabled_node_codes
    }
    if enabled_rule_keys is None:
        selected_keys = set(available_keys)
    elif not enabled_rule_keys and not allow_empty:
        raise BusinessError("TASK_RULE_EMPTY", "创建任务时至少需要启用一条规则")
    elif not enabled_rule_keys:
        # allow_empty 仅供方案范围预览使用：空集合要如实展示「哪些规则未启用」，
        # 不能伪装成全部启用，也不能按建任务门禁直接报错。
        selected_keys = set()
    else:
        selected_keys = set(enabled_rule_keys)
    unknown = selected_keys - known_rules.keys()
    if unknown:
        raise BusinessError("TASK_RULE_UNKNOWN", "创建任务时选择了不存在的规则", details={"rule_keys": sorted(unknown)})
    disabled = selected_keys - available_keys
    if disabled:
        raise BusinessError("TASK_RULE_DISABLED", "创建任务时选择了未启用规则", details={"rule_keys": sorted(disabled)})

    fields_by_key = {item["field_key"]: item for item in manifest["fields"]}
    selected_rules = [known_rules[key] for key in sorted(selected_keys)]
    # 消费者引用的字段定义仍进入清单，用于解释类型、单位及人工值。
    # 是否自动取数独立由启用的抽取规则决定，消费者引用不能重新开启抽取。
    extraction_field_keys = enabled_extraction_field_keys(manifest, sorted(selected_keys))
    field_keys = {
        field_key for section in manifest.get("report_template", []) for field_key in section.get("field_keys", [])
    } | {
        field_key for rule in selected_rules for field_key in (item["field_key"] for item in rule.get("inputs", []))
    } | {
        field_key
        for rule in selected_rules
        if rule["operator"] == "LLM_EXTRACT"
        for field_key in rule.get("outputs", [])
    }
    calculation_field_codes = {
        str(code)
        for calculation in manifest.get("calculations", [])
        if calculation.get("status") != CONFIG_STATUS_DISABLED
        and calculation.get("node_instance_code") in enabled_node_codes
        for code in calculation.get("depends_on_field_codes", [])
    }
    fields_by_code: dict[str, dict[str, Any]] = {}
    for field in manifest["fields"]:
        code = field.get("source_ref") or field["field_key"]
        if code not in calculation_field_codes:
            continue
        if code in fields_by_code:
            raise BusinessError(
                "TASK_CALC_FIELD_AMBIGUOUS",
                "计算规则引用的字段编号对应多条字段定义，无法确定任务取数范围",
                details={"field_code": code},
            )
        fields_by_code[code] = field
    missing_calculation_fields = calculation_field_codes - fields_by_code.keys()
    if missing_calculation_fields:
        raise BusinessError(
            "TASK_CALC_FIELD_MISSING",
            "计算规则引用的字段编号不在任务字段集合中",
            details={"field_codes": sorted(missing_calculation_fields)},
        )
    field_keys.update(field["field_key"] for field in fields_by_code.values())
    missing = field_keys - fields_by_key.keys()
    if missing:
        raise BusinessError("TASK_SCHEMA_FIELD_MISSING", "任务规则引用的字段不在场景字段集合中", details={"field_keys": sorted(missing)})
    return {
        "schema_version": "1.0",
        "enabled_rule_keys": sorted(rule["rule_key"] for rule in selected_rules),
        # 模板里存在、但本任务明确未启用的规则：页面与报告据此区分"未启用"与"未配置"。
        "unenabled_rule_keys": sorted(known_rules.keys() - selected_keys),
        # 当前场景未配置的业务校验节点（场景级事实，不是任务级选择）。
        # 类型级与实例级分开返回：页面按类型判断「这类检查有没有配」，
        # 按实例码判断「当前这个节点是不是未配置的类别」。
        "unconfigured_check_node_types": list(manifest.get("unconfigured_check_node_types", [])),
        "unconfigured_check_node_codes": list(manifest.get("unconfigured_check_node_codes", [])),
        "fields": [
            {**fields_by_key[key], "extraction_enabled": key in extraction_field_keys}
            for key in sorted(field_keys)
        ],
    }


# 页面表单配置的来源标记：材料/字段/规则/报告章节由页面直接维护，没有 Excel 工作表，
# 因此 source 中不包含工作表与行号——不伪造导入作业、工作表行号或供应商结果。
FORM_SOURCE = {"sheet_key": None, "row_no": None, "cells": []}

# 场景字段维护与导入候选共用的数据类型，包含大写金额（DECIMAL_CN）。
FORM_VALUE_TYPES = {"STRING", "DECIMAL", "AMOUNT", "DECIMAL_CN", "DATE", "BOOLEAN", "CLAUSE"}


def _form_parameter_instruction(parameters: dict[str, Any]) -> Any:
    return parameters.get("instruction") if isinstance(parameters, dict) else None


def manifest_scope_hash(manifest: dict[str, Any]) -> str:
    """范围指纹：忽略编译时间等非业务字段，用于判断“两个版本内容是否一致”。

    manifest_hash 把 compiled_at 也算进去，两次编译必然不同，因此不能用它判断
    “内容没变、可以复用已发布版本”。范围指纹只覆盖业务定义（材料/字段/规则/报告章节/
    未配置节点/能力缺口），与发布时间无关。
    """
    payload = {
        key: value
        for key, value in manifest.items()
        if key not in {"manifest_hash", "compiled_at", "published_at", "source_workbook_id"}
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def assert_frozen_calculations(
    compiled: list[dict[str, Any]],
    *,
    field_codes: set[str],
    node_codes: set[str],
) -> None:
    """核对历史计算的必需结构与引用，再由重新编译刷新取数口径和依赖。

    对不上就明确失败。不把过期编译产物原样带进新版本，也不按字段名重新猜一份。
    """
    missing_fields: list[dict[str, str]] = []
    missing_nodes: list[dict[str, str]] = []
    for item in compiled:
        rule_code = str(item.get("rule_code") or "")
        definition = _calculation_definition(item)
        bands = item.get("bands")
        if not isinstance(bands, list):
            raise BusinessError(
                "FROZEN_CALC_BAND_MISSING",
                f"历史计算规则“{rule_code}”缺少判定配置列表，不能重新发布",
                details={"rule_code": rule_code},
            )
        # 历史编译产物重新发布时也必须满足当前判定契约。特别是结果状态不能
        # 留到任务运行时才发现缺失，否则同一份无效配置会再次进入新版本。
        validated_bands = []
        for band in bands:
            if not isinstance(band, dict):
                raise BusinessError(
                    "FROZEN_CALC_BAND_INVALID",
                    f"历史计算规则“{rule_code}”包含无效判定区间，不能重新发布",
                    details={"rule_code": rule_code},
                )
            validated_bands.append(_judgement_band(band))
        node_code = item.get("node_instance_code")
        if not isinstance(node_code, str) or node_code not in node_codes:
            missing_nodes.append({"rule_code": rule_code, "node_code": str(node_code or "")})
        for field_code in item.get("depends_on_field_codes") or []:
            if str(field_code) not in field_codes:
                missing_fields.append({"rule_code": rule_code, "field_code": str(field_code)})
    if missing_fields or missing_nodes:
        raise BusinessError(
            "MANIFEST_CALC_FROZEN_STALE",
            "已冻结的计算配置与当前草稿的字段编号或计算节点不一致，不能发布",
            details={"missing_fields": missing_fields, "missing_nodes": missing_nodes},
        )


def calculation_definitions_of_frozen(compiled: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """读取历史记录中明示的业务定义，不生成模型候选身份或补造来源。"""
    definitions = []
    for item in compiled:
        definition = _calculation_definition(item)
        node_code = item.get("node_instance_code")
        if not isinstance(node_code, str) or not node_code:
            raise BusinessError(
                "FROZEN_CALC_NODE_MISSING",
                f"历史计算规则“{definition.rule_code}”缺少实际节点归属，不能转换业务定义",
                details={"rule_code": definition.rule_code},
            )
        updated = deepcopy(item)
        # 兼容此前 LLM 冻结记录携带的无用 AST/哈希，不生成这些旧引擎产物。
        for key in (
            "ast", "expression_hash", "parameter_scopes", "depends_on_field_codes",
            "depends_on_result_codes", "node_instance_code",
        ):
            updated.pop(key, None)
        updated["node_code"] = node_code
        validated_bands = []
        for key, origin_key, identity in (
            ("parameters", "parameter_origins", "parameter_code"),
            ("bands", "band_origins", "condition_code"),
        ):
            entries = updated.get(key)
            if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
                raise BusinessError(
                    "FROZEN_CALC_DEFINITION_INCOMPLETE",
                    f"历史计算规则“{definition.rule_code}”缺少完整的参数或判定列表，不能转换业务定义",
                    details={"rule_code": definition.rule_code, "property": key},
                )
            origins = updated.get(origin_key) or {}
            for entry in entries:
                if key == "parameters":
                    _calculation_parameter(entry)
                else:
                    validated_bands.append(_judgement_band(entry))
                if entry.get("origin") is None and entry[identity] in origins:
                    entry["origin"] = deepcopy(origins[entry[identity]])
        definitions.append(updated)
    return definitions


def recompile_frozen_calculations(
    compiled: list[dict[str, Any]],
    *,
    field_code_index: dict[str, dict[str, Any]],
    node_instances: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """用历史配置明示的公式和参数重新编译，取数口径由当前字段决定。

    不从 AST 反推公式，不从旧取数定位猜字段，也不重建缺失的业务配置。
    node_instance_code 是历史记录的实际归属，精确传给编译器。
    """
    assert_frozen_calculations(
        compiled,
        field_codes=set(field_code_index),
        node_codes={str(item.get("node_code") or "") for item in node_instances},
    )
    inputs = calculation_definitions_of_frozen(compiled)
    refreshed = compile_calculations(
        inputs, field_code_index=field_code_index, node_instances=node_instances,
    )
    originals = {item["rule_code"]: item for item in compiled}
    # 保留业务来源等扩展元数据，执行派生属性全部使用本次编译结果。
    return [{**deepcopy(originals[item["rule_code"]]), **item} for item in refreshed]


def _has_enabled_calculation(calculations: list[dict[str, Any]]) -> bool:
    return any(item.get("status") != CONFIG_STATUS_DISABLED for item in calculations)


def compile_form_manifest(
    *,
    scene_code: str,
    scene_name: str,
    namespace: str,
    materials: list[dict[str, Any]],
    fields: list[dict[str, Any]],
    rules: list[dict[str, Any]],
    sections: list[dict[str, Any]],
    node_instances: list[dict[str, Any]],
    selected_sheets: list[dict[str, Any]],
    profile_version: str,
    compiled_at: datetime,
    scenario_schema_config: dict[str, Any] | None = None,
    calculation_candidates: list[dict[str, Any]] | None = None,
    frozen_calculations: list[dict[str, Any]] | None = None,
    disabled_source_rows: list[dict[str, Any]] | None = None,
    acknowledged_capability_gaps: list[str] | None = None,
    system_rule_enabled: dict[str, bool] | None = None,
    import_proposals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把页面表单维护的业务配置编译成与导入流程同构的 manifest。

    输入是草稿的原始业务定义（materials / fields / rules / sections）与用户配置的节点
    实例。节点不由系统按规则内容生成，因此它是必填输入——缺节点时明确报错，不补固定节点表。

    `rules` 里停用的业务校验不参与编译；独立抽取规则保留停用定义，防止被系统派生规则重启。
    停用规则不算「有规则」。字段抽取规则与
    报告生成规则按同一确定性规则派生；`system_rule_enabled` 只改变派生规则的启停，
    不改变它们的业务定义。
    """
    if not materials:
        raise BusinessError("MANIFEST_NO_MATERIAL", "场景至少需要一个材料位")
    # 「至少一条启用规则」必须真的按 `enabled=true` 过滤：把全部停用的规则也算进来，
    # 一份所有业务规则都停用的配置会被判成「有规则」而通过，发布出去却没有任何检查执行。
    business_rules = [
        item
        for item in rules
        if not is_system_rule(item)
    ]
    if not any(item.get("enabled", True) for item in business_rules) and not _has_enabled_calculation(
        list(calculation_candidates or frozen_calculations or [])
    ):
        raise BusinessError("MANIFEST_NO_RULE", "场景至少需要一条启用规则")

    material_by_key: dict[str, dict[str, Any]] = {}
    material_names_seen: dict[str, dict[str, Any]] = {}
    for item in materials:
        key = str(item.get("material_key") or "")
        name = str(item.get("name") or "").strip()
        if not key or key in material_by_key:
            raise BusinessError("MANIFEST_DUPLICATE_MATERIAL_KEY", "材料配置缺少唯一标识", details={"material_key": key})
        if not name:
            raise BusinessError("MANIFEST_MATERIAL_NAME_REQUIRED", "材料名称不能为空", details={"material_key": key})
        # 与导入路径同口径：「是不是同一个材料位」只由材料名称决定。
        signature = _normalize_text(name)
        if signature in material_names_seen:
            raise BusinessError(
                "MANIFEST_DUPLICATE_MATERIAL",
                "场景中存在重复材料位",
                details={"name": name},
            )
        material_names_seen[signature] = item
        material_by_key[key] = item

    compiled_materials = [
        {
            "material_key": item["material_key"],
            "name": item["name"].strip(),
            "required": bool(item.get("required")),
            "description": str(item.get("description") or ""),
            # 来源业务编号：由导入 / 修订带入草稿，导出时按它写回 Excel 的编号列。
            # 页面新建的行没有编号，保持 None（导出为空），不伪造一个。
            "source_ref": str(item.get("source_ref") or "").strip() or None,
            "source": dict(FORM_SOURCE),
            **({"origin": item["origin"]} if item.get("origin") else {}),
        }
        for item in materials
    ]
    # 材料引用已按 material_key 定位；同一来源编号拆出的多个材料无需重复编号校验。

    field_by_key: dict[str, dict[str, Any]] = {}
    compiled_fields: list[dict[str, Any]] = []
    field_types: dict[str, str] = {}
    field_units: dict[str, str | None] = {}
    # 币种与单位一样是金额比较与加总的口径，发布编译必须按它校验；
    # 只收单位会让页面建的规则在币种未声明或不一致时照常发布。
    field_currencies: dict[str, str | None] = {}
    field_materials: dict[str, str] = {}
    for item in fields:
        key = str(item.get("field_key") or "")
        name = str(item.get("name") or "").strip()
        value_type = str(item.get("value_type") or "")
        material_key = str(item.get("material_key") or "")
        if not key or key in field_by_key:
            raise BusinessError("MANIFEST_DUPLICATE_FIELD_KEY", "字段配置缺少唯一标识", details={"field_key": key})
        if not name:
            raise BusinessError("MANIFEST_FIELD_NAME_REQUIRED", "字段名称不能为空", details={"field_key": key})
        if value_type not in FORM_VALUE_TYPES:
            raise BusinessError(
                "MANIFEST_FIELD_TYPE_INVALID",
                "字段类型不受支持",
                details={"field_name": name, "value_type": value_type, "allowed": sorted(FORM_VALUE_TYPES)},
            )
        extraction_strategy, structure_schema = field_strategy.validate_field_strategy(
            value_type=value_type,
            extraction_strategy=item.get("extraction_strategy"),
            structure_schema=item.get("structure_schema"),
            field_name=name,
        )
        if material_key not in material_by_key:
            raise BusinessError(
                "MANIFEST_FIELD_MATERIAL_MISSING",
                "字段所属材料不存在或已被删除",
                details={"field_name": name, "material_key": material_key},
            )
        field_by_key[key] = item
        field_types[key] = value_type
        field_units[key] = item.get("unit")
        field_currencies[key] = item.get("currency")
        field_materials[key] = material_key
        compiled_fields.append(
            {
                "field_key": key,
                "name": name,
                "value_type": value_type,
                "node_code": item.get("node_code"),
                "extraction_strategy": extraction_strategy,
                "structure_schema": structure_schema,
                "unit": item.get("unit"),
                # 币种是金额比较与加总的口径之一；留空表示未声明，不表示人民币。
                "currency": item.get("currency"),
                "material_key": material_key,
                "required": bool(item.get("required")),
                "instruction": str(item.get("instruction") or ""),
                "artifact_source": item.get("artifact_source"),
                # 取数期间与报表口径仅在业务涉及时声明；缺省归一为不适用。
                "period_scope": required_field_scope(item, "period_scope"),
                "statement_scope": required_field_scope(item, "statement_scope"),
                "source_ref": str(item.get("source_ref") or "").strip() or None,
                "source": dict(FORM_SOURCE),
                **({"origin": item["origin"]} if item.get("origin") else {}),
            }
        )

    field_code_index: dict[str, dict[str, Any]] = {}
    for field in compiled_fields:
        code = field["source_ref"] or field["field_key"]
        previous = field_code_index.get(code)
        if previous is not None:
            raise BusinessError(
                "MANIFEST_DUPLICATE_FIELD_CODE",
                "字段业务编号重复，跨表计算参数无法唯一定位字段",
                details={"field_code": code, "field_keys": [previous["field_key"], field["field_key"]]},
            )
        field_code_index[code] = field

    compiled_rules: list[dict[str, Any]] = []
    rule_keys: set[str] = set()
    rule_nodes: dict[str, str] = {}
    # 规则 key → 能力类型。页面表单维护的规则同样把归属写成节点实例码，
    # 能力类型在 `node_type` 里（缺省时按旧口径取 node）。
    rule_node_types: dict[str, str] = {}
    disabled_rules = []
    for item in business_rules:
        if not item.get("enabled", True) or item.get("config_status") == CONFIG_STATUS_DISABLED:
            # 停用配置完整保留；不检查算子、参数、字段引用或来源规则。
            disabled_rules.append({**deepcopy(item), "rule_key": item.get("rule_key") or stable_key(
                                       "rule", namespace, f"disabled:{item.get('candidate_ref') or item.get('name') or 'unknown'}"),
                                   "enabled": False,
                                   "rule_code": item.get("source_ref") or item.get("rule_code"),
                                   "source": dict(FORM_SOURCE)})
            continue
        key = str(item.get("rule_key") or "")
        name = str(item.get("name") or "").strip()
        operator = str(item.get("operator") or "")
        node = str(item.get("node") or "")
        node_type = rule_node_type_of(item)
        if not key or key in rule_keys:
            raise BusinessError("MANIFEST_DUPLICATE_RULE_KEY", "规则配置缺少唯一标识", details={"rule_key": key})
        if not name:
            raise BusinessError("MANIFEST_RULE_NAME_REQUIRED", "规则名称不能为空", details={"rule_key": key})
        if operator not in OPERATOR_NODE_CONSTRAINTS or node_type not in OPERATOR_NODE_CONSTRAINTS[operator]:
            raise BusinessError(
                "MANIFEST_OPERATOR_NODE_INVALID",
                "规则算子不能在当前节点执行",
                details={"rule": name, "operator": operator, "node": node, "node_type": node_type},
            )
        parameters = item.get("parameters") if isinstance(item.get("parameters"), dict) else {}
        inputs: list[dict[str, str]] = []
        for rule_input in item.get("inputs") or []:
            field_key = str(rule_input.get("field_key") or "")
            material_key = str(rule_input.get("material_key") or "")
            if field_key not in field_by_key:
                raise BusinessError(
                    "MANIFEST_RULE_INPUT_MISSING",
                    "规则输入字段不存在或已被删除",
                    details={"rule": name, "field_key": field_key},
                )
            if field_materials[field_key] != material_key or material_key not in material_by_key:
                raise BusinessError(
                    "MANIFEST_RULE_INPUT_MATERIAL_MISMATCH",
                    "规则输入材料与字段所属材料不一致",
                    details={"rule": name, "field_key": field_key, "material_key": material_key},
                )
            inputs.append({"field_key": field_key, "material_key": material_key})
        material_scope: list[str] = []
        for material_key in item.get("materials") or []:
            if material_key not in material_by_key:
                raise BusinessError(
                    "MANIFEST_RULE_MATERIAL_MISSING",
                    "规则材料范围不存在或已被删除",
                    details={"rule": name, "material_key": material_key},
                )
            if material_key not in material_scope:
                material_scope.append(material_key)
        output_keys: list[str] = []
        for field_key in item.get("outputs") or []:
            if field_key not in field_by_key:
                raise BusinessError(
                    "MANIFEST_RULE_OUTPUT_MISSING",
                    "规则输出字段不存在或已被删除",
                    details={"rule": name, "field_key": field_key},
                )
            if field_key not in output_keys:
                output_keys.append(field_key)
        if operator == "LLM_EXTRACT":
            if not output_keys:
                raise BusinessError("MANIFEST_EXTRACTION_OUTPUT_REQUIRED", "字段抽取规则必须声明输出字段", details={"rule": name})
            if any(field_materials[field_key] not in material_scope for field_key in output_keys):
                raise BusinessError("MANIFEST_EXTRACTION_MATERIAL_INVALID", "字段抽取规则的输出字段不属于规则材料范围", details={"rule": name})
        # 材料范围原样保留；缺少执行依据由该条规则在运行时明确说明，
        # 不以单条材料规则缺项阻断整个场景发布。
        source_node: str | None = None
        source_rule_key: str | None = None
        source_rule_keys: list[str] = []
        source_rule_codes: list[str] = []
        if operator == "RISK_TRIGGER":
            # 风险可以关联多条来源检查（任一 / 全部）。表单一次只提交一个 source_rule_key，
            # 但经导入 / 修订进入草稿的节点版风险带的是 source_rule_keys 列表；
            # 只读第一个会把「全部关联检查不合格」静默放宽成只监视一条来源。
            declared_keys: list[str] = []
            # 不要用 key 作循环变量：上面已经用 key 承载了本条规则的 rule_key，
            # 复用同一个名字会在下面 rule_nodes[key] = node 时把来源规则的键写成本规则。
            for declared in item.get("source_rule_keys") or []:
                text = str(declared)
                if text and text not in declared_keys:
                    declared_keys.append(text)
            source_ref = item.get("source_rule_key")
            if source_ref:
                text = str(source_ref)
                if text not in declared_keys:
                    declared_keys.insert(0, text)
            source_rule_codes = list(dict.fromkeys(
                str(code) for code in (item.get("source_rule_codes") or []) if str(code)
            ))
            # 来源节点是可选声明，由实际引用的来源规则或计算结果所属节点派生
            # （`rule["source_nodes"]`）。声明了才校验它，没声明不等于缺配置。
            source_node = (
                _risk_source_node(name, item.get("source_node")) if item.get("source_node") else None
            )
            source_rule_key = declared_keys[0] if declared_keys else None
            source_rule_keys = declared_keys
        # 输入数量、类型和单位是否足以执行业务规则，由执行器处理。
        applicability = applicability_view({**item, "applicability": item.get("applicability")})
        if applicability["kind"] not in APPLICABILITY_LABELS:
            raise BusinessError(
                "MANIFEST_APPLICABILITY_INVALID",
                "规则的适用条件不受支持",
                details={"rule": name, "kind": applicability["kind"]},
            )
        rule_keys.add(key)
        rule_nodes[key] = node
        rule_node_types[key] = node_type
        compiled_rules.append(
            {
                "rule_key": key,
                # 业务规则编号：页面表单维护的「规则编号」列（`source_ref`）。
                # 它是规则的业务身份，导出时写回 Excel 的「规则编号」列。
                # 不填这一列，导出的文件在该列上是空的，而它是行必填列，
                # 重新导入会直接报 RULE_CELL_EMPTY。页面新建且未填编号的规则保持 None，
                # 不用派生值冒充业务编号。
                "rule_code": item.get("source_ref") or None,
                "name": name,
                "node": node,
                "node_type": node_type,
                "operator": operator,
                "enabled": bool(item.get("enabled", True)),
                "inputs": inputs,
                "outputs": output_keys,
                "materials": material_scope,
                "source_rule_key": source_rule_key,
                "source_rule_keys": list(source_rule_keys),
                "source_rule_codes": list(source_rule_codes),
                "source_node": source_node,
                # 页面表单路径的来源节点由用户显式选择，单独一条来源规则：
                # 实际节点集合与声明一致，由下面的统一校验核对。
                "source_nodes": [],
                "parameters": parameters,
                # 适用条件随规则进入已发布配置：下面的统一校验会补齐 material_keys，
                # 但不能在这里丢掉用户选择的 kind（否则会静默变成“始终适用”）。
                "applicability": {"kind": applicability["kind"]},
                "requirement": str(item.get("requirement") or ""),
                # 业务说明：风险规则的「风险说明」列。它是业务配置事实，导出时必须写回，
                # 否则页面保存一次就把这一列丢成空。
                "description": str(item.get("description") or ""),
                # 业务配置事实随规则进入已发布配置，与导入编译保持一致：
                # 检查等级、合格要求原文、风险等级原文、风险成立条件与业务配置状态。
                # 缺了它们，页面保存过的规则在导出、报告与风险结果里就只剩执行参数，
                # 「这条检查是什么等级」「风险的成立条件是什么」都无从说明。
                "config_status": item.get("config_status"),
                "check_operator": item.get("check_operator"),
                "check_level": item.get("check_level"),
                "risk_level_value": item.get("risk_level_value"),
                # 风险的成立条件（任一 / 全部关联检查不合格）：它是业务配置事实，
                # 导出时要写回「成立条件」列。丢了它导出的该列为空，而它是必填枚举列，
                # 重新导入直接报 RULE_ENUM_VALUE_INVALID。
                "risk_condition": item.get("risk_condition"),
                "quality_supported": item.get("quality_supported"),
                "source_ref": item.get("source_ref"),
                "source": dict(FORM_SOURCE),
                **({"origin": item["origin"]} if item.get("origin") else {}),
            }
        )

    # 风险规则的来源规则必须真实存在、已启用，并位于来源节点允许的审核节点上。
    # 来源可能有多条（任一 / 全部）：全部来源都要校验，只查第一条会让其余来源
    # 指向已删除或已停用的规则却仍然发布出去。
    # 节点先后位置取自用户配置的节点实例顺序。
    node_positions = {
        str(item["node_code"]): int(item.get("sequence_no") or 0) for item in node_instances
    }
    rules_by_key = {item["rule_key"]: item for item in compiled_rules}
    for rule in compiled_rules:
        if rule["operator"] != "RISK_TRIGGER":
            continue
        declared_keys = list(rule.get("source_rule_keys") or [])
        if not declared_keys and rule.get("source_rule_key"):
            declared_keys = [rule["source_rule_key"]]
        resolved_keys: list[str] = []
        actual_nodes: set[str] = set()
        for source_rule_key in declared_keys:
            if source_rule_key not in rule_nodes:
                raise BusinessError(
                    "MANIFEST_RISK_SOURCE_MISSING",
                    "风险规则的来源规则不存在或已被删除",
                    details={"rule": rule["name"], "source_rule_key": source_rule_key},
                )
            source_rule = rules_by_key[source_rule_key]
            if not source_rule["enabled"]:
                raise BusinessError(
                    "MANIFEST_RISK_SOURCE_MISSING",
                    "风险规则的来源规则已被停用，不能作为风险来源",
                    details={"rule": rule["name"], "source_rule_key": source_rule_key},
                )
            if source_rule["operator"] == "RISK_TRIGGER":
                raise BusinessError(
                    "MANIFEST_RISK_SOURCE_INVALID",
                    "风险规则不能引用自身或另一条风险规则",
                    details={"rule": rule["name"], "source_rule_key": source_rule_key},
                )
            actual_node = rule_nodes[source_rule_key]
            _rule_position = _node_position_of(node_positions, rule["node"], rule_name=rule["name"])
            _source_position = _node_position_of(
                node_positions, actual_node, rule_name=rule["name"], role="来源规则"
            )
            if _rule_position <= _source_position:
                raise BusinessError("MANIFEST_NODE_ORDER_INVALID", "风险规则必须位于来源规则之后", details={"rule": rule["name"]})
            # 来源节点声明的是能力类型（内部校验 / 交叉校验 / 材料质量），
            # 而 `node` 是实例码：比对必须用 `node_type`。
            # 未声明来源节点时不校验这一项：来源节点由来源规则自身派生
            # （`actual_nodes`），没有声明就没有可违反的限制。
            allowed_source_types = SOURCE_NODE_RULE_NODES.get(rule["source_node"] or "")
            if allowed_source_types is not None and rule_node_types[source_rule_key] not in allowed_source_types:
                raise BusinessError(
                    "MANIFEST_RISK_SOURCE_NODE_MISMATCH",
                    "风险疑点的来源审核规则不在指定的来源节点中",
                    details={
                        "rule": rule["name"],
                        "source_node": rule["source_node"],
                        "source_rule_key": source_rule_key,
                        "actual_node": actual_node,
                        "expected_nodes": sorted(SOURCE_NODE_RULE_NODES.get(rule["source_node"] or "", frozenset())),
                    },
                )
            actual_nodes.add(rule_node_types[source_rule_key])
            if source_rule_key not in resolved_keys:
                resolved_keys.append(source_rule_key)
        rule["source_rule_key"] = resolved_keys[0] if resolved_keys else None
        rule["source_rule_keys"] = resolved_keys
        rule["source_nodes"] = sorted(actual_nodes)

    # 取证方式与判定方式在编译期固化：运行时按冻结计划分派，不让大模型临时决定路由。
    # 主体/签章一致性检查的识别依据是字段的「产物来源=印章识别」声明（不是字段中文名），
    # 标记必须先算再算 route——execution_route 内部读的正是这个标记，
    # 顺序颠倒会让主体规则拿不到 SEAL_SUBJECT_RETRIEVAL 取证方式，
    # 索引准备、检索分支与执行分支就会互相不一致。
    field_artifact_source = {item["field_key"]: item.get("artifact_source") for item in compiled_fields}
    for rule in compiled_rules:
        if rule["operator"] in {"FIELD_EQUAL", "FIELD_NOT_EQUAL"}:
            rule["subject_check"] = any(
                (field_artifact_source.get(item["field_key"]) or "") == "SEAL" for item in rule["inputs"]
            )
        rule["route"] = execution_route(rule)

    # 适用条件“所引用材料均已上传”的判定材料：规则输入字段所属材料 + 声明的材料范围，
    # 去重后保持稳定顺序；没有可判定材料时显式报错，不默默改成“始终适用”。
    for rule in compiled_rules:
        applicability = applicability_view(rule)
        material_keys: list[str] = []
        for item in rule["inputs"]:
            if item["material_key"] not in material_keys:
                material_keys.append(item["material_key"])
        for material_key in rule["materials"]:
            if material_key not in material_keys:
                material_keys.append(material_key)
        if applicability["kind"] != "ALWAYS" and not material_keys:
            raise BusinessError(
                "MANIFEST_APPLICABILITY_MATERIAL_MISSING",
                "规则的适用条件为“所引用材料均已上传”，但该规则没有引用任何材料，无法判定何时适用",
                details={"rule": rule["name"]},
            )
        rule["applicability"] = {"kind": applicability["kind"], "material_keys": material_keys}

    # 抽取规则归属取自字段或独立抽取配置；不读取来源工作表的历史节点绑定。
    explicit_extraction_outputs: set[str] = set()
    extraction_output_counts: dict[str, int] = {}
    for rule in compiled_rules:
        if rule["operator"] != "LLM_EXTRACT":
            continue
        for field_key in rule["outputs"]:
            explicit_extraction_outputs.add(field_key)
            if rule.get("enabled", True):
                extraction_output_counts[field_key] = extraction_output_counts.get(field_key, 0) + 1
    duplicate_outputs = sorted(key for key, count in extraction_output_counts.items() if count > 1)
    if duplicate_outputs:
        raise BusinessError(
            "MANIFEST_EXTRACTION_OUTPUT_DUPLICATE",
            "同一字段不能由多个字段抽取规则同时产出",
            details={"field_keys": duplicate_outputs},
        )
    for field in compiled_fields:
        if field["field_key"] in explicit_extraction_outputs:
            continue
        if field.get("node_code"):
            extraction_node_code = field["node_code"]
            owner = next((node for node in node_instances if node["node_code"] == extraction_node_code), None)
            if owner is None or not owner["enabled"] or owner["node_type"] != NODE_TYPE_FIELD_EXTRACTION:
                raise BusinessError("MANIFEST_FIELD_NODE_INVALID", "字段引用的抽取节点不存在、停用或类型不匹配", details={"field_key": field["field_key"], "node_code": extraction_node_code})
        else:
            extraction_node_code = instance_code_of_type(
                node_instances, NODE_TYPE_FIELD_EXTRACTION, purpose="字段抽取"
            )
        instruction = str(field["instruction"] or "").strip()
        extraction_enabled = (system_rule_enabled or {}).get(f"SYS-EXTRACT-{field['field_key']}", True)
        if extraction_enabled and not instruction:
            raise BusinessError(
                "MANIFEST_FIELD_INSTRUCTION_REQUIRED",
                f"字段“{field['name']}”缺少抽取要求，不能发布",
                details={"field_name": field["name"]},
            )
        extraction_parameters = {"instruction": instruction}
        if extraction_enabled:
            compiled_rules.append(
                {
                    "rule_key": stable_key("rule", namespace, f"extract:{field['field_key']}"),
                    # 系统派生规则的编号显式带 SYS- 前缀：它不是业务规则编号，
                    # 不能让页面或导出把它读成一条业务维护的规则。派生依据（字段身份）
                    # 拼在后面，同一字段重编译得到同一个编号。
                    "rule_code": f"SYS-EXTRACT-{field['field_key']}",
                    "legacy_rule_key": stable_key("rule", namespace, f"extract:{field['field_key']}"),
                    "name": f"抽取{field['name']}",
                    "node": extraction_node_code,
                    "node_type": NODE_TYPE_FIELD_EXTRACTION,
                    "operator": "LLM_EXTRACT",
                    "enabled": extraction_enabled,
                    "inputs": [],
                    "outputs": [field["field_key"]],
                    "materials": [field["material_key"]],
                    "source_rule_key": None,
                    "source_node": None,
                    "source_nodes": [],
                    "parameters": extraction_parameters,
                    "applicability": {"kind": "ALWAYS", "material_keys": []},
                    "requirement": instruction,
                    "source_ref": None,
                    "source": dict(FORM_SOURCE),
                }
            )

    report_template: list[dict[str, Any]] = []
    active_sections = [section for section in sections if section.get("config_status") != CONFIG_STATUS_DISABLED]
    explicit_report_groups = any(section.get("report_code") for section in active_sections)
    for section in active_sections:
        section_key = section.get("section_key")
        if not isinstance(section_key, str) or not section_key.strip():
            raise BusinessError(
                "MANIFEST_SECTION_KEY_REQUIRED",
                "报告章节缺少稳定键",
                details={"section_name": section.get("section_name")},
            )
        title = str(section.get("section_name") or "").strip()
        if not title:
            raise BusinessError("MANIFEST_SECTION_NAME_REQUIRED", "报告章节名称不能为空")
        field_keys: list[str] = []
        for field_key in section.get("field_keys") or []:
            if field_key not in field_by_key:
                raise BusinessError(
                    "MANIFEST_REPORT_FIELD_MISSING",
                    "报告章节引用的字段不存在或已被删除",
                    details={"section": title, "field_key": field_key},
                )
            field_keys.append(field_key)
        entry: dict[str, Any] = {
            "section_key": section_key,
            "section_name": title,
            "content_instruction": str(section.get("content_instruction") or ""),
            "field_keys": field_keys,
            "source": dict(FORM_SOURCE),
            **({"origin": section["origin"]} if section.get("origin") else {}),
        }
        # 节点版的报告分组属性：报告编号、报告名称、材料范围、输出格式、
        # 章节顺序、内容类型、结果排序与配置状态。有报告编号时按分组渲染，
        # 没有报告编号时也按章节要求生成正文。字段名与导入编译保持一致。
        if explicit_report_groups:
            entry.update(
                {
                    "report_code": section.get("report_code"),
                    "report_name": section.get("report_name"),
                    "material_keys": list(section.get("material_keys") or []),
                    "formats": list(section.get("formats") or []),
                    "section_order": section.get("section_order"),
                    "content_type": section.get("content_type"),
                    "result_sort": section.get("result_sort"),
                    "risk_level_order": list(section.get("risk_level_order") or []),
                    "config_status": section.get("config_status"),
                }
            )
        else:
            entry["material_keys"] = list(section.get("material_keys") or [])
            entry["formats"] = list(section.get("formats") or [])
            entry["result_sort"] = section.get("result_sort")
            entry["risk_level_order"] = list(section.get("risk_level_order") or [])
            entry["config_status"] = section.get("config_status")
            entry["section_order"] = section.get("section_order")
            entry["content_type"] = section.get("content_type")
        report_template.append(entry)

    report_node_code = instance_code_of_type(node_instances, NODE_TYPE_REPORT_OUTPUT, purpose="报告生成")
    if not any(item["node"] == report_node_code and item["operator"] == "REPORT_RENDER" for item in compiled_rules):
        compiled_rules.append(
            {
                "rule_key": stable_key("rule", namespace, "report:render"),
                # 报告生成是系统动作，编号显式带 SYS- 前缀。
                "rule_code": "SYS-REPORT-RENDER",
                "legacy_rule_key": stable_key("rule", namespace, "report:render"),
                "name": "生成审核报告",
                "node": report_node_code,
                "node_type": NODE_TYPE_REPORT_OUTPUT,
                "operator": "REPORT_RENDER",
                "enabled": (system_rule_enabled or {}).get("SYS-REPORT-RENDER", True),
                "inputs": [],
                "outputs": [],
                "materials": [],
                "source_rule_key": None,
                "source_node": None,
                "source_nodes": [],
                "parameters": {},
                "applicability": {"kind": "ALWAYS", "material_keys": []},
                "requirement": "",
                "source_ref": None,
                "source": dict(FORM_SOURCE),
            }
        )

    compiled_rules.extend(
        _material_check_rules(compiled_materials, compiled_rules, node_instances, namespace, system_rule_enabled)
    )
    _ensure_unique_rules(compiled_rules)
    ensure_unique_rule_codes(compiled_rules)
    _ensure_manifest_node_coverage(
        compiled_rules,
        node_instances,
        require_report_rule=(system_rule_enabled or {}).get("SYS-REPORT-RENDER", True),
    )

    candidates = refresh_calculation_references(list(calculation_candidates or []))
    if candidates:
        compiled_calculations = compile_calculations(
            candidates,
            field_code_index=field_code_index,
            node_instances=node_instances,
        )
        stored_inputs: list[dict[str, Any]] | None = candidates
    elif frozen_calculations:
        compiled_calculations = recompile_frozen_calculations(
            frozen_calculations,
            field_code_index=field_code_index,
            node_instances=node_instances,
        )
        stored_inputs = None
    else:
        compiled_calculations = []
        stored_inputs = []

    normalized_scenario_schema = scenario_schema.normalize_schema(scenario_schema_config)
    node_plan = build_node_plan(compiled_rules, compiled_calculations or None, node_instances, compiled_fields, compiled_materials)
    manifest = {
        "manifest_version": "1.0",
        "scenario_schema": normalized_scenario_schema,
        "execution_contract_version": "1",
        # 节点能力目录的版本身份，不限制规则库工作表结构。
        "profile_version": profile_version,
        "source_kind": "FORM",
        "scene_code": scene_code,
        "scene_name": scene_name,
        "materials": sorted(compiled_materials, key=lambda item: item["material_key"]),
        "fields": sorted(compiled_fields, key=lambda item: item["field_key"]),
        "rules": sorted([*compiled_rules, *disabled_rules], key=lambda item: item["rule_key"]),
        # 节点来自用户配置：实例码、能力类型、名称、顺序、依赖与配置全部原样固化，
        # 不由系统按规则内容生成固定节点表。
        "nodes": attach_execution_contracts(node_plan, compiled_rules, compiled_calculations),
        "calculations": sorted(compiled_calculations, key=lambda item: item["rule_code"]),
        # 有候选时随版本留下，修订时重新编译。只有冻结产物时不写空候选，
        # 否则加载草稿会把已编译的计算规则当成「没有计算配置」丢掉。
        **({"calculation_inputs": stored_inputs} if stored_inputs is not None else {}),
        # 来源表清单：动态导入保留真实表名和身份；历史模板仍保留角色与节点绑定。
        "selected_sheets": [
            {"sheet_role": str(item["sheet_role"]), "node_code": item.get("node_code"),
             **{key: item[key] for key in ("sheet_key", "sheet_name") if key in item}}
            for item in selected_sheets
        ],
        "disabled_source_rows": list(disabled_source_rows or []),
        "report_template": report_template,
        # 报告分组：报告编号、报告名称、材料范围、输出格式、结果排序与章节顺序
        # 随章节一起保留；页面表单配置若来自导入/修订，同样按编号分组，
        # 不会因为经过一次页面保存就退回成旧的附加展示章节。
        "report_groups": _build_report_groups(report_template, explicit_report_groups),
        # 未配置的检查类别：类型级汇总与实例级清单分开记录。页面按类型判断
        # 「这类检查有没有配」，按实例码判断「当前这个节点是不是未配置的类别」。
        "unconfigured_check_node_types": unconfigured_check_node_types(compiled_rules),
        "unconfigured_check_node_codes": unconfigured_check_node_codes(compiled_rules, node_instances),
        "capability_gaps": capability_gaps(compiled_rules),
        # 本次发布逐条确认过的能力缺口：属于版本审计事实，随版本冻结并参与指纹。
        "acknowledged_capability_gaps": sorted(set(acknowledged_capability_gaps or [])),
        # 能力覆盖结果随版本一起冻结。
        "capability_coverage": capabilities.coverage_of(compiled_rules),
        "capability_version": capabilities.CAPABILITY_VERSION,
        # 工具目录与工具策略随版本冻结，供运行期核对发布时的执行契约。
        "tool_catalog_version": tool_catalog.TOOL_CATALOG_VERSION,
        "tool_catalog_hash": tool_catalog.catalog_fingerprint(),
        "tool_policy_version": tool_catalog.TOOL_POLICY_VERSION,
        "allowed_tools": tool_catalog.allowed_tools_of(compiled_rules),
        "compiled_at": compiled_at.isoformat(),
        "source_workbook_id": None,
    }
    if import_proposals is not None:
        manifest["import_proposals"] = import_proposals
    canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    manifest["manifest_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return manifest
