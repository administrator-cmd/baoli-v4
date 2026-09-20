import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import ValidationError

from .proposals import ProposalBundle
from . import capabilities, file_quality
from ..errors import BusinessError
from .identity import stable_key


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

# 算子能挂在哪个节点：从能力目录（domain.capabilities）派生，不在这里另维护一份名单。
# 两份名单漂移过一次（后端 12 个算子、页面下拉 7 个），因此这里只做引用。
# 返回类型是 frozenset：调用方只做成员判断，不原地修改。
OPERATOR_NODE_CONSTRAINTS: dict[str, frozenset[str]] = capabilities.NODE_CONSTRAINTS

DOCUMENT_ARTIFACT_TYPES = {"MATERIAL_CLASSIFY", "DOCUMENT_PARSE", "OCR", "SEAL"}
DATE_COMPARE_OPERATORS = {"EQ", "GTE", "LTE", "GT", "LT"}
# 日期差值只以天为单位：填写规范规定「日期差值以天为单位」，不接受月、年这类不等长单位。
DATE_THRESHOLD_UNIT = "天"
# 业务配置状态：停用行不参与执行。它是业务口径，不是系统能力声明或运行结果。
CONFIG_STATUS_DISABLED = "DISABLED"
# 待确认：非停用但业务口径尚未确认。它能保存为草稿，但不能进入已发布配置。
CONFIG_STATUS_PENDING = "PENDING"
DECIMAL_COMPARE_OPERATORS = {"EQ", "GTE", "LTE", "ABS_DIFF_LTE"}
# 加总比较只支持数值的等于/不大于/不小于：加法不能用于日期、文本或条款。
DECIMAL_SUM_COMPARE_OPERATORS = {"EQ", "GTE", "LTE"}
# 风险等级。旧格式（v1/v2）沿用 PM 原「高/中/低/待确认」，节点版（v3）用
# 「最高/一级/二级/三级」。两套标签都是已确认的业务取值，必须同时保留：
# 只接受旧集合会直接拒绝节点版风险行，「一级」这类真实等级根本发布不了。
LEGACY_RISK_LEVELS = ("高", "中", "低", "待确认")
NODE_RISK_LEVELS = ("最高", "一级", "二级", "三级")
RISK_LEVELS = set(LEGACY_RISK_LEVELS) | set(NODE_RISK_LEVELS)
# 等级排序口径（由高到低）。仅用于同等级内排序展示，不做等级之间的业务映射：
# PM 原「高/低」与四级标签的对应关系仍未确认，系统不代填。
LEGACY_RISK_LEVEL_ORDER = {"高": 0, "中": 1, "低": 2, "待确认": 3}
NODE_RISK_LEVEL_ORDER = {"最高": 0, "一级": 1, "二级": 2, "三级": 3}


def risk_level_rank(value: Any) -> int | None:
    """风险等级在同等级体系内的排序位次；未识别时返回 None（不当作最低等级）。

    两套标签各自排序：旧「高/中/低/待确认」与节点版「最高/一级/二级/三级」。
    未知取值不给默认位次，排序时排到已识别等级之后并保持原有顺序。
    """
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if normalized in NODE_RISK_LEVEL_ORDER:
        return NODE_RISK_LEVEL_ORDER[normalized]
    return LEGACY_RISK_LEVEL_ORDER.get(normalized)
# 风险成立条件：任一关联检查不合格 / 全部关联检查不合格。它由检查行的「关联风险」
# 反向确定，不允许在风险表重复维护第二份来源列表。
RISK_CONDITIONS = {"ANY_ABNORMAL", "ALL_ABNORMAL"}
RISK_CONDITION_LABELS = {
    "ANY_ABNORMAL": "任一关联检查不合格",
    "ALL_ABNORMAL": "全部关联检查不合格",
}
# 风险疑点的来源节点：业务必须显式声明这条风险监视的是哪一类审核规则，
# 取值与业务 Sheet 一一对应，避免风险来源规则跨节点引用。
SOURCE_NODE_LABELS = {
    "MATERIAL_QUALITY": "材料质量",
    "INTERNAL_VALIDATE": "文件内部校验",
    "CROSS_VALIDATE": "文件交叉校验",
}
# 来源节点 → 审核节点代码集合：来源规则的实际节点必须落在这个集合里。
# 「材料质量」对应 02_材料质量 这一份业务配置，而它的两种检查项目分别执行在两个节点上：
# 「材料是否齐全」= MATERIAL_EXISTS 在材料检查节点，「文件质量检查」= FILE_QUALITY_CHECK 在文件处理节点。
# 因此这里必须是集合，否则文件质量类风险（例如缺页、缺章）无法声明来源。
SOURCE_NODE_RULE_NODES: dict[str, frozenset[str]] = {
    "MATERIAL_QUALITY": frozenset({"MATERIAL_CHECK", "DOCUMENT_PREPARE"}),
    "INTERNAL_VALIDATE": frozenset({"INTERNAL_VALIDATE"}),
    "CROSS_VALIDATE": frozenset({"CROSS_VALIDATE"}),
}
# 结构上必须存在的节点：报告输出决定任务能否产出结果。
# 其余节点由模板内容决定：单合同场景可以没有交叉校验，也可以不配风险评级，
# 未配置的节点在执行时显式记为 SKIPPED，并说明"本模板未配置此类检查"，绝不写成通过。
REQUIRED_RULE_NODES = {"REPORT_OUTPUT"}
# 允许为空、需要显式跳过并说明原因的业务校验节点。
OPTIONAL_CHECK_NODES = ("INTERNAL_VALIDATE", "CROSS_VALIDATE", "RISK_RATING")

# 算子与比较方式的业务中文口径，供页面展示"实际比较对象和比较方式"。
# 从能力目录派生：中文标签只维护一份，改文案不会让页面与后端各说一套。
OPERATOR_LABELS = capabilities.OPERATOR_LABELS

DATE_COMPARISON_LABELS = {"EQ": "同一天", "GTE": "不早于", "LTE": "不晚于", "GT": "晚于", "LT": "早于"}
DECIMAL_COMPARISON_LABELS = {"EQ": "等于", "GTE": "不小于", "LTE": "不大于", "ABS_DIFF_LTE": "绝对差不超过"}
DECIMAL_SUM_COMPARISON_LABELS = {"EQ": "等于", "GTE": "不小于", "LTE": "不大于"}
APPLICABILITY_LABELS = {"ALWAYS": "始终适用", "MATERIALS_UPLOADED": "所引用材料均已上传"}
# 参数字典里不再使用的键：它们是旧格式的执行细节，节点版由检查类型与列事实确定。
# 编译时必须显式拒绝，不能静默丢弃（静默丢弃等于把业务没确认的差异藏起来）。
FORMULA_PARAMETER_KEYS = ("formula_type", "formula", "expression", "script")

# 当前不具备执行能力的规则语义。模型对同一条业务要求的改写方式不固定，
# 因此这些识别必须对所有算子生效，不能只在比较算子或某一种措辞上生效：
# 把「所有发票合计」写成 LLM_VALIDATE 同样是不支持的汇总取数，不能在运行时才发现。
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


# 否定/限制语境：这些词出现在关键词所在的**同一小句**内时，说的是
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


def detect_unsupported_capability(*texts: Any) -> tuple[str, str, str] | None:
    """对任意算子统一识别当前不具备执行能力的规则语义。

    返回 (capability_code, matched_text, description)；没有命中时返回 None。
    判定只依据用户真正写在规则里的文字，不猜测、不自动改写，命中即显式报错。

    否定语境不算命中：业务写「不能直接比较或加总」「无法汇总」是在说明约束或技术
    限制，不是在要求系统做汇总取数。判定只在关键词所在的**同一小句**内检查否定词，
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

    文字判定对算式类算子会误伤：`DECIMAL_SUM_COMPARE`（金额加总比较）**本身就是**
    合法的字段间加总，而它的审核要求原文里必然出现「总额 / 合计 / 加总 / 累计」这类词
    （「合同总额等于已付款加未付款」「结算总额与加数合计一致」）。这些词正是
    `SUM_ALL_FILES` 的关键词集合，一律判成「对多份材料做汇总取数」会让一条系统完全支持、
    且已正确编译出多个输入的规则根本存不下来。

    因此只对算式算子跳过**关键词与加总措辞重叠**的两个码。跳过的理由是措辞重叠，
    不是放宽能力：

      - `SUM_ALL_FILES`（总额/合计/汇总/求和/总计/累计/加总）与
        `CROSS_PERIOD_TOTAL`（跨期/分期/各期…合计/累计到）的关键词，逐一核对过
        都会被「合同总额等于已付款加未付款」「各项金额分期汇总后与总额一致」
        这类正常写法命中；
      - 这两个码真正要拦的**越界**（无界聚合、跨期自动累计），在算式算子上由结构约束
        拦下：`DECIMAL_SUM_COMPARE` 必须显式列出参与相加的字段（编译期校验至少两个），
        系统不会自行跨文件取数，也不接受「取第一份文件」代替。没有显式字段就编译不出来；
      - 用户真要求跨文件无界聚合时，措辞通常落在下面仍生效的两个码上：
        `MULTI_FILE_AGGREGATE`（「所有发票合计」）、`MULTI_CONTRACT_GROUP`（「全部合同」）。

    `CONSTANT_COMPARISON` 与 `EXTERNAL_LOOKUP` 的关键词与加总措辞无关，照常生效。
    非算式算子走完整判定，行为不变。
    """
    capability = capabilities.capability_of(operator)
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


# 算式算子上因**关键词与加总措辞重叠**而跳过的识别码。
# 只含这两个：它们的关键词（总额/合计/汇总/求和/总计/累计、跨期/分期/各期/累计到）
# 就是加总比较本身的业务措辞。MULTI_FILE_AGGREGATE / MULTI_CONTRACT_GROUP /
# CONSTANT_COMPARISON / EXTERNAL_LOOKUP 不在其中，照常在算式算子上生效。
_ADDEND_OPERATOR_EXEMPT_CODES = frozenset({"SUM_ALL_FILES", "CROSS_PERIOD_TOTAL"})


# 取证方式：按材料与字段直接读取结构化事实，或在材料文本中检索相关证据。
EVIDENCE_DIRECT = capabilities.EVIDENCE_DIRECT
EVIDENCE_RETRIEVAL = capabilities.EVIDENCE_RETRIEVAL
# 主体检索：只对印章识别产物中的**主体名称**建向量，用待核对主体查相似印章主体。
# 它不是全文检索：不检索合同正文，也不检索条款。
EVIDENCE_SEAL_SUBJECT = capabilities.EVIDENCE_SEAL_SUBJECT
# 判定方式：确定性程序 / 大模型语义判断 / 需要人工裁决。
JUDGE_PROGRAM = capabilities.JUDGE_PROGRAM
JUDGE_LLM = capabilities.JUDGE_LLM
JUDGE_HUMAN = capabilities.JUDGE_HUMAN

# 取证与判定的固定路由：从能力目录（domain.capabilities）派生，由规则算子确定性派生，
# 业务人员不填写 RAG/LLM 技术选项，大模型也不在运行时自行路由或扩大材料范围。
ROUTE_BY_OPERATOR: dict[str, tuple[str, str]] = capabilities.ROUTE_BY_OPERATOR

# 主体/签章一致性一类需要核对印鉴与合同主体一致性的规则：即使证据由程序读取，
# 识别冲突或归属不明时也必须交人工，不能仅因原始字符串不同就宣告已证实违规。
HUMAN_REVIEW_ON_CONFLICT = capabilities.HUMAN_REVIEW_ON_CONFLICT


def execution_route(rule: dict[str, Any]) -> dict[str, Any]:
    """规则的取证方式与判定方式；无法归入已支持流程时明确列为未支持。

    路由由能力目录（domain.capabilities）派生：算子存在、但当前 `implemented=false` 时
    同样记为 `supported=false` 并带上真实原因。这样「配置合法但能力未实现」不会在预览里
    显示成一条可执行规则——此前只有文件检查与大小写金额两处硬编码做了这件事。
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
    if rule.get("subject_check") is True and operator in {"FIELD_EQUAL", "FIELD_NOT_EQUAL"}:
        # 主体/签章一致性必须走**主体检索**：待核对主体与印章识别主体比对本就需要印章产物，
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
        # 算子本身已实现，但判定能力取决于**检查子项**：子项未实现时本条规则不可判定。
        # 子项的实现状态同样来自能力目录，不再由 quality_supported 单独承载一份。
        check_item = (rule.get("parameters") or {}).get("check_item")
        payload["capability_id"] = check_item or capability.capability_id
        payload["supported"] = capabilities.is_implemented(check_item)
        if not payload["supported"]:
            sub = capabilities.capability_of(check_item)
            payload["message"] = (
                sub.unimplemented_reason if sub else "该文件检查的检查项目不是系统已知的能力，不能作为可执行规则发布"
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


def _assert_no_legacy_formula_parameters(rule_name: str, operator: str, parameters: dict[str, Any]) -> None:
    """显式拒绝旧格式的公式参数，不静默丢弃。

    静默丢弃等于把「业务写了一段公式、系统实际没执行它」这件事藏起来；
    这里必须报错，让用户看到具体是哪条规则的哪个参数不被支持。
    """
    if not isinstance(parameters, dict):
        return
    present = [key for key in FORMULA_PARAMETER_KEYS if key in parameters]
    if present:
        _parameter_error(
            rule_name,
            operator,
            "包含当前不支持的公式类参数，系统不执行任何 Excel 公式或脚本",
            details={"parameter": present[0]},
        )


def _risk_condition_of(rule: dict[str, Any]) -> str:
    """风险成立条件必须是契约枚举之一。

    缺失与取值非法都显式报错，绝不回落到默认的「任一」：猜一个成立条件会让
    “全部关联检查不合格”被静默放宽成“任一”。
    """
    value = rule.get("risk_condition")
    if not isinstance(value, str) or value not in RISK_CONDITIONS:
        raise BusinessError(
            "MANIFEST_RISK_CONDITION_REQUIRED",
            "风险规则必须声明成立条件（任一关联检查不合格 / 全部关联检查不合格）",
            details={"rule": rule["name"], "risk_condition": value, "allowed": sorted(RISK_CONDITIONS)},
        )
    return value


# 报告章节的数据来源：内容类型决定章节取哪些结果，章节标题不参与推断。
REPORT_CONTENT_TYPES = {"FIELD_DETAIL", "AUDIT_RESULT", "FIELD_AND_RESULT", "RISK_LIST", "CONCLUSION"}
# 报告输出格式中**当前真正实现了文件生成**的只有 Markdown。
# 节点版模板在 07 声明 PDF / Word / Excel，那是业务对报告形态的期望，
# 不等于系统已经能产出这三种文件；声明与实现必须分开记录，不能混为一谈。
IMPLEMENTED_REPORT_FORMATS = frozenset({"Markdown"})
REPORT_FIELD_CONTENT_TYPES = {"FIELD_DETAIL", "FIELD_AND_RESULT"}
REPORT_RESULT_CONTENT_TYPES = {"AUDIT_RESULT", "FIELD_AND_RESULT", "CONCLUSION"}
REPORT_RESULT_SORTS = {"RISK_LEVEL_DESC", "MATERIAL_ORDER", "MATERIAL_ORDER_RISK_LEVEL_DESC"}


def _build_report_groups(
    sections: list[dict[str, Any]],
    explicit: bool,
) -> dict[str, Any]:
    """把报告章节按报告编号分组，并保留每份报告的属性与章节顺序。

    节点版里同一报告编号的报告名称、材料范围、输出格式、结果排序必须完全一致；
    这里做发布前最后一道核对，冲突时明确指出是哪一项、在哪两行之间不一致，
    不取第一行、不用空白继承，也不忽略冲突。旧格式没有报告编号，
    沿用原有的“附加展示章节”语义（一份报告，章节按声明顺序）。
    """
    if not explicit:
        ordered = sorted(sections, key=lambda item: item.get("section_order") or 0)
        return {
            "reports": [
                {
                    "report_code": None,
                    "report_name": "审核报告（附加展示章节）",
                    "material_keys": [],
                    "formats": ["Markdown"],
                    "result_sort": None,
                    "sections": [
                        {
                            "section_name": section["section_name"],
                            "content_type": section.get("content_type") or "FIELD_DETAIL",
                            "section_order": index,
                            "field_keys": list(section.get("field_keys", [])),
                            "content_instruction": section.get("content_instruction", ""),
                        }
                        for index, section in enumerate(ordered, start=1)
                    ],
                }
            ],
            "source_kind": "LEGACY_SECTIONS",
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
        if content_type not in REPORT_CONTENT_TYPES:
            raise BusinessError(
                "MANIFEST_REPORT_CONTENT_TYPE_INVALID",
                f"报告章节“{section.get('section_name')}”的内容类型不受支持",
                details={"report_code": code, "content_type": content_type, "allowed": sorted(REPORT_CONTENT_TYPES)},
            )
        if content_type in REPORT_FIELD_CONTENT_TYPES and not section.get("field_keys"):
            raise BusinessError(
                "MANIFEST_REPORT_FIELD_REQUIRED",
                f"报告章节“{section.get('section_name')}”的内容类型需要展示字段，但没有任何绑定字段",
                details={"report_code": code},
            )
        result_sort = section.get("result_sort")
        if result_sort is not None and result_sort not in REPORT_RESULT_SORTS:
            raise BusinessError(
                "MANIFEST_REPORT_SORT_INVALID",
                f"报告“{code}”的结果排序不受支持",
                details={"report_code": code, "result_sort": result_sort, "allowed": sorted(REPORT_RESULT_SORTS)},
            )
        entry = groups.get(code)
        signature = {
            "report_name": section.get("report_name"),
            "material_keys": list(section.get("material_keys", [])),
            "formats": list(section.get("formats", [])),
            "result_sort": result_sort,
        }
        if entry is None:
            if not signature["formats"]:
                raise BusinessError(
                    "MANIFEST_REPORT_FORMAT_REQUIRED",
                    f"报告“{code}”没有声明输出格式",
                    details={"report_code": code},
                )
            if not signature["material_keys"]:
                raise BusinessError(
                    "MANIFEST_REPORT_SCOPE_REQUIRED",
                    f"报告“{code}”没有声明材料范围：空白不表示全部",
                    details={"report_code": code},
                )
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
    unsupported = detect_unsupported_capability(requirement, parameters.get("instruction"))
    if unsupported:
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
        # 风险疑点的来源节点：页面据此说明这条风险监视的是哪一类审核规则。
        # 非风险规则没有来源节点，两个键显式为 None。
        "source_node": rule.get("source_node"),
        "source_node_label": SOURCE_NODE_LABELS.get(rule.get("source_node")),
        "scope_material_names": [material_names.get(ref) for ref in rule.get("material_refs", [])],
        "output_field_names": [field_labels.get(ref) for ref in rule.get("output_field_refs", [])],
        # 方向、阈值、单位与支持状态必须显式可见，不能只靠 operator/parameters 原始编码。
        "direction_label": direction,
        "threshold": threshold,
        "support_status": support_status,
        "support_message": support_message,
        "input_units": [item.get("unit") for item in inputs],
        # 风险成立条件与等级原文：等级差异必须显式可见，不能用较低风险等级覆盖来源异常等级。
        "risk_condition": rule.get("risk_condition"),
        "risk_condition_label": RISK_CONDITION_LABELS.get(rule.get("risk_condition") or ""),
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


def unconfigured_check_nodes(rules: list[dict[str, Any]]) -> list[str]:
    """返回本模板未配置任何启用规则的业务校验节点。"""
    covered = {item["node"] for item in rules if item.get("enabled", True)}
    return [code for code in OPTIONAL_CHECK_NODES if code not in covered]


def capability_gaps(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """已配置但当前**无法执行**的检查。

    只有**声明了、但当前没有判定实现**的能力才构成能力缺口。缺口由能力目录
    （domain.capabilities）派生：一条规则引用的能力（算子，或文件检查的检查子项）
    implemented=false 时即为缺口，不再只有文件检查参与判定。

    文件格式、文件大小、文件内容三项已有真实处理器并按真实数据判定，不能列为缺口——
    把它们报成缺口会让发布预览与页面对业务谎称“这项做不了”，也会让执行端误以为
    该走不可判定分支。反过来，大小写金额一致性（DECIMAL_CN_EQUAL）此前不在任何缺口里，
    只能在运行时才发现做不了，现在同样在发布前暴露。

    这里如实记录缺口，让发布预览可见；运行时遇到该缺口明确报不可判定，
    既不默认通过，也不在发布时承诺一个永远执行不了的支持能力。
    """
    gaps: list[dict[str, Any]] = []
    for rule in rules:
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
                raise BusinessError(
                    "MANIFEST_QUALITY_ITEM_UNKNOWN",
                    "文件检查规则的「检查项目」不是系统已知的检查项目，无法确定它是否可执行",
                    details={"rule": rule.get("name"), "check_item": check_item},
                )
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
    raise BusinessError(
        "MANIFEST_RULE_PARAMETER_INVALID",
        "规则参数不符合算子契约",
        details={"rule": rule_name, "operator": operator, "reason": reason, **(details or {})},
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
    页面也可能提交 JSON 数字。这里**同时接受**十进制字符串与 int/float，
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
            optional={"requires_manual_review"},
        )
        _require_non_empty_string(rule_name, operator, parameters, "instruction")
        if "requires_manual_review" in parameters:
            _require_boolean(rule_name, operator, parameters, "requires_manual_review")
        return
    if operator == "RISK_TRIGGER":
        # 风险成立条件不属于算子参数：它由检查行的「关联风险」反向确定，
        # 不允许在风险表重复维护第二份来源列表。
        _validate_parameter_keys(rule_name, operator, parameters, required={"risk_level", "action"})
        risk_level = _require_non_empty_string(rule_name, operator, parameters, "risk_level")
        if risk_level not in RISK_LEVELS:
            _parameter_error(
                rule_name,
                operator,
                "风险等级不受支持",
                details={"parameter": "risk_level", "allowed": sorted(RISK_LEVELS)},
            )
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
    candidate_refs: dict[str, dict[str, str]] = {"MATERIAL": {}, "FIELD": {}, "RULE": {}}
    source_refs: dict[tuple[str, str, str], str] = {}
    for sheet in proposals["sheets"]:
        sheet_key = sheet["sheet_key"]
        for property_name, target_type in (("materials", "MATERIAL"), ("fields", "FIELD"), ("rules", "RULE")):
            # 同一行业务记录按材料展开出的多条候选共用同一个业务编号（source_id）：
            # 内部校验写了三种材料就是**一行**业务记录，展开只是执行需要。
            # 这些兄弟候选共享编号是配置事实，不是「两个对象撞了编号」。
            # 只有来自**不同**业务行的候选共用编号才是错误。
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
                if len(by_source.get(source_id, set())) > 1:
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

    既有 profile 里 `value_labels` 的书写方向并不统一，必须按列自适应：

      - v2 是「契约值 → 界面标签」，例如 {"SEED": "种子"}，allowed_values 是英文码；
      - v3 是「界面标签 → 契约值」，例如 {"一级风险": "一级"}，allowed_values 本身是中文。

    `allowed_values` 是这一列的**契约取值集合**，因此它同时是判定依据：
    只有落在 allowed_values 里的映射才被接受，其余映射（指向集合外的取值）不能
    覆盖掉合法取值本身，否则「一级」会被解析成并不存在的「一级风险」。
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


def resolve_label(column: dict[str, Any], text: str) -> str | None:
    """把单元格原文解析成契约值；无法解析时返回 None。"""
    return label_resolver(column).get(str(text).strip())


def _ensure_unique_materials(materials: list[dict[str, Any]]) -> None:
    seen: dict[tuple[str, str], dict[str, Any]] = {}
    for material in materials:
        # 识别类型可能尚未声明（None）。它不参与「是不是同一个材料位」的判断：
        # 重复与否由材料名称决定，未确认类型的两个同名材料位仍是重复。
        signature = (_normalize_text(material["name"]), _normalize_text(material.get("material_type") or ""))
        existing = seen.get(signature)
        if existing is not None:
            raise BusinessError(
                "MANIFEST_DUPLICATE_MATERIAL",
                "场景中存在重复材料位",
                details={
                    "name": material["name"],
                    "material_type": material.get("material_type"),
                    "candidate_refs": [existing["candidate_ref"], material["candidate_ref"]],
                },
            )
        seen[signature] = material


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
        "material_refs": sorted(rule.get("materials", rule.get("material_refs", []))),
        "parameters": rule.get("parameters", {}),
        # 适用条件不同的两条规则不是重复规则（例如一条始终适用、一条仅材料齐备时适用）。
        "applicability": rule.get("applicability"),
    }
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _ensure_unique_rules(rules: list[dict[str, Any]]) -> None:
    seen: dict[str, dict[str, Any]] = {}
    for rule in rules:
        signature = _rule_semantic_signature(rule)
        existing = seen.get(signature)
        if existing is not None:
            raise BusinessError(
                "MANIFEST_DUPLICATE_RULE",
                "场景中存在重复规则",
                details={
                    "rule_refs": [
                        existing.get("candidate_ref", existing.get("rule_key")),
                        rule.get("candidate_ref", rule.get("rule_key")),
                    ]
                },
            )
        seen[signature] = rule


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
    report_template: list[dict[str, Any]],
) -> None:
    """发布前的节点覆盖校验。

    固定事实报告是系统能力，必须能生成，因此 REPORT_OUTPUT 必须有 REPORT_RENDER；
    该规则由后端确定性补齐，不要求业务在 Excel 里填写。
    附加报告章节是可选配置：没有 07_报告模板 的最小模板同样可以发布，
    只要固定事实报告仍能生成，就不把「没有附加章节」当成阻断项。
    """
    covered_nodes = {item["node"] for item in rules if item.get("enabled", True)}
    missing_nodes = [code for code, _ in NODE_DEFINITIONS if code in REQUIRED_RULE_NODES and code not in covered_nodes]
    if missing_nodes:
        raise BusinessError(
            "MANIFEST_NODE_RULE_MISSING",
            "审核模板缺少必需节点的规则，不能发布",
            details={"node_codes": missing_nodes},
        )
    if not any(item["node"] == "REPORT_OUTPUT" and item["operator"] == "REPORT_RENDER" for item in rules):
        # 编译期已确定性补齐报告规则；走到这里说明规则被外部裁剪或被禁用。
        raise BusinessError("MANIFEST_REPORT_RULE_MISSING", "报告输出节点缺少报告生成规则")


def _ensure_task_node_coverage(selected_rules: list[dict[str, Any]]) -> None:
    selected_nodes = {item["node"] for item in selected_rules}
    missing_nodes = [code for code, _ in NODE_DEFINITIONS if code in REQUIRED_RULE_NODES and code not in selected_nodes]
    if missing_nodes:
        raise BusinessError(
            "TASK_NODE_RULE_MISSING",
            "所选规则缺少必需节点的规则，任务无法执行",
            details={"node_codes": missing_nodes},
        )
    if not any(item["node"] == "REPORT_OUTPUT" and item["operator"] == "REPORT_RENDER" for item in selected_rules):
        raise BusinessError("TASK_REPORT_RULE_MISSING", "所选规则缺少报告生成规则")


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
    "MATERIAL": {"name", "material_type", "required", "description", "config_status"},
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
        # 产物来源：主体核对需要它确定字段取自哪种处理产物，业务可通过裁决入口补齐。
        "artifact_source",
        "config_status",
    },
    # source_node 与 source_rule_ref 一样是「06_风险评定」工作表里的业务列，
    # 人工裁决可以显式指定来源节点；它仍会在编译期与来源规则的实际节点比对。
    # check_level 是节点版的检查等级原文：PM 原「高/低」没有已确认映射，
    # 只能由业务在裁决里明确成四级之一。
    "RULE": {
        "name",
        "node",
        "operator",
        "parameters",
        "source_rule_ref",
        "source_node",
        "material_refs",
        "check_level",
        # risk_condition 是「06_风险评定」的成立条件列：Excel 里为空时业务必须显式
        # 指定「任一关联检查不合格」还是「全部关联检查不合格」，系统不代选默认值。
        "risk_condition",
        "config_status",
    },
    # 07_报告输出 的章节：非停用章节标记为待确认时该报告仍是未完成配置，
    # 业务可以在这里确认该章节可以执行，或明确停用（停用章节不进入报告）。
    "REPORT_SECTION": {"config_status"},
    "SHEET": {"suggested_role"},
}


def _apply_resolutions(proposals: dict[str, Any], decisions: list[dict[str, Any]]) -> dict[str, Any]:
    updated = deepcopy(proposals)
    indexes: dict[str, dict[str, dict[str, Any]]] = {
        "MATERIAL": {},
        "FIELD": {},
        "RULE": {},
        "REPORT_SECTION": {},
        "SHEET": {},
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
            target[property_name] = value
    try:
        validated = ProposalBundle.model_validate(updated).model_dump(mode="json")
    except ValidationError as exc:
        raise BusinessError("MANIFEST_DECISION_VALUE_INVALID", "裁决值不符合候选对象的业务类型") from exc
    _validate_proposal_identity(validated)
    material_refs, material_source_refs = _proposal_reference_index(validated, "materials")
    field_refs, field_source_refs = _proposal_reference_index(validated, "fields")
    rule_refs, rule_source_refs = _proposal_reference_index(validated, "rules")
    for sheet in validated["sheets"]:
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
            if rule.get("source_rule_ref") and not _proposal_reference_exists(
                rule["source_rule_ref"], sheet_key, rule_refs, rule_source_refs
            ):
                raise BusinessError("MANIFEST_RISK_SOURCE_MISSING", "裁决后的风险规则来源不存在", details={"rule": rule["name"]})
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


def resolve_saved_decisions(proposals: dict[str, Any], decisions: list[dict[str, Any]]) -> dict[str, Any]:
    """只读地叠加已保存裁决，供业务视图展示有效值；绝不改写原始候选与来源记录。"""
    if not decisions:
        return deepcopy(proposals)
    return _apply_resolutions(proposals, decisions)


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
        and item.get("target_type") in {"MATERIAL", "FIELD", "RULE", "REPORT_SECTION"}
    }
    return excluded_sheets, excluded_refs


def resolved_targets(decisions: list[dict[str, Any]]) -> set[tuple[str, str]]:
    """返回已由人工裁决过的 (target_type, candidate_ref)。"""
    return {
        (item["target_type"], item["target_ref"])
        for item in decisions
        if item.get("action") == "RESOLVE"
        and item.get("target_type") in {"MATERIAL", "FIELD", "RULE", "REPORT_SECTION"}
    }


def _alias_map(
    items: list[dict[str, Any]],
    key_map: dict[str, str],
) -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    candidate_aliases: dict[str, str] = {}
    source_aliases: dict[tuple[str, str], str] = {}
    # 同一行业务记录按材料展开出的多条候选共用同一个编号：只有来自**不同**业务行的
    # 候选共用编号才是冲突。这里按行号区分，与 _validate_proposal_identity 同一口径。
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
        if len(rows_by_source.get(scope, set())) > 1:
            raise BusinessError(
                "MANIFEST_DUPLICATE_SOURCE_ID",
                "同一工作表内的来源编码重复：同一编号出现在多行业务记录上，无法生成场景",
                details={
                    "sheet_key": scope[0],
                    "source_ref": source_id,
                    "row_nos": sorted(rows_by_source[scope]),
                },
            )
        source_aliases.setdefault(scope, key)
    return candidate_aliases, source_aliases


def _resolve_alias(
    reference: str,
    owner_sheet_key: str,
    candidate_aliases: dict[str, str],
    source_aliases: dict[tuple[str, str], str],
    *,
    target_type: str,
    error_code: str,
    error_message: str,
    details: dict[str, Any],
) -> str:
    candidate = candidate_aliases.get(reference)
    if candidate is not None:
        return candidate
    source = source_aliases.get((owner_sheet_key, reference))
    if source is not None:
        return source
    cross_sheet_matches = {
        key
        for (_, source_ref), key in source_aliases.items()
        if source_ref == reference
    }
    if len(cross_sheet_matches) == 1:
        return next(iter(cross_sheet_matches))
    if cross_sheet_matches:
        raise BusinessError(
            "MANIFEST_SOURCE_SCOPE_INVALID",
            "来源编码在多个工作表中重复，无法跨工作表唯一解析，请使用候选引用关联",
            details={"target_type": target_type, "reference": reference, "owner_sheet_key": owner_sheet_key, **details},
        )
    raise BusinessError(error_code, error_message, details=details)


def compile_scene_manifest(
    *,
    workbook_ir: dict[str, Any],
    proposals: dict[str, Any],
    decisions: list[dict[str, Any]],
    scene_code: str,
    scene_name: str,
    namespace: str,
    compiled_at: datetime,
) -> dict[str, Any]:
    proposals = _apply_resolutions(proposals, decisions)
    excluded_sheets = {
        item["target_ref"]
        for item in decisions
        if item["target_type"] == "SHEET" and item["action"] == "EXCLUDE"
    }
    included = [item for item in proposals["sheets"] if item["sheet_key"] not in excluded_sheets]
    materials = [item for sheet in included for item in sheet.get("materials", []) if _accepted(decisions, "MATERIAL", item["candidate_ref"])]
    fields = [item for sheet in included for item in sheet.get("fields", []) if _accepted(decisions, "FIELD", item["candidate_ref"])]
    rules = [item for sheet in included for item in sheet.get("rules", []) if _accepted(decisions, "RULE", item["candidate_ref"])]
    sections = [
        item
        for sheet in included
        for item in sheet.get("report_sections", [])
        if _accepted(decisions, "REPORT_SECTION", item["candidate_ref"])
    ]
    # 非停用章节里仍标着「待确认」的，是尚未确认的业务口径，不能进已发布配置：
    # 章节携带展示字段、内容类型与内容要求，把它当成已确认会直接改变报告内容。
    # 它本来必须由业务确认或明确停用；发布检查会因仍有未处理问题先阻断，走到这里
    # 说明有人在未处理该问题的情况下继续，必须显式报错，不能静默带上或静默丢弃。
    pending_sections = [
        item
        for item in sections
        if item.get("config_status") == CONFIG_STATUS_PENDING
    ]
    if pending_sections:
        raise BusinessError(
            "MANIFEST_REPORT_SECTION_PENDING",
            "报告章节仍是「待确认」状态，不能发布：请确认该章节可以执行，或明确停用该章节",
            details={
                "sections": [
                    {"report_code": item.get("report_code"), "section_name": item.get("section_name")}
                    for item in pending_sections
                ]
            },
        )
    # 节点版的报告章节自带报告编号、材料范围、输出格式与结果排序；这些属性必须整组一致。
    # 旧格式没有这些列，保持原有的“附加展示章节”语义。
    explicit_report_groups = any(section.get("report_code") for section in sections)
    if not materials:
        raise BusinessError("MANIFEST_NO_MATERIAL", "场景至少需要一个材料位")
    if not rules:
        raise BusinessError("MANIFEST_NO_RULE", "场景至少需要一条启用规则")
    _ensure_unique_materials(materials)

    material_keys = {item["candidate_ref"]: stable_key("mat", namespace, item["candidate_ref"]) for item in materials}
    field_keys = {item["candidate_ref"]: stable_key("fld", namespace, item["candidate_ref"]) for item in fields}
    rule_keys = {item["candidate_ref"]: stable_key("rule", namespace, item["candidate_ref"]) for item in rules}
    material_candidate_aliases, material_source_aliases = _alias_map(materials, material_keys)
    field_candidate_aliases, field_source_aliases = _alias_map(fields, field_keys)
    rule_candidate_aliases, rule_source_aliases = _alias_map(rules, rule_keys)
    field_types = {field_keys[item["candidate_ref"]]: item["value_type"] for item in fields}
    field_units = {field_keys[item["candidate_ref"]]: item.get("unit") for item in fields}
    field_currencies = {field_keys[item["candidate_ref"]]: item.get("currency") for item in fields}
    rule_nodes = {rule_keys[item["candidate_ref"]]: item["node"] for item in rules}

    compiled_materials = []
    for item in materials:
        # 材料识别类型必须由业务确认后才能发布：8018 的 doc_type 是严格枚举，
        # 未声明的类型会让材料校验在运行时被拒。这里显式阻断，不代填、不用材料名称冒充。
        recognize_type = str(item.get("material_type") or "").strip()
        if not recognize_type:
            raise BusinessError(
                "MANIFEST_MATERIAL_RECOGNIZE_TYPE_MISSING",
                f"材料“{item['name']}”没有确认识别类型：请从 8018 支持的类型目录中选择后再发布",
                details={"material": item["name"]},
            )
        compiled_materials.append(
            {
                "material_key": material_keys[item["candidate_ref"]],
                "name": item["name"],
                # 材料位名称与 8018 识别类型分开固化：名称是业务叫法，
                # material_type 是提交给 8018 的 doc_type。
                "material_type": recognize_type,
                "required": item["required"],
                # 「业务说明」是业务含义的一部分，随材料一起进入已发布配置，不能被丢掉。
                "description": item.get("description", ""),
                "source_ref": item.get("source_id"),
                "source": item["source"],
            }
        )

    compiled_fields = []
    for item in fields:
        material_key = _resolve_alias(
            item["material_ref"],
            item["source"]["sheet_key"],
            material_candidate_aliases,
            material_source_aliases,
            target_type="MATERIAL",
            error_code="MANIFEST_MATERIAL_REF_MISSING",
            error_message="字段引用的材料不存在或已被排除",
            details={"field": item["name"]},
        )
        compiled_fields.append(
            {
                "field_key": field_keys[item["candidate_ref"]],
                "name": item["name"],
                "value_type": item["value_type"],
                "unit": item.get("unit"),
                # 币种是金额比较与加总的口径之一；留空表示未声明，不表示人民币。
                "currency": item.get("currency"),
                "material_key": material_key,
                "required": item["required"],
                "instruction": item["instruction"],
                # 产物来源（当前仅印章识别）：主体核对据此确定待核对主体取自哪种产物，
                # 不靠字段名里是否含“签章主体”来猜。留空表示常规字段抽取。
                "artifact_source": item.get("artifact_source"),
                "source_ref": item.get("source_id"),
                "source": item["source"],
            }
        )
    field_materials = {
        item["field_key"]: item["material_key"]
        for item in compiled_fields
    }
    field_names = {item["field_key"]: item["name"] for item in compiled_fields}
    # 字段的产物来源声明：主体核对的判定依据，不能靠字段中文名猜测。
    artifact_source_by_field = {
        item["field_key"]: item.get("artifact_source") for item in compiled_fields
    }

    node_positions = {code: index for index, (code, _) in enumerate(NODE_DEFINITIONS)}
    compiled_rules = []
    for item in rules:
        if item["operator"] not in OPERATOR_NODE_CONSTRAINTS or item["node"] not in OPERATOR_NODE_CONSTRAINTS[item["operator"]]:
            raise BusinessError(
                "MANIFEST_OPERATOR_NODE_INVALID",
                "规则算子不能在当前节点执行",
                details={"rule": item["name"], "operator": item["operator"], "node": item["node"]},
            )
        parameters = item.get("parameters", {})
        if item["operator"] == "DATE_COMPARE" and isinstance(parameters, dict) and parameters.get("formula_type") == "DECIMAL":
            raise BusinessError("MANIFEST_DATE_FORMULA_INVALID", "日期比较不能使用 DECIMAL 公式", details={"rule": item["name"]})
        # 公式类参数在节点版里没有位置：显式拒绝，不静默丢弃。
        _assert_no_legacy_formula_parameters(item["name"], item["operator"], parameters)
        _validate_rule_parameters(item["name"], item["operator"], parameters)
        # 引用的能力必须真实存在于能力目录：不在目录里的算子没有 handler，
        # 拖到运行时才发现会变成「导入成功、执行时静默跳过」。
        if capabilities.capability_of(item["operator"]) is None:
            raise BusinessError(
                "MANIFEST_OPERATOR_UNKNOWN",
                f"规则“{item['name']}”引用的算子不在能力目录中，系统无法确定它是否可执行",
                details={"rule": item["name"], "operator": item["operator"]},
            )
        # 能力判定分成两步，顺序不能颠倒：
        #
        # 1) **结构事实优先**：规则实际引用了几份材料、几个字段，是可核对的事实。
        #    节点版把「备注」作为 requirement 带入，而备注是说明性文字，里面经常出现
        #    「不能加总」「关联汇总风险」这类措辞——它们既可能是技术限制说明，也可能
        #    只是别的对象的名称。按文字判定会把整份规则库误拦，因此结构能定的先用结构定。
        #    （多材料内部校验的判定在下面 inputs 构造之后进行。）
        # 2) 文字判定只用于**真正的执行说明**：字段抽取指令（instruction）是写给系统的
        #    操作要求，出现在那里才代表业务要求系统做汇总取数。
        unsupported = detect_unsupported_capability_for_operator(
            item["operator"],
            parameters.get("instruction") if isinstance(parameters, dict) else None,
        )
        if unsupported:
            raise BusinessError(
                "RULE_CAPABILITY_UNSUPPORTED",
                f"规则“{item['name']}”包含当前不支持的能力：{unsupported[2]}",
                details={
                    "rule": item["name"],
                    "node": item["node"],
                    "operator": item["operator"],
                    "capability": unsupported[0],
                    "matched": unsupported[1],
                },
            )
        if item["operator"] in {"MATERIAL_EXISTS", "FILE_QUALITY_CHECK"} and not item.get("material_refs"):
            raise BusinessError("MANIFEST_RULE_MATERIAL_REQUIRED", "材料规则必须声明适用材料", details={"rule": item["name"]})
        if item["operator"] == "FILE_QUALITY_CHECK":
            # 检查项目与合格要求必须在发布期就能确定判定方式；口径未明确的检查
            # 只保留为能力缺口（见 capability_gaps），绝不在发布时承诺支持、运行时才发现做不了。
            check_item = parameters.get("check_item") if isinstance(parameters, dict) else None
            requirement = parameters.get("requirement") if isinstance(parameters, dict) else None
            if not check_item or not requirement:
                raise BusinessError(
                    "MANIFEST_QUALITY_PARAMETER_MISSING",
                    "文件处理检查必须声明检查项目与合格要求",
                    details={"rule": item["name"]},
                )
        if item["operator"] == "LLM_EXTRACT" and not item.get("output_field_refs"):
            raise BusinessError("MANIFEST_EXTRACTION_OUTPUT_REQUIRED", "字段抽取规则必须声明输出字段", details={"rule": item["name"]})
        # 风险疑点必须声明来源规则与成立条件；来源节点决定来源规则允许落在哪个节点。
        source_node: str | None = None
        risk_condition: str | None = None
        declared_source_refs: list[str] = []
        # 本规则全部来源规则实际所在的审核节点。节点版不维护来源节点列，
        # 风险结果里据此说明它监视的是哪一类检查；跨节点多来源时全部保留。
        actual_source_nodes: set[str] = set()
        if item["operator"] == "RISK_TRIGGER":
            declared_source_refs = list(item.get("source_rule_refs") or [])
            if item.get("source_rule_ref") and item["source_rule_ref"] not in declared_source_refs:
                declared_source_refs.insert(0, item["source_rule_ref"])
            if not declared_source_refs:
                raise BusinessError("MANIFEST_RISK_SOURCE_REQUIRED", "风险规则必须声明来源规则", details={"rule": item["name"]})
            risk_condition = _risk_condition_of(item)
        if item["node"] == "RISK_RATING" and item.get("parameters", {}).get("requires_manual_review") is True:
            raise BusinessError(
                "MANIFEST_RISK_REVIEW_NODE_ORDER_INVALID",
                "风险评定位于人工复核节点之后，不能要求新增人工复核",
                details={"rule": item["name"]},
            )
        inputs = []
        for rule_input in item["inputs"]:
            field_key = _resolve_alias(
                rule_input["field_ref"],
                item["source"]["sheet_key"],
                field_candidate_aliases,
                field_source_aliases,
                target_type="FIELD",
                error_code="MANIFEST_RULE_INPUT_MISSING",
                error_message="规则输入字段不存在或已被排除",
                details={"rule": item["name"], "field_ref": rule_input["field_ref"]},
            )
            material_key = _resolve_alias(
                rule_input["material_ref"],
                item["source"]["sheet_key"],
                material_candidate_aliases,
                material_source_aliases,
                target_type="MATERIAL",
                error_code="MANIFEST_RULE_INPUT_MISSING",
                error_message="规则输入材料不存在或已被排除",
                details={"rule": item["name"], "material_ref": rule_input["material_ref"]},
            )
            if field_materials[field_key] != material_key:
                raise BusinessError(
                    "MANIFEST_RULE_INPUT_MATERIAL_MISMATCH",
                    "规则输入材料与字段所属材料不一致",
                    details={
                        "rule": item["name"],
                        "field_ref": rule_input["field_ref"],
                        "material_ref": rule_input["material_ref"],
                    },
                )
            inputs.append(
                {
                    "field_key": field_key,
                    "material_key": material_key,
                }
            )
        output_keys = []
        for ref in item.get("output_field_refs", []):
            output_keys.append(
                _resolve_alias(
                    ref,
                    item["source"]["sheet_key"],
                    field_candidate_aliases,
                    field_source_aliases,
                    target_type="FIELD",
                    error_code="MANIFEST_RULE_OUTPUT_MISSING",
                    error_message="规则输出字段不存在或已被排除",
                    details={"rule": item["name"], "field_ref": ref},
                )
            )
        material_scope = []
        for ref in item.get("material_refs", []):
            material_scope.append(
                _resolve_alias(
                    ref,
                    item["source"]["sheet_key"],
                    material_candidate_aliases,
                    material_source_aliases,
                    target_type="MATERIAL",
                    error_code="MANIFEST_RULE_MATERIAL_MISSING",
                    error_message="规则材料范围不存在或已被排除",
                    details={"rule": item["name"], "material_ref": ref},
                )
            )
        source_rule_key = None
        source_rule_keys: list[str] = []
        if declared_source_refs:
            for declared in declared_source_refs:
                resolved = _resolve_alias(
                    declared,
                    item["source"]["sheet_key"],
                    rule_candidate_aliases,
                    rule_source_aliases,
                    target_type="RULE",
                    error_code="MANIFEST_RISK_SOURCE_MISSING",
                    error_message="风险规则的来源规则不存在或已被排除",
                    details={"rule": item["name"], "source_rule_ref": declared},
                )
                actual_node = rule_nodes[resolved]
                if node_positions[item["node"]] <= node_positions[actual_node]:
                    raise BusinessError("MANIFEST_NODE_ORDER_INVALID", "风险规则必须位于来源规则之后", details={"rule": item["name"]})
                # 来源节点必须与来源规则的实际节点一致：声明与实际不符时显式报错，
                # 不按实际节点改写来源节点，避免风险挂到业务没有声明的节点上。
                #
                # 声明来源节点是「06_风险评定」的**可选**业务列（只有旧格式有这一列）。
                # 节点版（contract_review_v3）不要求业务重复维护来源节点：来源由检查行的
                # 「关联风险」反向确定，一条风险可以跨节点关联多条来源检查。这种情况下
                # 按来源规则的实际节点校验关系，而不是对 None 做下标取值。
                declared_node = item.get("source_node")
                if declared_node is not None and actual_node not in SOURCE_NODE_RULE_NODES.get(declared_node, frozenset()):
                    raise BusinessError(
                        "MANIFEST_RISK_SOURCE_NODE_MISMATCH",
                        "风险疑点的来源审核规则不在指定的来源节点中",
                        details={
                            "rule": item["name"],
                            "source_node": declared_node,
                            "source_rule_ref": declared,
                            "actual_node": actual_node,
                            "expected_nodes": sorted(SOURCE_NODE_RULE_NODES.get(declared_node, frozenset())),
                        },
                    )
                actual_source_nodes.add(actual_node)
                if resolved not in source_rule_keys:
                    source_rule_keys.append(resolved)
            source_rule_key = source_rule_keys[0]
        comparison_operators = {
            "FIELD_EQUAL",
            "FIELD_NOT_EQUAL",
            "DATE_COMPARE",
            "DECIMAL_COMPARE",
            "DECIMAL_SUM_COMPARE",
        }
        if item["operator"] in comparison_operators:
            # 加总比较的右侧是多个加数：至少一个左字段与一个加数；单字段比较仍然必须恰好两个输入。
            minimum = 2
            maximum = None if operator_uses_addends(item["operator"]) else 2
            if len(inputs) < minimum or (maximum is not None and len(inputs) > maximum):
                raise BusinessError(
                    "MANIFEST_RULE_INPUT_COUNT_INVALID",
                    "金额加总比较必须包含左侧字段和至少一个加数"
                    if operator_uses_addends(item["operator"])
                    else "比较规则必须恰好包含两个输入字段",
                    details={"rule": item["name"], "input_count": len(inputs)},
                )
            # 「文件内部校验」的字段必须属于同一份材料：加总式的全部加数继承同一份适用材料，
            # 跨材料的比较属于交叉校验，不能靠改一行材料引用把跨文件比较混进内部校验。
            if item["node"] == "INTERNAL_VALIDATE" and len({item["material_key"] for item in inputs}) != 1:
                raise BusinessError(
                    "MANIFEST_INTERNAL_MATERIAL_MISMATCH",
                    "文件内部校验的字段必须属于同一份材料",
                    details={
                        "rule": item["name"],
                        "left_material_key": inputs[0]["material_key"],
                        "left_field_key": inputs[0]["field_key"],
                        "material_keys": sorted({item["material_key"] for item in inputs}),
                    },
                )
            input_types = [field_types[input_item["field_key"]] for input_item in inputs]
            if item["operator"] == "DATE_COMPARE" and any(value_type != "DATE" for value_type in input_types):
                raise BusinessError("MANIFEST_RULE_TYPE_INVALID", "日期比较规则只能引用 DATE 字段", details={"rule": item["name"]})
            if item["operator"] in {"DECIMAL_COMPARE", "DECIMAL_SUM_COMPARE"} and any(
                value_type != "DECIMAL" for value_type in input_types
            ):
                raise BusinessError(
                    "MANIFEST_RULE_TYPE_INVALID",
                    "金额比较规则只能引用金额字段（不接收大写金额字段）",
                    details={"rule": item["name"]},
                )
            if item["operator"] in {"FIELD_EQUAL", "FIELD_NOT_EQUAL"} and len(set(input_types)) != 1:
                raise BusinessError("MANIFEST_RULE_TYPE_INVALID", "字段比较规则的两个字段类型必须一致", details={"rule": item["name"]})
            unit_mismatch = _comparison_unit_mismatch(
                item["operator"],
                [{"unit": field_units.get(input_item["field_key"])} for input_item in inputs],
            )
            if unit_mismatch:
                # 单位/币种不一致时系统不做换算：这类规则不能发布后再以“数值相等”通过。
                raise BusinessError(
                    "MANIFEST_RULE_UNIT_MISMATCH",
                    f"规则“{item['name']}”不能发布：{unit_mismatch}",
                    details={
                        "rule": item["name"],
                        "units": sorted(
                            {
                                (field_units.get(input_item["field_key"]) or "")
                                for input_item in inputs
                            }
                        ),
                    },
                )
            # 加总比较的加数单位必须与字段归一化单位一致，否则“求和”本身没有意义。
            if operator_uses_addends(item["operator"]):
                for input_item in inputs:
                    if not (field_units.get(input_item["field_key"]) or "").strip():
                        raise BusinessError(
                            "MANIFEST_RULE_UNIT_MISSING",
                            f"规则“{item['name']}”需要加总，但引用的字段没有声明归一化单位："
                            "系统不默认单位，也不做单位换算",
                            details={"rule": item["name"], "field_key": input_item["field_key"]},
                        )
            # 金额比较与加总（含大小写金额一致性）：币种必须都声明且一致；
            # 币种未声明时不能做数值比较或求和，系统也不默认人民币。
            if item["operator"] in {"DECIMAL_COMPARE", "DECIMAL_SUM_COMPARE"} or "DECIMAL" in input_types or "DECIMAL_CN" in input_types:
                currencies = {field_currencies.get(input_item["field_key"]) for input_item in inputs}
                missing_currency = any(not (value or "").strip() for value in currencies)
                declared = {value for value in currencies if (value or "").strip()}
                if missing_currency:
                    raise BusinessError(
                        "MANIFEST_RULE_CURRENCY_MISSING",
                        f"规则“{item['name']}”引用的字段没有在 03_字段抽取 中声明币种："
                        "系统不默认人民币，币种未知时不做数值比较或求和",
                        details={"rule": item["name"]},
                    )
                if len(declared) > 1:
                    raise BusinessError(
                        "MANIFEST_RULE_CURRENCY_MISMATCH",
                        f"规则“{item['name']}”比较双方币种不一致（{' / '.join(sorted(declared))}）："
                        "系统不做汇率换算，也不能直接相加",
                        details={"rule": item["name"], "currencies": sorted(declared)},
                    )
        if item["operator"] == "FIELD_NOT_EMPTY":
            if len(inputs) != 1:
                raise BusinessError(
                    "MANIFEST_RULE_INPUT_COUNT_INVALID",
                    "非空校验必须且只能引用一个字段",
                    details={"rule": item["name"], "input_count": len(inputs)},
                )
        if item["operator"] == "CLAUSE_ABSENT":
            if len(inputs) != 1:
                raise BusinessError(
                    "MANIFEST_RULE_INPUT_COUNT_INVALID",
                    "条款存在性检查必须且只能引用一个条款字段",
                    details={"rule": item["name"], "input_count": len(inputs)},
                )
            if field_types[inputs[0]["field_key"]] != "CLAUSE":
                raise BusinessError(
                    "MANIFEST_RULE_TYPE_INVALID",
                    "条款存在性检查只能引用条款字段",
                    details={"rule": item["name"]},
                )
        if item["operator"] == "LLM_VALIDATE" and not inputs:
            raise BusinessError("MANIFEST_RULE_INPUT_COUNT_INVALID", "智能校验规则必须声明输入字段", details={"rule": item["name"]})
        # 1 对 1 的输入数量限制在 manifest 层兜底，不依赖导入层已经纠正过输入：
        # 内部校验的智能判断只针对一份材料的字段（最多一个输入），交叉校验必须左右各一个字段。
        if item["node"] == "INTERNAL_VALIDATE" and item["operator"] == "LLM_VALIDATE" and len(inputs) > 1:
            raise BusinessError(
                "MANIFEST_RULE_INPUT_COUNT_INVALID",
                "文件内部校验的智能判断最多只能引用一个字段",
                details={
                    "rule": item["name"],
                    "node": item["node"],
                    "input_count": len(inputs),
                    "max_input_count": 1,
                },
            )
        # 交叉校验要求左右两侧都有字段，但不强求恰好两个输入：加总比较的右侧是多个加数，
        # 「结算总金额 = 已付款金额 + 未付款金额」展开后就是三个输入。与表单路径
        # （compile_form_manifest 里的 comparison_operators 分支）同一口径。
        if item["node"] == "CROSS_VALIDATE" and (
            len(inputs) < 2 or (not operator_uses_addends(item["operator"]) and len(inputs) > 2)
        ):
            raise BusinessError(
                "MANIFEST_RULE_INPUT_COUNT_INVALID",
                "文件交叉校验必须引用左右两侧的字段（加总比较的右侧可以有多个加数）"
                if operator_uses_addends(item["operator"])
                else "文件交叉校验必须左右各引用一个字段，共两个输入",
                details={
                    "rule": item["name"],
                    "node": item["node"],
                    "input_count": len(inputs),
                    "required_input_count": 2,
                },
            )
        if item["operator"] == "LLM_EXTRACT":
            if any(field_materials[output_key] not in material_scope for output_key in output_keys):
                raise BusinessError("MANIFEST_EXTRACTION_MATERIAL_INVALID", "字段抽取规则的输出字段不属于规则材料范围", details={"rule": item["name"]})
        compiled_rules.append(
            {
                "rule_key": rule_keys[item["candidate_ref"]],
                "name": item["name"],
                "node": item["node"],
                "operator": item["operator"],
                # 配置状态=停用 的行不参与本次规则库：它们不进入执行，也不进入风险汇总。
                # 其余状态（已明确 / 待确认）进入已发布配置；待确认项由发布检查按未处理问题阻断，
                # 不在这里静默当成“已确认”。
                "enabled": item.get("config_status") != CONFIG_STATUS_DISABLED,
                "inputs": inputs,
                "outputs": output_keys,
                "materials": material_scope,
                # 风险来源可能有多条（任一 / 全部）。单一 source_rule_key 保留为第一条，
                # 供旧读取路径与页面“来源规则”列继续可用；完整来源集合进入 source_rule_keys。
                "source_rule_key": source_rule_key or (source_rule_keys[0] if source_rule_keys else None),
                "source_rule_keys": source_rule_keys,
                # 来源节点与 rule_key 同级进入已发布配置，保证风险来源可追溯；
                # 它不属于 RISK_TRIGGER 的参数契约，因此不写进 parameters。
                # 节点版没有来源节点列时如实留空，不按来源规则反推一个业务没有声明的节点；
                # 实际节点集合单独记录，供执行结果与报告说明真实来源。
                "source_node": source_node,
                "source_nodes": sorted(actual_source_nodes),
                "parameters": parameters,
                # 适用条件与审核要求原文是执行与证据链的一部分，随规则一起进入已发布配置。
                "applicability": _compiled_applicability(item, inputs, material_scope),
                "requirement": item.get("requirement", ""),
                # 业务配置事实随已发布配置一起固化：停用行不会进入执行，检查等级/风险等级
                # 原文与风险成立条件必须保留，否则报告与风险结果无法说明依据。
                "config_status": item.get("config_status"),
                "check_operator": item.get("check_operator"),
                "check_level": item.get("check_level"),
                "risk_level_value": item.get("risk_level_value"),
                "risk_condition": item.get("risk_condition"),
                "quality_supported": item.get("quality_supported"),
                "source_ref": item.get("source_id"),
                "source": item["source"],
            }
        )
        # 主体/签章一致性检查：规则引用了**显式声明产物来源=印章识别**的字段。
        # 判定依据是配置声明（03_字段抽取 的「产物来源」列），不是字段名里是否含某几个汉字：
        # 中文名可以随便改，靠名字猜产物归属会把主体核对接到错误的证据源上。
        # 这类规则的识别冲突或归属不明必须展示完整印章记录并进入人工复核。
        #
        # 必须先判定主体标记再算 route：execution_route 内部读的正是这个标记，
        # 顺序颠倒会让主体规则拿不到 SEAL_SUBJECT_RETRIEVAL 取证方式，
        # 索引准备、检索分支与执行分支就会互相不一致。
        if compiled_rules[-1]["operator"] in {"FIELD_EQUAL", "FIELD_NOT_EQUAL"}:
            compiled_rules[-1]["subject_check"] = any(
                (artifact_source_by_field.get(item["field_key"]) or "") == "SEAL" for item in inputs
            )
        # 取证方式与判定方式在编译期固化：运行时按冻结计划分派，不让大模型临时决定路由。
        compiled_rules[-1]["route"] = execution_route(compiled_rules[-1])

    extraction_output_keys = {
        output_key
        for rule in compiled_rules
        if rule["operator"] == "LLM_EXTRACT"
        for output_key in rule["outputs"]
    }
    extraction_output_counts: dict[str, int] = {}
    for rule in compiled_rules:
        if rule["operator"] != "LLM_EXTRACT":
            continue
        for output_key in rule["outputs"]:
            extraction_output_counts[output_key] = extraction_output_counts.get(output_key, 0) + 1
    duplicate_extraction_outputs = sorted(
        output_key for output_key, count in extraction_output_counts.items() if count > 1
    )
    if duplicate_extraction_outputs:
        raise BusinessError(
            "MANIFEST_EXTRACTION_OUTPUT_DUPLICATE",
            "同一字段不能由多个字段抽取规则同时产出",
            details={"field_keys": duplicate_extraction_outputs},
        )
    for field in compiled_fields:
        if field["field_key"] in extraction_output_keys:
            continue
        extraction_parameters = {"instruction": field["instruction"]}
        _validate_rule_parameters(f"抽取{field['name']}", "LLM_EXTRACT", extraction_parameters)
        compiled_rules.append(
            {
                "rule_key": stable_key("rule", namespace, f"extract:{field['field_key']}"),
                "name": f"抽取{field['name']}",
                "node": "FIELD_EXTRACTION",
                "operator": "LLM_EXTRACT",
                "enabled": True,
                "inputs": [],
                "outputs": [field["field_key"]],
                "materials": [field["material_key"]],
                "source_rule_key": None,
                "source_rule_keys": [],
                "source_node": None,
                "source_nodes": [],
                "parameters": extraction_parameters,
                # 抽取规则的适用条件不是用户配置项：保持“始终适用”，材料未上传时由执行期
                # 呈现“缺输入”，不能借适用条件把缺材料说成规则不适用。
                "applicability": {"kind": "ALWAYS", "material_keys": []},
                "requirement": field["instruction"],
                # 系统派生的抽取规则没有业务配置状态与等级：显式为 None，不伪造业务口径。
                "config_status": None,
                "check_operator": None,
                "check_level": None,
                "risk_level_value": None,
                "risk_condition": None,
                "quality_supported": None,
                "source_ref": field.get("source_ref"),
                "source": field["source"],
            }
        )

    report_template = []
    for section in sections:
        # 停用章节不进入报告：Excel 里的停用章节在读取阶段就不产生候选，
        # 经裁决改成停用的章节必须在编译期同样丢弃，否则两条路径结果不一致。
        if section.get("config_status") == CONFIG_STATUS_DISABLED:
            continue
        field_refs = []
        for ref in section.get("field_refs", []):
            field_refs.append(
                _resolve_alias(
                    ref,
                    section["source"]["sheet_key"],
                    field_candidate_aliases,
                    field_source_aliases,
                    target_type="FIELD",
                    error_code="MANIFEST_REPORT_FIELD_MISSING",
                    error_message="报告章节引用的字段不存在或已被排除",
                    details={"section": section["section_name"], "field_ref": ref},
                )
            )
        material_scope = []
        for ref in section.get("material_refs", []):
            material_scope.append(
                _resolve_alias(
                    ref,
                    section["source"]["sheet_key"],
                    material_candidate_aliases,
                    material_source_aliases,
                    target_type="MATERIAL",
                    error_code="MANIFEST_REPORT_MATERIAL_MISSING",
                    error_message="报告章节引用的材料范围不存在或已被排除",
                    details={"section": section["section_name"], "material_ref": ref},
                )
            )
        # 展示字段必须属于报告材料范围：范围外材料不能自动进入普通字段明细。
        for field_key in field_refs:
            if material_scope and field_materials[field_key] not in material_scope:
                raise BusinessError(
                    "MANIFEST_REPORT_FIELD_OUT_OF_SCOPE",
                    f"报告章节“{section['section_name']}”的展示字段不属于该报告的材料范围",
                    details={"section": section["section_name"], "field_key": field_key},
                )
        report_template.append({**section, "field_keys": field_refs, "material_keys": material_scope})
        report_template[-1].pop("field_refs", None)
        report_template[-1].pop("material_refs", None)

    report_groups = _build_report_groups(report_template, explicit_report_groups)
    report_template.sort(key=lambda item: (item.get("report_code") or "", item.get("section_order") or 0))

    if not any(
        item["node"] == "REPORT_OUTPUT" and item["operator"] == "REPORT_RENDER" for item in compiled_rules
    ):
        # 生成报告不需要业务决策，也不需要附加章节：固定事实报告是系统能力，
        # 因此报告规则无条件由后端确定性补齐，最小模板（无 07_报告模板）同样可发布。
        compiled_rules.append(
            {
                "rule_key": stable_key("rule", namespace, "report:render"),
                "name": "生成审核报告",
                "node": "REPORT_OUTPUT",
                "operator": "REPORT_RENDER",
                "enabled": True,
                "inputs": [],
                "outputs": [],
                "materials": [],
                "source_rule_key": None,
                "source_rule_keys": [],
                "source_node": None,
                "source_nodes": [],
                "parameters": {},
                "applicability": {"kind": "ALWAYS", "material_keys": []},
                "requirement": "",
                "config_status": None,
                "check_operator": None,
                "check_level": None,
                "risk_level_value": None,
                "risk_condition": None,
                "quality_supported": None,
                "source_ref": None,
                "source": sections[0]["source"] if sections else {"sheet_key": None, "row_no": None, "cells": []},
            }
        )

    _ensure_unique_rules(compiled_rules)
    _ensure_manifest_node_coverage(compiled_rules, report_template)

    manifest = {
        "manifest_version": "1.0",
        "profile_version": workbook_ir["profile_version"],
        "scene_code": scene_code,
        "scene_name": scene_name,
        "materials": sorted(compiled_materials, key=lambda item: item["material_key"]),
        "fields": sorted(compiled_fields, key=lambda item: item["field_key"]),
        "rules": sorted(compiled_rules, key=lambda item: item["rule_key"]),
        "nodes": [
            {"node_code": code, "name": name, "sequence_no": index, "enabled": True}
            for index, (code, name) in enumerate(NODE_DEFINITIONS, start=1)
        ],
        "report_template": report_template,
        # 报告分组：报告编号、报告名称、材料范围、输出格式、结果排序与章节顺序。
        # 「报告只能是附加章节」的旧范围在这里更新为按报告编号分组的多报告配置。
        "report_groups": report_groups,
        # 显式记录本模板未配置的业务校验节点，供任务节点展示与报告说明使用。
        "unconfigured_check_nodes": unconfigured_check_nodes(compiled_rules),
        # 已配置但当前无法无条件执行的检查。它们在预览里必须可见，
        # 不能被当成已支持的能力，也不能在执行时静默跳过。
        "capability_gaps": capability_gaps(compiled_rules),
        # 能力覆盖结果随版本一起冻结（参与 manifest_hash）：每条启用规则引用了哪项能力、
        # 哪一版、输入形态、可表达的状态与输出键、以及**发布当时**是否已实现。
        # 后续能力实现状态变化不会重新解释这个已发布版本的含义。
        "capability_coverage": capabilities.coverage_of(compiled_rules),
        "capability_version": capabilities.CAPABILITY_VERSION,
        "compiled_at": compiled_at.isoformat(),
        "source_workbook_id": workbook_ir["workbook_id"],
    }
    canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    manifest["manifest_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return manifest


def build_task_schema(
    manifest: dict[str, Any],
    enabled_rule_keys: list[str] | None,
    *,
    allow_empty: bool = False,
) -> dict[str, Any]:
    known_rules = {item["rule_key"]: item for item in manifest["rules"]}
    available_keys = {key for key, rule in known_rules.items() if rule.get("enabled", True)}
    # 报告生成是系统技术规则：用户误取消它不能让任务无法创建，由后端保证补回。
    system_rule_keys = {
        key for key, rule in known_rules.items() if rule["node"] == "REPORT_OUTPUT" and rule["operator"] == "REPORT_RENDER"
    }
    if enabled_rule_keys is None:
        selected_keys = set(available_keys)
    elif not enabled_rule_keys and not allow_empty:
        raise BusinessError("TASK_RULE_EMPTY", "创建任务时至少需要启用一条规则")
    elif not enabled_rule_keys:
        # allow_empty 仅供方案范围预览使用：空显式集合时仍要展示“系统会补入哪些规则、
        # 哪些业务规则未启用”，不能伪装成全部启用，也不能按建任务门禁直接报错。
        selected_keys = set()
    else:
        selected_keys = set(enabled_rule_keys)
    unknown = selected_keys - known_rules.keys()
    if unknown:
        raise BusinessError("TASK_RULE_UNKNOWN", "创建任务时选择了不存在的规则", details={"rule_keys": sorted(unknown)})
    disabled = selected_keys - available_keys
    if disabled:
        raise BusinessError("TASK_RULE_DISABLED", "创建任务时选择了未启用规则", details={"rule_keys": sorted(disabled)})

    requested_keys = set(selected_keys)
    selected_keys |= system_rule_keys

    # 自动补入的每一条规则都要能说出真实原因：报告生成能力、风险来源依赖、
    # 或字段抽取依赖。页面据此说明“为什么用户没选它却仍会执行”。
    fields_by_key = {item["field_key"]: item for item in manifest["fields"]}
    auto_reasons: dict[str, str] = {
        key: "报告生成依赖：报告输出由系统保证执行" for key in system_rule_keys
    }

    # 任务执行需要先完成来源规则和字段抽取；闭包写入任务快照，后续节点不再猜测依赖。
    required_fields = {
        field_key for section in manifest.get("report_template", []) for field_key in section.get("field_keys", [])
    }
    while True:
        before = set(selected_keys)
        for rule_key in sorted(selected_keys):
            rule = known_rules[rule_key]
            # 风险可能关联多条来源检查（任一 / 全部）：全部来源都要进入任务范围，
            # 只补第一条会让风险在运行时拿不到其余来源的实际结果。
            for source_key in (rule.get("source_rule_keys") or ([rule["source_rule_key"]] if rule.get("source_rule_key") else [])):
                source_rule = known_rules.get(source_key)
                if not source_rule or not source_rule.get("enabled", True):
                    raise BusinessError(
                        "TASK_RULE_DEPENDENCY_MISSING",
                        "任务风险规则依赖的来源规则不可用",
                        details={"rule_key": rule_key, "source_rule_key": source_key},
                    )
                if source_key not in requested_keys:
                    auto_reasons.setdefault(
                        source_key,
                        f"风险来源依赖：由风险规则「{rule.get('name', rule_key)}」引用",
                    )
                selected_keys.add(source_key)
            required_fields.update(item["field_key"] for item in rule.get("inputs", []))

        extraction_producers: dict[str, list[str]] = {}
        for rule_key, rule in known_rules.items():
            if rule.get("enabled", True) and rule["operator"] == "LLM_EXTRACT":
                for field_key in rule.get("outputs", []):
                    extraction_producers.setdefault(field_key, []).append(rule_key)
        for field_key in sorted(required_fields):
            producers = extraction_producers.get(field_key, [])
            if len(producers) != 1:
                raise BusinessError(
                    "TASK_FIELD_EXTRACTION_DEPENDENCY_INVALID",
                    "任务字段缺少唯一的字段抽取规则",
                    details={"field_key": field_key, "producer_rule_keys": sorted(producers)},
                )
            if producers[0] not in requested_keys:
                auto_reasons.setdefault(
                    producers[0],
                    f"字段抽取依赖：产出字段「{fields_by_key.get(field_key, {}).get('name', field_key)}」",
                )
            selected_keys.add(producers[0])
        if selected_keys == before:
            break

    selected_rules = [known_rules[key] for key in sorted(selected_keys)]
    # 建任务必须覆盖必需节点；范围预览（allow_empty）不启用任何业务规则时允许为空，
    # 节点覆盖由方案发布与建任务的门禁保证。
    if not allow_empty:
        _ensure_task_node_coverage(selected_rules)
    field_keys = required_fields | {
        field_key
        for rule in selected_rules
        if rule["operator"] == "LLM_EXTRACT"
        for field_key in rule.get("outputs", [])
    }
    missing = field_keys - fields_by_key.keys()
    if missing:
        raise BusinessError("TASK_SCHEMA_FIELD_MISSING", "任务规则引用的字段不在场景字段集合中", details={"field_keys": sorted(missing)})
    auto_included = sorted(selected_keys - requested_keys)
    return {
        "schema_version": "1.0",
        "enabled_rule_keys": sorted(rule["rule_key"] for rule in selected_rules),
        # 后端为满足依赖与系统能力自动补入的规则：必须在建任务前对用户可见，
        # 否则用户会以为自己取消掉的规则真的不会执行。
        "auto_included_rule_keys": auto_included,
        # 每条自动补入规则的真实原因（报告生成 / 风险来源 / 字段抽取），供页面直显。
        "auto_included_reasons": {key: auto_reasons[key] for key in auto_included if key in auto_reasons},
        # 模板里存在、但本任务明确未启用的规则：页面与报告据此区分"未启用"与"未配置"。
        "unenabled_rule_keys": sorted(available_keys - selected_keys),
        # 本模板未配置的业务校验节点（模板级事实，不是任务级选择）。
        "unconfigured_check_nodes": list(manifest.get("unconfigured_check_nodes", [])),
        "fields": [fields_by_key[key] for key in sorted(field_keys)],
    }


# 页面表单配置的来源标记：材料/字段/规则/报告章节由页面直接维护，没有 Excel 工作表，
# 因此 source 中不包含工作表与行号——不伪造导入作业、工作表行号或供应商结果。
FORM_SOURCE = {"sheet_key": None, "row_no": None, "cells": []}

# 页面表单允许维护的字段类型。它必须与 03_字段抽取 的「数据类型」下拉一致，
# 包含大写金额（DECIMAL_CN）：只接受五种会让页面上确实存在的选项在保存时被拒绝，
# 也会让草稿无法保留导入进来的大写金额字段。
FORM_VALUE_TYPES = {"STRING", "DECIMAL", "DECIMAL_CN", "DATE", "BOOLEAN", "CLAUSE"}


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


def compile_form_manifest(
    *,
    scene_code: str,
    scene_name: str,
    namespace: str,
    materials: list[dict[str, Any]],
    fields: list[dict[str, Any]],
    rules: list[dict[str, Any]],
    sections: list[dict[str, Any]],
    profile_version: str,
    compiled_at: datetime,
) -> dict[str, Any]:
    """把页面表单维护的业务配置编译成与导入流程同构的 manifest。

    输入的 materials/fields/rules/sections 是草稿的原始业务定义：
      materials: {material_key, name, material_type, required, description}
      fields:    {field_key, material_key, name, value_type, unit, required, instruction}
      rules:     {rule_key, name, node, operator, enabled, inputs:[{field_key, material_key}],
                  materials:[material_key], source_rule_key, source_node, parameters,
                  applicability:{kind, material_keys}, requirement}
      sections:  {section_name, content_instruction, field_keys}

    校验口径与 compile_scene_manifest 完全一致（同一个参数契约、能力判定、节点约束、
    比较类型与单位校验），差别只在于引用已经是稳定键、且没有工作表来源。
    字段抽取规则与报告生成规则按同一确定性规则派生：字段抽取每条字段一条，
    报告生成无条件补齐——最小配置同样可以发布并生成固定事实报告。
    """
    if not materials:
        raise BusinessError("MANIFEST_NO_MATERIAL", "场景至少需要一个材料位")
    business_rules = [item for item in rules if item.get("operator") not in {"LLM_EXTRACT", "REPORT_RENDER"}]
    if not business_rules:
        raise BusinessError("MANIFEST_NO_RULE", "场景至少需要一条启用规则")

    material_by_key: dict[str, dict[str, Any]] = {}
    material_names_seen: dict[tuple[str, str], dict[str, Any]] = {}
    for item in materials:
        key = str(item.get("material_key") or "")
        name = str(item.get("name") or "").strip()
        material_type = str(item.get("material_type") or "").strip()
        if not key or key in material_by_key:
            raise BusinessError("MANIFEST_DUPLICATE_MATERIAL_KEY", "材料配置缺少唯一标识", details={"material_key": key})
        if not name:
            raise BusinessError("MANIFEST_MATERIAL_NAME_REQUIRED", "材料名称不能为空", details={"material_key": key})
        # 与导入路径同口径：识别类型必须由业务确认，不能用材料名称冒充。
        if not material_type:
            raise BusinessError(
                "MANIFEST_MATERIAL_RECOGNIZE_TYPE_MISSING",
                f"材料“{name}”没有确认识别类型：请从 8018 支持的类型目录中选择后再发布",
                details={"material_key": key, "material": name},
            )
        signature = (_normalize_text(name), _normalize_text(material_type))
        if signature in material_names_seen:
            raise BusinessError(
                "MANIFEST_DUPLICATE_MATERIAL",
                "场景中存在重复材料位",
                details={"name": name, "material_type": material_type},
            )
        material_names_seen[signature] = item
        material_by_key[key] = item

    compiled_materials = [
        {
            "material_key": item["material_key"],
            "name": item["name"].strip(),
            "material_type": str(item.get("material_type") or "").strip(),
            "required": bool(item.get("required")),
            "description": str(item.get("description") or ""),
            # 来源业务编号：由导入 / 修订带入草稿，导出时按它写回 Excel 的编号列。
            # 页面新建的行没有编号，保持 None（导出为空），不伪造一个。
            "source_ref": item.get("source_ref"),
            "source": dict(FORM_SOURCE),
        }
        for item in materials
    ]

    field_by_key: dict[str, dict[str, Any]] = {}
    compiled_fields: list[dict[str, Any]] = []
    field_types: dict[str, str] = {}
    field_units: dict[str, str | None] = {}
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
        if material_key not in material_by_key:
            raise BusinessError(
                "MANIFEST_FIELD_MATERIAL_MISSING",
                "字段所属材料不存在或已被删除",
                details={"field_name": name, "material_key": material_key},
            )
        field_by_key[key] = item
        field_types[key] = value_type
        field_units[key] = item.get("unit")
        field_materials[key] = material_key
        compiled_fields.append(
            {
                "field_key": key,
                "name": name,
                "value_type": value_type,
                "unit": item.get("unit"),
                # 币种是金额比较与加总的口径之一；留空表示未声明，不表示人民币。
                "currency": item.get("currency"),
                "material_key": material_key,
                "required": bool(item.get("required")),
                "instruction": str(item.get("instruction") or ""),
                "artifact_source": item.get("artifact_source"),
                "source_ref": item.get("source_ref"),
                "source": dict(FORM_SOURCE),
            }
        )

    compiled_rules: list[dict[str, Any]] = []
    rule_keys: set[str] = set()
    rule_nodes: dict[str, str] = {}
    for item in business_rules:
        key = str(item.get("rule_key") or "")
        name = str(item.get("name") or "").strip()
        operator = str(item.get("operator") or "")
        node = str(item.get("node") or "")
        if not key or key in rule_keys:
            raise BusinessError("MANIFEST_DUPLICATE_RULE_KEY", "规则配置缺少唯一标识", details={"rule_key": key})
        if not name:
            raise BusinessError("MANIFEST_RULE_NAME_REQUIRED", "规则名称不能为空", details={"rule_key": key})
        if operator not in OPERATOR_NODE_CONSTRAINTS or node not in OPERATOR_NODE_CONSTRAINTS[operator]:
            raise BusinessError(
                "MANIFEST_OPERATOR_NODE_INVALID",
                "规则算子不能在当前节点执行",
                details={"rule": name, "operator": operator, "node": node},
            )
        parameters = item.get("parameters") if isinstance(item.get("parameters"), dict) else {}
        if operator == "DATE_COMPARE" and parameters.get("formula_type") == "DECIMAL":
            raise BusinessError("MANIFEST_DATE_FORMULA_INVALID", "日期比较不能使用 DECIMAL 公式", details={"rule": name})
        _validate_rule_parameters(name, operator, parameters)
        unsupported = detect_unsupported_capability_for_operator(
            item["operator"],
            item.get("requirement"),
            _form_parameter_instruction(parameters),
        )
        if unsupported:
            raise BusinessError(
                "RULE_CAPABILITY_UNSUPPORTED",
                f"规则“{name}”包含当前不支持的能力：{unsupported[2]}",
                details={"rule": name, "node": node, "operator": operator, "capability": unsupported[0], "matched": unsupported[1]},
            )
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
        if operator in {"MATERIAL_EXISTS", "FILE_QUALITY_CHECK"} and not material_scope:
            raise BusinessError("MANIFEST_RULE_MATERIAL_REQUIRED", "材料规则必须声明适用材料", details={"rule": name})
        source_node: str | None = None
        source_rule_key: str | None = None
        source_rule_keys: list[str] = []
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
            if not declared_keys:
                raise BusinessError("MANIFEST_RISK_SOURCE_REQUIRED", "风险规则必须声明来源规则", details={"rule": name})
            source_node = _risk_source_node(name, item.get("source_node"))
            source_rule_key = declared_keys[0]
            source_rule_keys = declared_keys
        comparison_operators = {"FIELD_EQUAL", "FIELD_NOT_EQUAL", "DATE_COMPARE", "DECIMAL_COMPARE"}
        if operator in comparison_operators:
            # 加总比较（DECIMAL_SUM_COMPARE）的右侧是多个加数，输入可以多于两个；
            # 其余比较算子仍是严格的一对一。按算子区分，不要用统一上限拒绝跨文件多字段加总。
            maximum = None if operator_uses_addends(operator) else 2
            if len(inputs) < 2 or (maximum is not None and len(inputs) > maximum):
                raise BusinessError(
                    "MANIFEST_RULE_INPUT_COUNT_INVALID",
                    "金额加总比较必须包含左侧字段和至少一个加数" if operator_uses_addends(operator) else "比较规则必须恰好包含两个输入字段",
                    details={"rule": name, "input_count": len(inputs)},
                )
            if node == "INTERNAL_VALIDATE" and len({entry["material_key"] for entry in inputs}) != 1:
                raise BusinessError(
                    "MANIFEST_INTERNAL_MATERIAL_MISMATCH",
                    "文件内部校验的左右字段必须属于同一份材料",
                    details={"rule": name},
                )
            input_types = [field_types[input_item["field_key"]] for input_item in inputs]
            if operator == "DATE_COMPARE" and any(value_type != "DATE" for value_type in input_types):
                raise BusinessError("MANIFEST_RULE_TYPE_INVALID", "日期比较规则只能引用 DATE 字段", details={"rule": name})
            if operator == "DECIMAL_COMPARE" and any(value_type != "DECIMAL" for value_type in input_types):
                raise BusinessError("MANIFEST_RULE_TYPE_INVALID", "金额比较规则只能引用 DECIMAL 字段", details={"rule": name})
            if operator in {"FIELD_EQUAL", "FIELD_NOT_EQUAL"} and input_types[0] != input_types[1]:
                raise BusinessError("MANIFEST_RULE_TYPE_INVALID", "字段比较规则的两个字段类型必须一致", details={"rule": name})
            unit_mismatch = _comparison_unit_mismatch(
                operator,
                [{"unit": field_units.get(input_item["field_key"])} for input_item in inputs],
            )
            if unit_mismatch:
                raise BusinessError(
                    "MANIFEST_RULE_UNIT_MISMATCH",
                    f"规则“{name}”不能发布：{unit_mismatch}",
                    details={"rule": name},
                )
        if operator == "LLM_VALIDATE" and not inputs:
            raise BusinessError("MANIFEST_RULE_INPUT_COUNT_INVALID", "智能校验规则必须声明输入字段", details={"rule": name})
        if node == "INTERNAL_VALIDATE" and operator == "LLM_VALIDATE" and len(inputs) > 1:
            raise BusinessError(
                "MANIFEST_RULE_INPUT_COUNT_INVALID",
                "文件内部校验的智能判断最多只能引用一个字段",
                details={"rule": name, "input_count": len(inputs)},
            )
        if node == "CROSS_VALIDATE" and len(inputs) < 2:
            raise BusinessError(
                "MANIFEST_RULE_INPUT_COUNT_INVALID",
                "文件交叉校验必须引用左右两侧的字段",
                details={"rule": name, "input_count": len(inputs)},
            )
        applicability = applicability_view({**item, "applicability": item.get("applicability")})
        if applicability["kind"] not in APPLICABILITY_LABELS:
            raise BusinessError(
                "MANIFEST_APPLICABILITY_INVALID",
                "规则的适用条件不受支持",
                details={"rule": name, "kind": applicability["kind"]},
            )
        rule_keys.add(key)
        rule_nodes[key] = node
        compiled_rules.append(
            {
                "rule_key": key,
                "name": name,
                "node": node,
                "operator": operator,
                "enabled": bool(item.get("enabled", True)),
                "inputs": inputs,
                "outputs": [],
                "materials": material_scope,
                "source_rule_key": source_rule_key,
                "source_rule_keys": list(source_rule_keys),
                "source_node": source_node,
                # 页面表单路径的来源节点由用户显式选择，单独一条来源规则：
                # 实际节点集合与声明一致，由下面的统一校验核对。
                "source_nodes": [],
                "parameters": parameters,
                # 适用条件随规则进入已发布配置：下面的统一校验会补齐 material_keys，
                # 但不能在这里丢掉用户选择的 kind（否则会静默变成“始终适用”）。
                "applicability": {"kind": applicability["kind"]},
                "requirement": str(item.get("requirement") or ""),
                # 业务配置事实随规则进入已发布配置，与导入编译保持一致：
                # 检查等级、合格要求原文、风险等级原文、风险成立条件与业务配置状态。
                # 缺了它们，页面保存过的规则在导出、报告与风险结果里就只剩执行参数，
                # 「这条检查是什么等级」「风险的成立条件是什么」都无从说明。
                "config_status": item.get("config_status"),
                "check_operator": item.get("check_operator"),
                "check_level": item.get("check_level"),
                "risk_level_value": item.get("risk_level_value"),
                "risk_condition": item.get("risk_condition"),
                "quality_supported": item.get("quality_supported"),
                "source_ref": item.get("source_ref"),
                "source": dict(FORM_SOURCE),
            }
        )

    # 风险规则的来源规则必须真实存在、已启用，并位于来源节点允许的审核节点上。
    # 来源可能有多条（任一 / 全部）：全部来源都要校验，只查第一条会让其余来源
    # 指向已删除或已停用的规则却仍然发布出去。
    node_positions = {code: index for index, (code, _) in enumerate(NODE_DEFINITIONS)}
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
            actual_node = rule_nodes[source_rule_key]
            if node_positions[rule["node"]] <= node_positions[actual_node]:
                raise BusinessError("MANIFEST_NODE_ORDER_INVALID", "风险规则必须位于来源规则之后", details={"rule": rule["name"]})
            if actual_node not in SOURCE_NODE_RULE_NODES.get(rule["source_node"] or "", frozenset()):
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
            actual_nodes.add(actual_node)
            if source_rule_key not in resolved_keys:
                resolved_keys.append(source_rule_key)
        rule["source_rule_key"] = resolved_keys[0]
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

    # 字段抽取规则：每个字段一条，与导入编译使用同一确定性派生规则。
    for field in compiled_fields:
        instruction = str(field["instruction"] or "").strip()
        if not instruction:
            raise BusinessError(
                "MANIFEST_FIELD_INSTRUCTION_REQUIRED",
                f"字段“{field['name']}”缺少抽取要求，不能发布",
                details={"field_name": field["name"]},
            )
        extraction_parameters = {"instruction": instruction}
        _validate_rule_parameters(f"抽取{field['name']}", "LLM_EXTRACT", extraction_parameters)
        compiled_rules.append(
            {
                "rule_key": stable_key("rule", namespace, f"extract:{field['field_key']}"),
                "name": f"抽取{field['name']}",
                "node": "FIELD_EXTRACTION",
                "operator": "LLM_EXTRACT",
                "enabled": True,
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
    explicit_report_groups = any(section.get("report_code") for section in sections)
    for section in sections:
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
            "section_name": title,
            "content_instruction": str(section.get("content_instruction") or ""),
            "field_keys": field_keys,
            "source": dict(FORM_SOURCE),
        }
        # 节点版的报告分组属性：报告编号、报告名称、材料范围、输出格式、
        # 章节顺序、内容类型、结果排序与配置状态。有报告编号时按分组渲染，
        # 没有时保持旧的「附加展示章节」语义。字段名与导入编译保持一致。
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
                    "config_status": section.get("config_status"),
                }
            )
        else:
            entry["section_order"] = section.get("section_order")
            entry["content_type"] = section.get("content_type")
        report_template.append(entry)

    if not any(item["node"] == "REPORT_OUTPUT" and item["operator"] == "REPORT_RENDER" for item in compiled_rules):
        compiled_rules.append(
            {
                "rule_key": stable_key("rule", namespace, "report:render"),
                "name": "生成审核报告",
                "node": "REPORT_OUTPUT",
                "operator": "REPORT_RENDER",
                "enabled": True,
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

    _ensure_unique_rules(compiled_rules)
    _ensure_manifest_node_coverage(compiled_rules, report_template)

    manifest = {
        "manifest_version": "1.0",
        # 页面表单配置没有 Excel 工作簿；导出时会按该格式版本写出业务列，
        # 因此这里记录可导出的目标格式版本，并显式标注配置来源。
        "profile_version": profile_version,
        "source_kind": "FORM",
        "scene_code": scene_code,
        "scene_name": scene_name,
        "materials": sorted(compiled_materials, key=lambda item: item["material_key"]),
        "fields": sorted(compiled_fields, key=lambda item: item["field_key"]),
        "rules": sorted(compiled_rules, key=lambda item: item["rule_key"]),
        "nodes": [
            {"node_code": code, "name": name, "sequence_no": index, "enabled": True}
            for index, (code, name) in enumerate(NODE_DEFINITIONS, start=1)
        ],
        "report_template": report_template,
        # 报告分组：节点版的报告编号、报告名称、材料范围、输出格式、结果排序与章节顺序
        # 随章节一起保留；页面表单配置若来自节点版导入/修订，同样按编号分组，
        # 不会因为经过一次页面保存就退回成旧的附加展示章节。
        "report_groups": _build_report_groups(report_template, explicit_report_groups),
        "unconfigured_check_nodes": unconfigured_check_nodes(compiled_rules),
        "capability_gaps": capability_gaps(compiled_rules),
        # 与导入编译同一口径：能力覆盖结果随版本一起冻结。
        "capability_coverage": capabilities.coverage_of(compiled_rules),
        "capability_version": capabilities.CAPABILITY_VERSION,
        "compiled_at": compiled_at.isoformat(),
        "source_workbook_id": None,
    }
    canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    manifest["manifest_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return manifest
