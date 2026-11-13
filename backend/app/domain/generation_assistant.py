"""助手只输出有限的文档编辑操作，不能执行代码、路径或任意 JSON Patch。"""

from typing import Annotated, Literal

from pydantic import Field

from app.common.exceptions import BusinessError
from app.domain.rulebook_generation import (
    GenerationColumn,
    GenerationDocument,
    GenerationEntry,
    GenerationIssue,
    GenerationModel,
    GenerationSheet,
    business_code_column,
    normalize_business_codes,
)

ASSISTANT_PROMPT_VERSION = "generation_assistant_v12"


class SetDocument(GenerationModel):
    op: Literal["set_document"]
    title: str | None = None
    summary: str | None = None


class AddSheet(GenerationModel):
    op: Literal["add_sheet"]
    sheet: GenerationSheet


class UpdateSheet(GenerationModel):
    op: Literal["update_sheet"]
    sheet_id: str
    name: str | None = None
    description: str | None = None
    columns: list[GenerationColumn] | None = None
    business_code_scope: Literal["DOCUMENT", "SHEET"] | None = None


class RemoveSheet(GenerationModel):
    op: Literal["remove_sheet"]
    sheet_id: str


class UpsertRow(GenerationModel):
    op: Literal["upsert_row"]
    sheet_id: str
    row: GenerationEntry


class UpdateCells(GenerationModel):
    op: Literal["update_cells"]
    row_id: str
    cells: dict[str, str] = Field(description="仅列要修改的既有 column_id 与新文字，其余栏目及条目元数据保持原样。")


class UpdateRowMetadata(GenerationModel):
    op: Literal["update_row_metadata"]
    row_id: str
    business_code: str | None = Field(default=None, max_length=80, description="已有自身编号沿用，仅缺号时补充；无独立编号的辅助记录留空，纠正误用批次或引用号时可清空，不改其原业务单元格。")
    entry_kind: Literal["RULE", "MATERIAL", "REFERENCE", "OTHER"] | None = None
    related_row_ids: list[str] | None = Field(default=None, description="该条目的完整新关联列表；未提供则保留，空数组则清空。")
    source_note: str | None = Field(default=None, description="当前编制说明，可更新过时描述；原始出处位置保持不变。")


class MoveRow(GenerationModel):
    op: Literal["move_row"]
    row_id: str
    sheet_id: str
    cells: dict[str, str]


class RemoveRow(GenerationModel):
    op: Literal["remove_row"]
    row_id: str


class Reorder(GenerationModel):
    op: Literal["reorder"]
    sheet_id: str | None = None
    ids: list[str]


class UpsertIssue(GenerationModel):
    op: Literal["upsert_issue"]
    issue: GenerationIssue


class RemoveIssue(GenerationModel):
    op: Literal["remove_issue"]
    issue_id: str


Edit = Annotated[
    SetDocument | AddSheet | UpdateSheet | RemoveSheet | UpsertRow | UpdateCells | UpdateRowMetadata | MoveRow | RemoveRow | Reorder | UpsertIssue | RemoveIssue,
    Field(discriminator="op"),
]


class AssistantReply(GenerationModel):
    kind: Literal["EDIT", "EXPLAIN", "CLARIFY"]
    message: str = Field(min_length=1, max_length=1600)
    edits: list[Edit] = Field(default_factory=list)


class ReviewTextRepair(GenerationModel):
    target: Literal["document_summary", "sheet_description", "issue_detail", "issue_resolution"]
    target_id: str = Field(min_length=1)
    text: str


class AssistantReview(GenerationModel):
    problems: list[str]
    text_repairs: list[ReviewTextRepair] = Field(default_factory=list)


def document_changes(before: GenerationDocument, after: GenerationDocument) -> list[dict]:
    """按对象身份比较，增删/排序不重发整表，也不把移位误当正文变化。"""
    before = normalize_business_codes(before)
    after = normalize_business_codes(after)
    changes = []
    identities = {"sheets": "sheet_id", "rows": "row_id", "columns": "column_id",
                  "issues": "issue_id", "requirements": "requirement_id"}

    def compare(old, new, path):
        if old == new:
            return
        if isinstance(old, dict) and isinstance(new, dict) and old.keys() == new.keys():
            for key in old:
                compare(old[key], new[key], [*path, key])
        elif isinstance(old, list) and isinstance(new, list):
            identity = identities.get(path[-1]) if path and isinstance(path[-1], str) else None
            if identity:
                old_items = {item[identity]: item for item in old}
                new_items = {item[identity]: item for item in new}
                for key, item in old_items.items():
                    location = [*path, {identity: key}]
                    if key in new_items:
                        compare(item, new_items[key], location)
                    else:
                        changes.append({"path": location, "before": item, "after": None})
                for key, item in new_items.items():
                    if key not in old_items:
                        changes.append({"path": [*path, {identity: key}], "before": None, "after": item})
                if list(old_items) != list(new_items):
                    changes.append({"path": [*path, "$order"], "before": list(old_items), "after": list(new_items)})
            else:
                changes.append({"path": path, "before": old, "after": new})
        else:
            changes.append({"path": path, "before": old, "after": new})

    compare(before.model_dump(mode="json"), after.model_dump(mode="json"), [])
    return changes


def apply_review_text_repairs(
    previous: GenerationDocument, candidate: GenerationDocument, repairs: list[ReviewTextRepair],
) -> GenerationDocument:
    """复核只可补修受本次修改影响的说明文字；正文、状态、范围仍由编辑器处理。"""
    previous = normalize_business_codes(previous)
    candidate = normalize_business_codes(candidate)
    old_rows = {row.row_id: row for sheet in previous.sheets for row in sheet.rows}
    new_rows = {row.row_id: row for sheet in candidate.sheets for row in sheet.rows}
    old_positions = {row.row_id: (sheet.sheet_id, sheet_index, row_index)
                     for sheet_index, sheet in enumerate(previous.sheets) for row_index, row in enumerate(sheet.rows)}
    new_positions = {row.row_id: (sheet.sheet_id, sheet_index, row_index)
                     for sheet_index, sheet in enumerate(candidate.sheets) for row_index, row in enumerate(sheet.rows)}
    changed_rows = {key for key in old_rows.keys() | new_rows.keys()
                    if old_rows.get(key) != new_rows.get(key) or old_positions.get(key) != new_positions.get(key)}
    old_sheets = {sheet.sheet_id: sheet for sheet in previous.sheets}
    old_issues = {issue.issue_id: issue for issue in previous.issues}
    operations = []
    seen = set()
    for repair in repairs:
        key = (repair.target, repair.target_id)
        if key in seen:
            raise BusinessError("ASSISTANT_INVALID_REPAIR", "复核修补重复指定了同一处说明", status_code=502)
        seen.add(key)
        if repair.target == "document_summary":
            if repair.target_id != "document" or candidate == previous:
                raise BusinessError("ASSISTANT_INVALID_REPAIR", "复核修补引用了未受本次修改影响的文档", status_code=502)
            operations.append(SetDocument(op="set_document", summary=repair.text))
        elif repair.target == "sheet_description":
            sheet = next((sheet for sheet in candidate.sheets if sheet.sheet_id == repair.target_id), None)
            if sheet is None or sheet == old_sheets.get(repair.target_id):
                raise BusinessError("ASSISTANT_INVALID_REPAIR", "复核修补引用了未受本次修改影响的工作表", status_code=502)
            operations.append(UpdateSheet(op="update_sheet", sheet_id=sheet.sheet_id, description=repair.text))
        else:
            issue = next((issue for issue in candidate.issues if issue.issue_id == repair.target_id), None)
            old_issue = old_issues.get(repair.target_id)
            affected = set(issue.affected_row_ids if issue else []) | set(old_issue.affected_row_ids if old_issue else [])
            if issue is None or (issue == old_issue and not affected & changed_rows):
                raise BusinessError("ASSISTANT_INVALID_REPAIR", "复核修补引用了未受本次修改影响的事项", status_code=502)
            updated = issue.model_copy(deep=True)
            if repair.target == "issue_resolution" and issue.status == "RESOLVED" and not repair.text.strip():
                raise BusinessError("ASSISTANT_INVALID_REPAIR", "复核不能清空已明确事项的依据", status_code=502)
            setattr(updated, "detail" if repair.target == "issue_detail" else "resolution", repair.text)
            # 同一事项可同时修正文案和答复，后一个操作须承接前一个字段的修补。
            for operation in operations:
                if isinstance(operation, UpsertIssue) and operation.issue.issue_id == updated.issue_id:
                    setattr(operation.issue, "detail" if repair.target == "issue_detail" else "resolution", repair.text)
                    break
            else:
                operations.append(UpsertIssue(op="upsert_issue", issue=updated))
    return apply_edits(candidate, AssistantReply(kind="EDIT", message="同步受影响的说明", edits=operations))


def apply_edits(previous: GenerationDocument, reply: AssistantReply) -> GenerationDocument:
    if reply.kind != "EDIT":
        if reply.edits:
            raise BusinessError("ASSISTANT_INVALID_EDIT", "解释或提问不能同时修改文档", status_code=502)
        return previous.model_copy(deep=True)
    result = previous.model_copy(deep=True)
    source_notes = {}

    def invalid(text: str):
        raise BusinessError("ASSISTANT_INVALID_EDIT", text, status_code=502)

    def sheet_for(identifier: str):
        found = next((sheet for sheet in result.sheets if sheet.sheet_id == identifier), None)
        if found is None:
            invalid("修改引用了不存在的工作表")
        return found

    def row_for(identifier: str):
        for sheet in result.sheets:
            for row in sheet.rows:
                if row.row_id == identifier:
                    return sheet, row
        invalid("修改引用了不存在的条目")

    for edit in reply.edits:
        if isinstance(edit, SetDocument):
            if edit.title is not None:
                result.title = edit.title
            if edit.summary is not None:
                result.summary = edit.summary
        elif isinstance(edit, AddSheet):
            if any(sheet.sheet_id == edit.sheet.sheet_id for sheet in result.sheets):
                invalid("新增工作表使用了已有身份")
            result.sheets.append(edit.sheet.model_copy(deep=True))
        elif isinstance(edit, UpdateSheet):
            target = sheet_for(edit.sheet_id)
            for field in ("name", "description", "columns", "business_code_scope"):
                value = getattr(edit, field)
                if value is not None:
                    setattr(target, field, value)
        elif isinstance(edit, RemoveSheet):
            target = sheet_for(edit.sheet_id)
            if target.rows:
                invalid("删除工作表前必须明确处理其中条目，不能整表丢弃正文")
            result.sheets.remove(target)
        elif isinstance(edit, UpsertRow):
            target = sheet_for(edit.sheet_id)
            locations = [(sheet, row) for sheet in result.sheets for row in sheet.rows if row.row_id == edit.row.row_id]
            if locations:
                owner, old = locations[0]
                if owner.sheet_id != target.sheet_id:
                    invalid("移动条目应使用 move_row 操作")
                target.rows[target.rows.index(old)] = edit.row.model_copy(deep=True)
            else:
                target.rows.append(edit.row.model_copy(deep=True))
            if "note" in edit.row.source.model_fields_set:
                source_notes[edit.row.row_id] = edit.row.source.note
        elif isinstance(edit, UpdateCells):
            owner, row = row_for(edit.row_id)
            if not set(edit.cells) <= {column.column_id for column in owner.columns}:
                invalid("单元格修改引用了不存在的栏目")
            row.cells.update(edit.cells)
        elif isinstance(edit, UpdateRowMetadata):
            _, row = row_for(edit.row_id)
            if edit.business_code is not None:
                row.business_code = edit.business_code
            if edit.entry_kind is not None:
                row.entry_kind = edit.entry_kind
            if edit.related_row_ids is not None:
                row.related_row_ids = list(edit.related_row_ids)
            if edit.source_note is not None:
                source_notes[edit.row_id] = edit.source_note
        elif isinstance(edit, MoveRow):
            owner, row = row_for(edit.row_id)
            target = sheet_for(edit.sheet_id)
            owner.rows.remove(row)
            row.cells = edit.cells
            target.rows.append(row)
        elif isinstance(edit, RemoveRow):
            owner, row = row_for(edit.row_id)
            owner.rows.remove(row)
            for sheet in result.sheets:
                for other in sheet.rows:
                    other.related_row_ids = [ref for ref in other.related_row_ids if ref != edit.row_id]
            for issue in result.issues:
                issue.affected_row_ids = [ref for ref in issue.affected_row_ids if ref != edit.row_id]
        elif isinstance(edit, Reorder):
            items = sheet_for(edit.sheet_id).rows if edit.sheet_id else result.sheets
            key = "row_id" if edit.sheet_id else "sheet_id"
            indexed = {getattr(item, key): item for item in items}
            if len(edit.ids) != len(indexed) or set(edit.ids) != set(indexed):
                invalid("排序不能遗漏、重复或增加条目")
            items[:] = [indexed[ref] for ref in edit.ids]
        elif isinstance(edit, UpsertIssue):
            index = next((index for index, item in enumerate(result.issues) if item.issue_id == edit.issue.issue_id), None)
            if index is None:
                result.issues.append(edit.issue.model_copy(deep=True))
            else:
                result.issues[index] = edit.issue.model_copy(deep=True)
        elif isinstance(edit, RemoveIssue):
            if not any(item.issue_id == edit.issue_id for item in result.issues):
                invalid("修改引用了不存在的事项")
            result.issues = [item for item in result.issues if item.issue_id != edit.issue_id]

    result = normalize_business_codes(result, previous)
    # 业务正文保留原始出处；事项的范围和依据可随修复调整，并继续接受来源校验与内容复核。
    baseline = normalize_business_codes(previous)
    old_items = {item.row_id: item for sheet in baseline.sheets for item in sheet.rows}
    for sheet in result.sheets:
        old_sheet = next((item for item in previous.sheets if item.sheet_id == sheet.sheet_id), None)
        if old_sheet:
            sheet.source = old_sheet.source.model_copy(deep=True)
        identity_column = business_code_column(sheet)
        for row in sheet.rows:
            old = old_items.get(row.row_id)
            if old is None and not row.business_code and (row.entry_kind in {"RULE", "MATERIAL"} or identity_column):
                invalid("新增条目缺少业务编号，请沿用原编号，或由模型按现有编号规律和业务内容补充")
            if old:
                row.source = old.source.model_copy(deep=True)
            if row.row_id in source_notes:
                row.source.note = source_notes[row.row_id]
            if old:
                if (row.model_dump(exclude={"source"}) != old.model_dump(exclude={"source"})
                        or row.source.note != old.source.note):
                    if "AI按用户要求整理" not in row.source.note:
                        row.source.note = (row.source.note + "；AI按用户要求整理").strip("；")
    for issue in result.issues:
        old_issue = next((item for item in previous.issues if item.issue_id == issue.issue_id), None)
        if old_issue:
            if issue != old_issue:
                if "AI按用户要求整理" not in issue.source.note:
                    issue.source.note = (issue.source.note + "；AI按用户要求整理").strip("；")
    return result
