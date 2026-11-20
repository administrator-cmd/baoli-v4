"""面向业务人员的规则库编制文档；不承担系统执行配置的解析。"""

import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

GENERATION_DOCUMENT_SCHEMA_VERSION = "rulebook_generation_v2"
GENERATION_PROMPT_VERSION = "rulebook_generation_prompt_v12"
GENERATION_EXPORT_FORMAT_VERSION = "rulebook_generation_xlsx_v11"
GENERATION_WORKBOOK_PROFILE = "rulebook_generation_v2"
# UTF-8 每字符最多 4 字节，保证两个编制输入各自小于 MySQL TEXT 的字节上限。
GENERATION_INPUT_MAX_LENGTH = 16000

GENERATION_STATUS_NOT_GENERATED = "NOT_GENERATED"
GENERATION_STATUS_PENDING = "PENDING"
GENERATION_STATUS_RUNNING = "RUNNING"
GENERATION_STATUS_EDITABLE = "EDITABLE"
GENERATION_STATUS_NO_DEFINITIONS = "NO_DEFINITIONS"
GENERATION_STATUS_FAILED = "FAILED"
GENERATION_STATUS_INTERRUPTED = "INTERRUPTED"
GENERATION_STATUS_UNKNOWN = "UNKNOWN"

GenerationStatusFilter = Literal[
    "NOT_GENERATED", "PENDING", "RUNNING", "EDITABLE", "NO_DEFINITIONS",
    "FAILED", "INTERRUPTED", "UNKNOWN",
]
SourceKind = Literal["ORIGINAL_EXCEL", "USER_INPUT", "DERIVED"]
IssueStatus = Literal["OPEN", "RESOLVED"]


class GenerationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GenerationRowReference(GenerationModel):
    sheet_name: str
    row_no: int = Field(ge=1)


class GenerationOrigin(GenerationRowReference):
    related_rows: list[GenerationRowReference] = Field(
        default_factory=list,
        description="关联来源行，唯一位置为 source.origin.related_rows，不得放在 source 层。",
    )
    cell_range: str | None = None


class GenerationSource(GenerationModel):
    kind: SourceKind
    origin: GenerationOrigin | None = Field(
        default=None, description="主来源和关联来源的容器，related_rows 必须位于此对象内。",
    )
    requirement_ids: list[str] = Field(default_factory=list)
    note: str = ""


class GenerationRequirement(GenerationModel):
    requirement_id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1)
    category: str = Field(min_length=1, description="按原文内容命名的业务分类，不使用系统执行类型。")
    source: GenerationSource


class GenerationUnclearItem(GenerationModel):
    unclear_id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1)
    requirement_ids: list[str] = Field(default_factory=list)
    source: GenerationSource


class GenerationRequirementInventory(GenerationModel):
    schema_version: Literal["rulebook_generation_v2"]
    requirements: list[GenerationRequirement]
    unclear_items: list[GenerationUnclearItem] = Field(default_factory=list)


class GenerationColumn(GenerationModel):
    column_id: str = Field(min_length=1, max_length=80, description="仅供编辑器定位，不展示或导出。")
    name: str = Field(min_length=1, description="给业务人员阅读的栏目名称。")
    description: str = ""
    is_business_code: bool = Field(default=False, description="本条内容自身的业务编号栏，由模型结合表头和内容识别；不标记关联编号栏。每表最多一栏。")
    code_role: Literal["IDENTITY", "REFERENCE", "GROUP"] | None = Field(
        default=None,
        description="编号语义：IDENTITY 是本行自身编号，REFERENCE 是其他对象的引用，GROUP 是可重复的批次或分组号；非编号栏留空。优先于旧标记和表头推断。",
    )


class GenerationEntry(GenerationModel):
    row_id: str = Field(min_length=1, max_length=80, description="仅供编辑器定位，不展示或导出。")
    cells: dict[str, str] = Field(description="栏目 column_id 到业务正文的映射；正文使用人能直接阅读的文字。")
    source: GenerationSource
    business_code: str = Field(default="", max_length=80, description="本条内容自身的业务编号，与自身编号栏一致；规则、材料确实缺号时补充。无独立编号的说明或历史记录留空，不填批次号或被引用对象编号。")
    entry_kind: Literal["RULE", "MATERIAL", "REFERENCE", "OTHER"] = "OTHER"
    related_row_ids: list[str] = Field(default_factory=list, description="原文有明确关系的其他条目；不能猜测关联。")


class GenerationSheet(GenerationModel):
    sheet_id: str = Field(min_length=1, max_length=80, description="仅供编辑器定位，不展示或导出。")
    name: str = Field(min_length=1, max_length=31, description="符合 Excel 工作表命名规则的业务名称。")
    description: str = ""
    columns: list[GenerationColumn] = Field(min_length=1)
    rows: list[GenerationEntry]
    source: GenerationSource
    business_code_scope: Literal["DOCUMENT", "SHEET"] = Field(
        default="DOCUMENT", description="自身编号的唯一范围，默认整份文档；仅原文明示各表独立编号时选 SHEET，不为绕过真实重复而缩小范围。",
    )


class GenerationIssue(GenerationModel):
    source_unclear_ids: list[str] = Field(default_factory=list, description="本事项承接的首阶段疑点编号，可多对一，仅内部溯源。")
    issue_id: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1)
    detail: str = ""
    affected_row_ids: list[str] = Field(default_factory=list)
    status: IssueStatus = "OPEN"
    resolution: str = ""
    source: GenerationSource


class GenerationDocument(GenerationModel):
    schema_version: Literal["rulebook_generation_v2"]
    title: str = Field(min_length=1)
    summary: str = ""
    requirements: list[GenerationRequirement]
    sheets: list[GenerationSheet]
    issues: list[GenerationIssue]
    code_counters: dict[str, int] = Field(default_factory=dict, description="程序记录已有编号的流水上界，供模型补号时避免重用；模型不填写，不按固定前缀分配编号。")


class ReviewQuestion(GenerationIssue):
    status: Literal["OPEN"] = "OPEN"
    resolution: Literal[""] = ""


class GenerationContentReview(GenerationModel):
    problems: list[str]
    new_questions: list[ReviewQuestion] = Field(default_factory=list, description="仅提交尚无对应事项、正文未擅自定论的真实业务疑问。")


def is_business_code_column(name: str) -> bool:
    """识别条目自己的编号栏目，不把关联编号列当成该条目的身份。"""
    normalized = re.sub(r"\s+", "", name).casefold()
    return normalized in {
        "编号", "原编号", "原始编号", "业务编号", "编制编号", "id",
        *{prefix + suffix for prefix in ("规则", "材料", "条目", "映射", "字典", "风险", "报告", "字段", "原始", "原",
                                        "材料规则", "质量规则", "抽取规则", "计算规则", "核验规则", "校验规则", "风险规则")
          for suffix in ("编号", "id")},
    }


def is_identity_column(column: GenerationColumn) -> bool:
    """显式编号语义优先，未标注的旧文档仍沿用原识别规则。"""
    if column.code_role is not None:
        return column.code_role == "IDENTITY"
    return column.is_business_code or is_business_code_column(column.name)


def business_code_column(sheet: GenerationSheet) -> str | None:
    """优先使用原业务编号栏，避免历史额外编制编号覆盖原编号。"""
    columns = [column for column in sheet.columns if is_identity_column(column)]
    columns.sort(key=lambda column: (not (column.code_role == "IDENTITY" or column.is_business_code), re.sub(r"\s+", "", column.name) == "编制编号"))
    return columns[0].column_id if columns else None


def entry_business_code(sheet: GenerationSheet, row: GenerationEntry) -> str:
    column = business_code_column(sheet)
    return (row.cells.get(column, "").strip() if column else "") or row.business_code.strip()


def normalize_business_codes(document: GenerationDocument, previous: GenerationDocument | None = None) -> GenerationDocument:
    """原编号优先；同步模型补充的编号，不按固定前缀另造一套编号。"""
    result = document.model_copy(deep=True)
    old = {row.row_id: entry_business_code(sheet, row) for sheet in previous.sheets for row in sheet.rows} if previous else {}
    counters = dict(previous.code_counters if previous else result.code_counters)
    for sheet in result.sheets:
        column = business_code_column(sheet)
        for row in sheet.rows:
            # 明确标为引用/分组后，不能从旧元数据重新带回被排除的编号。
            excluded = {row.cells.get(item.column_id, "").strip().casefold() for item in sheet.columns
                        if item.code_role in {"REFERENCE", "GROUP"}}
            code = entry_business_code(sheet, row)
            old_code = old.get(row.row_id, "")
            row.business_code = code or (old_code if column or old_code.casefold() not in excluded else "")
            if column and column in row.cells and not row.cells[column].strip():
                row.cells[column] = row.business_code
            sequence = re.fullmatch(r"(.+?)(\d+)", row.business_code)
            if sequence:
                prefix = sequence[1].casefold()
                counters[prefix] = max(counters.get(prefix, 0), int(sequence[2]))
    result.code_counters = counters
    return result


def document_entry_count(document: GenerationDocument) -> int:
    """业务条目数，不统计编辑器身份或待明确事项。"""
    return sum(len(sheet.rows) for sheet in document.sheets)


def document_has_business_content(document: GenerationDocument) -> bool:
    return bool(document.sheets or document.issues)


def _add(problems: list[dict[str, Any]], owner: str, kind: str, reference: Any = None) -> None:
    item: dict[str, Any] = {"owner": owner, "kind": kind}
    if reference is not None:
        item["reference"] = reference
    problems.append(item)


def summarize_generation_problems(problems: Sequence[Mapping[str, Any]], *, limit: int = 5) -> str:
    parts = []
    for problem in problems[:limit]:
        text = f"{problem['owner']}（{problem['kind']}"
        if "reference" in problem:
            text += f"：{problem['reference']}"
        parts.append(text + "）")
    remaining = len(problems) - limit
    return "；".join(parts) + (f"；另有 {remaining} 项" if remaining > 0 else "")


def _check_unique(values: Sequence[str], owner: str, kind: str, problems: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            _add(problems, owner, kind, value)
        seen.add(value)


def _check_source(
    source: GenerationSource, owner: str, source_rows: set[tuple[str, int]],
    requirement_ids: set[str], problems: list[dict[str, Any]], *, allow_unlinked_review: bool = False,
) -> None:
    if source.kind == "ORIGINAL_EXCEL" and source.origin is None:
        _add(problems, owner, "source.origin_required")
    if source.origin:
        origin = source.origin
        for row in [origin, *origin.related_rows]:
            if (row.sheet_name, row.row_no) not in source_rows:
                _add(problems, owner, "source.origin_missing", f"{row.sheet_name}!{row.row_no}")
        if origin.cell_range and not re.fullmatch(r"[A-Z]+[1-9][0-9]*(?::[A-Z]+[1-9][0-9]*)?", origin.cell_range):
            _add(problems, owner, "source.cell_range_invalid", origin.cell_range)
    for reference in source.requirement_ids:
        if reference not in requirement_ids:
            _add(problems, owner, "source.requirement_missing", reference)
    if source.kind == "DERIVED" and not source.requirement_ids and not allow_unlinked_review:
        _add(problems, owner, "source.derived_without_requirement")


def source_record_coverage_problems(items, records: list[dict[str, Any]], *, require_code: bool = False) -> list[dict[str, Any]]:
    """核对原文明确带编号的记录有承接；不据编号推断内容职责或业务正确性。"""
    referenced: dict[tuple[str, int], list[str]] = {}
    for item in items:
        origin = item.source.origin
        if origin:
            content = "\n".join(item.cells.values()) if isinstance(item, GenerationEntry) else item.text
            for ref in [origin, *origin.related_rows]:
                referenced.setdefault((ref.sheet_name, ref.row_no), []).append(content)
    problems = []
    for record in records:
        key = (record["sheet_name"], record["row_no"])
        contents = referenced.get(key, [])
        if not contents:
            _add(problems, f"{key[0]}!{key[1]}", "coverage.source_record_missing", record["codes"])
            continue
        if require_code:
            for code in record["codes"]:
                pattern = re.compile(r"(?<![A-Za-z0-9_.-])" + re.escape(code) + r"(?![A-Za-z0-9_.-])")
                if not any(pattern.search(content) for content in contents):
                    _add(problems, f"{key[0]}!{key[1]}", "coverage.original_code_missing", code)
    return problems


def collect_requirement_inventory_problems(
    inventory: GenerationRequirementInventory, source_rows: set[tuple[str, int]],
) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    identifiers = [item.requirement_id for item in inventory.requirements]
    _check_unique(identifiers, "业务要求", "requirement.duplicate", problems)
    _check_unique([item.unclear_id for item in inventory.unclear_items], "待明确事项", "unclear.duplicate", problems)
    known = set(identifiers)
    for item in inventory.requirements:
        _check_source(item.source, item.requirement_id, source_rows, known, problems)
        if item.source.kind == "DERIVED":
            _add(problems, item.requirement_id, "requirement.must_have_original_basis")
    for item in inventory.unclear_items:
        _check_source(item.source, item.unclear_id, source_rows, known, problems)
        for reference in item.requirement_ids:
            if reference not in known:
                _add(problems, item.unclear_id, "unclear.requirement_missing", reference)
    return problems


def requirement_identity_problems(
    inventory: GenerationRequirementInventory, document: GenerationDocument,
    *, allow_content_repairs: bool = False,
) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    # 首轮补修可订正目录措辞与补充依据，身份、分类及主来源仍须与第一阶段一致。
    exclude = {"text": True, "source": {"note": True, "origin": {"related_rows": True}}} if allow_content_repairs else None
    first = {item.requirement_id: item.model_dump(mode="json", exclude=exclude) for item in inventory.requirements}
    second = {item.requirement_id: item.model_dump(mode="json", exclude=exclude) for item in document.requirements}
    for reference in first.keys() | second.keys():
        if first.get(reference) != second.get(reference):
            _add(problems, reference, "requirement.changed_or_missing")
    expected = {item.unclear_id for item in inventory.unclear_items}
    covered: set[str] = set()
    for issue in document.issues:
        refs = set(issue.source_unclear_ids)
        if issue.issue_id in expected:  # 兼容独立事项与历史格式
            refs.add(issue.issue_id)
        for ref in sorted(refs - expected):
            _add(problems, issue.issue_id, "unclear.unknown_reference", ref)
        for ref in sorted(refs & covered):
            _add(problems, issue.issue_id, "unclear.already_carried", ref)
        covered.update(refs)
    for item in inventory.unclear_items:
        if item.unclear_id not in covered:
            _add(problems, item.unclear_id, "unclear.not_carried_to_issue")
    return problems


def collect_document_problems(
    document: GenerationDocument, source_rows: set[tuple[str, int]], *, require_coverage: bool = False, require_codes: bool = False,
) -> list[dict[str, Any]]:
    """只核对可保存、可定位和可导出的结构，不检查业务合理性或执行能力。"""
    problems: list[dict[str, Any]] = []
    requirements = {item.requirement_id for item in document.requirements}
    _check_unique([item.requirement_id for item in document.requirements], "业务要求", "requirement.duplicate", problems)
    _check_unique([item.sheet_id for item in document.sheets], "业务表", "sheet.duplicate_id", problems)
    _check_unique([sheet.name.casefold() for sheet in document.sheets], "业务表", "sheet.duplicate_name", problems)
    _check_unique([row.row_id for sheet in document.sheets for row in sheet.rows], "业务条目", "row.duplicate_id", problems)
    _check_unique([issue.issue_id for issue in document.issues], "待明确事项", "issue.duplicate_id", problems)
    rows = {row.row_id for sheet in document.sheets for row in sheet.rows}
    seen_codes: dict[tuple[str, str], str] = {}
    covered: set[str] = set()
    for item in document.requirements:
        _check_source(item.source, item.requirement_id, source_rows, requirements, problems)
    for sheet in document.sheets:
        if re.search(r"[\\/*?:\[\]]", sheet.name) or sheet.name.startswith("'") or sheet.name.endswith("'"):
            _add(problems, sheet.name, "sheet.excel_name_invalid")
        _check_source(sheet.source, sheet.name, source_rows, requirements, problems)
        _check_unique([column.column_id for column in sheet.columns], sheet.name, "column.duplicate_id", problems)
        _check_unique([column.name for column in sheet.columns], sheet.name, "column.duplicate_name", problems)
        if sum(column.code_role == "IDENTITY" or (column.code_role is None and column.is_business_code)
               for column in sheet.columns) > 1:
            _add(problems, sheet.name, "column.multiple_business_code_columns")
        column_ids = {column.column_id for column in sheet.columns}
        identity_column = business_code_column(sheet)
        for row in sheet.rows:
            code = entry_business_code(sheet, row)
            owner = f"{sheet.name} / {row.row_id}"
            if require_codes and (row.entry_kind in {"RULE", "MATERIAL"} or identity_column) and not code:
                _add(problems, row.row_id, "row.business_code_missing")
            if code:
                excluded = {row.cells.get(column.column_id, "").strip().casefold() for column in sheet.columns
                            if column.code_role in {"REFERENCE", "GROUP"}}
                if identity_column is None and code.casefold() in excluded:
                    _add(problems, owner, "row.non_identity_business_code", code)
                scope = sheet.sheet_id if sheet.business_code_scope == "SHEET" else ""
                key = (scope, code.casefold())
                if key in seen_codes:
                    _add(problems, owner, "row.duplicate_business_code", f"{code}（已用于 {seen_codes[key]}）")
                else:
                    seen_codes[key] = owner
            for reference in row.related_row_ids:
                if reference not in rows or reference == row.row_id:
                    _add(problems, row.business_code or row.row_id, "row.related_missing_or_self", reference)
            _check_unique(row.related_row_ids, row.row_id, "row.duplicate_relation", problems)
            _check_source(row.source, f"{sheet.name}中的条目", source_rows, requirements, problems)
            if set(row.cells) != column_ids:
                _add(problems, sheet.name, "row.columns_mismatch", row.row_id)
            covered.update(row.source.requirement_ids)
    for issue in document.issues:
        _check_source(issue.source, issue.title, source_rows, requirements, problems,
                      allow_unlinked_review=issue.source.note == "自动复核疑点，原文位置未确定")
        for reference in issue.affected_row_ids:
            if reference not in rows:
                _add(problems, issue.title, "issue.row_missing", reference)
        if issue.status == "RESOLVED" and not issue.resolution.strip():
            _add(problems, issue.title, "issue.confirmation_required")
        covered.update(issue.source.requirement_ids)
    if require_coverage:
        for reference in requirements - covered:
            _add(problems, reference, "requirement.uncovered")
    return problems


def mark_user_edits(previous: GenerationDocument, current: GenerationDocument) -> GenerationDocument:
    """来源由服务端维护；只改说明或类型不能伪造出处。"""
    previous = normalize_business_codes(previous)
    result = current.model_copy(deep=True)
    before: dict[tuple[str, str], Any] = {}
    for sheet in previous.sheets:
        before[("sheet", sheet.sheet_id)] = sheet
        for row in sheet.rows:
            before[("row", row.row_id)] = row
    for issue in previous.issues:
        before[("issue", issue.issue_id)] = issue
    items = [("sheet", sheet.sheet_id, sheet) for sheet in result.sheets]
    items += [("row", row.row_id, row) for sheet in result.sheets for row in sheet.rows]
    items += [("issue", issue.issue_id, issue) for issue in result.issues]
    for kind, reference, item in items:
        old = before.get((kind, reference))
        if old is None:
            item.source = GenerationSource(kind="USER_INPUT", note="用户新增")
            continue
        old_payload = old.model_dump(mode="json", exclude={"source"})
        new_payload = item.model_dump(mode="json", exclude={"source"})
        if kind == "sheet":
            old_payload.pop("rows")
            new_payload.pop("rows")
        # 原始出处只能沿用，不能通过编辑请求伪造新的 Excel 位置。
        item.source = old.source.model_copy(deep=True)
        if old_payload != new_payload:
            note = old.source.note
            if "用户编辑" not in note:
                note = f"{note}；用户编辑" if note else "用户编辑"
            item.source.kind = "USER_INPUT"
            item.source.note = note
    return result
