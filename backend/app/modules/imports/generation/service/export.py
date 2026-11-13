"""将业务表格导出为供人阅读、确认的规则库 Excel；不导出系统执行配置。"""

import json
import os
import re
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from sqlalchemy.orm import Session

from app.common.exceptions import BusinessError
from app.core.transactions import after_rollback
from app.domain.shared.identity import public_no
from app.domain.generation.document import (
    GENERATION_DOCUMENT_SCHEMA_VERSION,
    GENERATION_EXPORT_FORMAT_VERSION,
    GenerationDocument,
    GenerationEntry,
    GenerationSheet,
    GenerationSource,
    business_code_column,
    document_has_business_content,
    is_identity_column,
    normalize_business_codes,
)
from app.modules.imports.generation import crud
from app.modules.imports.generation.model import RulebookGenerationExport
from app.modules.imports.generation.service.history import GenerationHistoryService
from app.modules.imports.generation.service.query import GenerationQueryService
from app.modules.imports.generation.service.workbook_layout import GenerationWorkbookLayout
from app.modules.imports.job.service.workbook import ImportsJobWorkbookService
from app.services.shared.storage import resolve_storage_path


def _source_text(source: GenerationSource) -> str:
    parts = []
    if source.origin:
        parts.append(f"《{source.origin.sheet_name}》第{source.origin.row_no}行")
        parts.extend(f"《{row.sheet_name}》第{row.row_no}行" for row in source.origin.related_rows)
    if source.kind == "USER_INPUT":
        parts.append("用户补充/编辑")
    elif source.kind == "DERIVED":
        parts.append("依据原文整理")
    note = source.note.replace("AI按用户要求整理", "").strip("； \n")
    if note:
        parts.append(note)
    return "；".join(parts)


def _write_value(cell: Any, value: Any) -> None:
    """字典与列表按 JSON 文本保存；以 = 开头的用户文本按字面字符串写入。"""
    if value is None:
        return
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    elif isinstance(value, float) or (
        isinstance(value, int) and not isinstance(value, bool) and len(str(abs(value))) > 15
    ):
        # 业务数值按完整文本保留，避免 Excel 数值精度截断；行号和修订仍用整数。
        value = str(value)
    if isinstance(value, str) and len(value) > 32767:
        raise BusinessError(
            "GENERATION_EXPORT_CELL_TOO_LONG",
            f"{cell.parent.title}!{cell.coordinate} 的内容超出 Excel 单元格容量，未生成截断文件",
            status_code=422,
            details={"sheet": cell.parent.title, "cell": cell.coordinate, "length": len(value)},
        )
    cell.value = value
    if isinstance(value, str):
        cell.data_type = "s"


def _safe_stem(filename: str) -> str:
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", Path(filename).stem.strip())
    return stem[:120] or "规则库"


def _relation_text(sheet: GenerationSheet, row: GenerationEntry) -> str:
    columns = []
    for column in sheet.columns:
        name = re.sub(r"\s+", "", column.name).casefold()
        if not is_identity_column(column) and column.code_role != "GROUP" and (
            column.code_role == "REFERENCE" or any(word in name for word in ("关联", "适用材料", "所属材料", "依赖"))
            or (any(word in name for word in ("规则", "材料")) and any(word in name for word in ("id", "编号")))
        ):
            columns.append(row.cells[column.column_id])
    return "\n".join(columns).casefold()


def _mentions_code(text: str, code: str) -> bool:
    code = code.casefold()
    if not code:
        return False
    if re.search(r"(?<![a-z0-9_.-])" + re.escape(code) + r"(?![a-z0-9_.-])", text):
        return True
    target = re.fullmatch(r"(.*?)(\d+)", code)
    if target is None:
        return False
    # 只判断现有编号是否落在明示区间，不展开区间或推断业务关系。
    for start, end in re.findall(r"([a-z0-9][a-z0-9_.-]*)\s*(?:[~～—–]|至)\s*([a-z0-9][a-z0-9_.-]*)", text):
        first, last = re.fullmatch(r"(.*?)(\d+)", start), re.fullmatch(r"(.*?)(\d+)", end)
        if first and last and first[1] == target[1] and last[1] in ("", first[1]):
            if len(target[2]) == len(first[2]) and int(first[2]) <= int(target[2]) <= int(last[2]):
                return True
    return False


def _export_note(row: GenerationEntry) -> str:
    note = row.source.note.replace("AI按用户要求整理", "").strip("； \n")
    return "" if any(note in value for value in row.cells.values()) else note


class GenerationExportService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def _existing_export(self, export: RulebookGenerationExport) -> RulebookGenerationExport:
        """历史导出必须是真实文件；缺失或损坏不拿当前文档重建冒充。"""
        path = resolve_storage_path(export.relative_path)
        if not path.is_file():
            raise BusinessError(
                "GENERATION_EXPORT_FILE_MISSING",
                "该修订的导出文件已不在存储目录中，不能重新生成冒充原产物",
                status_code=404,
                details={"export_no": export.export_no, "revision": export.revision},
            )
        size = path.stat().st_size
        sha256 = ImportsJobWorkbookService().file_sha256(path)
        if size != export.size or sha256 != export.sha256:
            raise BusinessError(
                "GENERATION_EXPORT_FILE_INVALID",
                "该修订的导出文件与创建时的内容不一致，不能作为原产物下载",
                status_code=409,
                details={"export_no": export.export_no, "revision": export.revision},
            )
        return export

    def create_export(self, job_no: str, *, expected_revision: int) -> RulebookGenerationExport:
        """按指定修订冻结导出；同一修订复用已有文件，不重新生成不同内容。"""
        job = GenerationQueryService(self.db).get_job(job_no, lock=True)
        if job.document_json is None:
            raise BusinessError(
                "GENERATION_DOCUMENT_MISSING", "该作业还没有已保存的编制结果，不能导出", status_code=409,
                details={"job_no": job_no},
            )
        existing = crud.RulebookGenerationExportCRUD(self.db).by_revision(int(job.id), expected_revision)
        if existing is not None:
            return self._existing_export(existing)

        document = GenerationHistoryService(self.db).document_at(job, expected_revision)
        if not document_has_business_content(document):
            raise BusinessError(
                "GENERATION_DOCUMENT_EMPTY",
                "该作业没有可导出的业务表格或待明确事项，不导出一份空规则库",
                status_code=409,
                details={"job_no": job_no, "revision": job.revision},
            )
        export_no = public_no("GENX")
        directory = f"rulebook_generations/{job.job_no}/exports/{expected_revision}"
        relative_path = f"{directory}/规则库.xlsx"
        target = resolve_storage_path(relative_path)
        self._discard_unreferenced_export(int(job.id), expected_revision, target)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            out = self._build_workbook(
                document, revision=expected_revision,
                source_filename=job.source_filename,
                business_background=job.business_background,
                generation_requirements=job.generation_requirements,
                source_path=self._layout_source_path(job),
            )
            try:
                out.save(temporary)
            finally:
                out.close()
            os.replace(temporary, target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        size = target.stat().st_size
        sha256 = ImportsJobWorkbookService().file_sha256(target)
        export = RulebookGenerationExport(
            export_no=export_no,
            generation_job_id=int(job.id),
            revision=expected_revision,
            document_snapshot_json=document.model_dump(mode="json"),
            export_format_version=GENERATION_EXPORT_FORMAT_VERSION,
            filename=f"{_safe_stem(job.source_filename)}-规则库-第{expected_revision}版.xlsx",
            relative_path=relative_path,
            sha256=sha256,
            size=size,
        )
        crud.RulebookGenerationExportCRUD(self.db).add(export)
        crud.RulebookGenerationExportCRUD(self.db).refresh(export)
        return export

    def _discard_unreferenced_export(self, job_id: int, revision: int, path: Path) -> None:
        bind = self.db.get_bind()

        def discard() -> None:
            with Session(bind=bind) as verification:
                if crud.RulebookGenerationExportCRUD(verification).by_revision(job_id, revision) is None:
                    path.unlink(missing_ok=True)

        after_rollback(self.db, discard)

    def get_export(self, job_no: str, export_no: str) -> tuple[RulebookGenerationExport, Any]:
        job = GenerationQueryService(self.db).get_job(job_no)
        export = crud.RulebookGenerationExportCRUD(self.db).by_no(int(job.id), export_no)
        if export is None:
            raise BusinessError(
                "GENERATION_EXPORT_NOT_FOUND", "该作业下不存在指定的导出修订", status_code=404,
                details={"job_no": job_no, "export_no": export_no},
            )
        return export, job

    def export_file(self, job_no: str, export_no: str) -> tuple[Path, RulebookGenerationExport]:
        export, _job = self.get_export(job_no, export_no)
        self._existing_export(export)
        return resolve_storage_path(export.relative_path), export

    def source_layout_file(self, job_no: str, export_no: str) -> tuple[BytesIO | Path, RulebookGenerationExport]:
        """旧产物按冻结快照重新排版供下载，不覆盖历史文件或创建业务修订。"""
        export, job = self.get_export(job_no, export_no)
        self._existing_export(export)
        if export.export_format_version == GENERATION_EXPORT_FORMAT_VERSION:
            return resolve_storage_path(export.relative_path), export
        snapshot = export.document_snapshot_json
        if snapshot.get("schema_version") != GENERATION_DOCUMENT_SCHEMA_VERSION:
            # 此前的执行配置文档没有当前业务表结构，保持原始产物。
            return resolve_storage_path(export.relative_path), export
        document = GenerationDocument.model_validate(snapshot)
        workbook = self._build_workbook(
            document, revision=export.revision, source_filename=job.source_filename,
            business_background=job.business_background, generation_requirements=job.generation_requirements,
            source_path=self._layout_source_path(job),
        )
        stream = BytesIO()
        try:
            workbook.save(stream)
        finally:
            workbook.close()
        stream.seek(0)
        return stream, export

    def _layout_source_path(self, job) -> Path:
        path = resolve_storage_path(job.source_path)
        if not path.is_file():
            raise BusinessError("GENERATION_SOURCE_MISSING", "上传的 Excel 原件缺失，无法沿用原表格式", status_code=404)
        if path.stat().st_size != job.source_size or ImportsJobWorkbookService().file_sha256(path) != job.source_sha256:
            raise BusinessError("GENERATION_SOURCE_CHANGED", "上传的 Excel 原件已变化，无法按原记录导出", status_code=409)
        return path

    def _build_workbook(
        self, document: GenerationDocument, *, revision: int, source_filename: str,
        business_background: str, generation_requirements: str,
        source_path: Path | None = None,
    ) -> Workbook:
        layout = GenerationWorkbookLayout(source_path)
        try:
            return self._populate_workbook(document, layout=layout)
        finally:
            layout.close()

    def _populate_workbook(self, document: GenerationDocument, *, layout: GenerationWorkbookLayout) -> Workbook:
        document = normalize_business_codes(document)
        wb = Workbook()
        wb.remove(wb.active)
        layout.prepare(wb)
        wb.properties.title = document.title
        wb.properties.subject = "业务规则库"
        wb.properties.description = document.summary
        names = {sheet.name.casefold() for sheet in document.sheets}

        def auxiliary_name(base: str) -> str:
            candidate = base
            number = 2
            while candidate.casefold() in names:
                candidate = f"{base}（{number}）"
                number += 1
            names.add(candidate.casefold())
            return candidate

        all_rows = {row.row_id: row for sheet in document.sheets for row in sheet.rows}
        row_names = {
            row.row_id: f"{sheet.name} · " + (" · ".join(
                row.cells[column.column_id] for column in sheet.columns if row.cells[column.column_id].strip()
            )[:160] or f"第{index}条")
            for sheet in document.sheets for index, row in enumerate(sheet.rows, 1)
        }
        relation_labels = {row.row_id: (f"{sheet.name} · {row.business_code}" if sheet.business_code_scope == "SHEET" and row.business_code
                                      else row.business_code or row_names[row.row_id])
                           for sheet in document.sheets for row in sheet.rows}
        # 表内编号须连同表名匹配，不能让另一张表的同号引用遮掉真实关联。
        aliases = {
            row.row_id: ({relation_labels[row.row_id]} if sheet.business_code_scope == "SHEET" else {
                row.business_code, *(row.cells[column.column_id].strip() for column in sheet.columns
                                     if is_identity_column(column)),
            }) for sheet in document.sheets for row in sheet.rows
        }
        relations = {row.row_id: list(row.related_row_ids) for row in all_rows.values()}
        for row in all_rows.values():
            if row.entry_kind == "MATERIAL":
                relations[row.row_id] = list(dict.fromkeys([
                    *relations[row.row_id], *(other.row_id for other in all_rows.values() if row.row_id in other.related_row_ids),
                ]))
        missing_relations = {row.row_id: [ref for ref in relations[row.row_id]
                             if not any(_mentions_code(_relation_text(sheet, row), code) for code in aliases[ref])]
                             for sheet in document.sheets for row in sheet.rows}
        for sheet in document.sheets:
            headings = {column.name for column in sheet.columns}

            def additional_heading(base: str) -> str:
                candidate = base
                number = 2
                while candidate in headings:
                    candidate = f"{base}（{number}）"
                    number += 1
                headings.add(candidate)
                return candidate

            code_heading = additional_heading("编号")
            relation_heading = additional_heading("关联条目编号")
            source_heading = additional_heading("依据与编制说明")
            rows = []
            has_codes = business_code_column(sheet) is None and any(row.business_code for row in sheet.rows)
            has_relations = any(missing_relations[row.row_id] for row in sheet.rows)
            has_notes = any(_export_note(row) for row in sheet.rows)
            for row in sheet.rows:
                values = [row.cells[column.column_id] for column in sheet.columns]
                related = "；".join(relation_labels[ref] for ref in missing_relations[row.row_id])
                rows.append([*([row.business_code] if has_codes else []), *values,
                             *([related] if has_relations else []),
                             *([_source_text(row.source.model_copy(update={"note": _export_note(row)})) if _export_note(row) else ""] if has_notes else [])])
            headers = [column.name for column in sheet.columns]
            layout.add_sheet(wb, sheet.name, [*([code_heading] if has_codes else []), *headers,
                                             *([relation_heading] if has_relations else []),
                                             *([source_heading] if has_notes else [])], rows,
                             write_value=_write_value, source=sheet.source, row_sources=[row.source for row in sheet.rows])

        materials = [(sheet, row) for sheet in document.sheets for row in sheet.rows if row.entry_kind == "MATERIAL"]
        # 已有完整关联的材料行或专门材料表可以直接充当目录；缺少的真实关联已补在原表。
        needs_catalog = any(missing_relations[row.row_id] and any(item.entry_kind != "MATERIAL" for item in sheet.rows)
                            for sheet, row in materials)
        if needs_catalog:
            catalog = []
            for sheet, material in materials:
                related_ids = set(material.related_row_ids)
                related_ids.update(row.row_id for row in all_rows.values() if material.row_id in row.related_row_ids)
                related = [row for ref, row in all_rows.items() if ref in related_ids]
                label = " · ".join([material.cells[column.column_id] for column in sheet.columns
                                    if material.cells[column.column_id].strip()][:2])
                catalog.append([
                    relation_labels[material.row_id], label,
                    "；".join(relation_labels[row.row_id] for row in related if row.entry_kind == "RULE"),
                    "；".join(relation_labels[row.row_id] for row in related if row.entry_kind != "RULE"),
                    _source_text(material.source),
                ])
            layout.add_sheet(wb, auxiliary_name("材料目录"), ["材料编号", "材料说明", "关联业务规则", "关联其他内容", "依据"], catalog,
                             write_value=_write_value, style_only=True)

        for status, title in (
            ("OPEN", "待明确事项"), ("RESOLVED", "已确认补充"),
        ):
            issues = [issue for issue in document.issues if issue.status == status]
            if not issues:
                continue
            layout.add_sheet(wb, auxiliary_name(title), ["事项", "问题与影响", "相关业务条目", "确认说明", "依据"], [
                [issue.title, issue.detail,
                 "；".join(row_names[reference] for reference in issue.affected_row_ids),
                 issue.resolution, _source_text(issue.source)]
                for issue in issues
            ], write_value=_write_value, style_only=True)
        return wb
