"""分组导入；只有所有分组通过最终契约才能进入导入草稿。"""

import hashlib
import json
import logging
from collections import Counter

from pydantic import ValidationError

from app.common.exceptions import BusinessError
from app.core.config import get_settings
from app.domain.configuration import field_strategy
from app.domain.imports.workbook_proposals import WorkbookProposals
from app.integrations.llm.gateway import call_json
from app.modules.imports.job.response_execution import save_raw_response
from app.modules.imports.job.service.grouped_plan import (
    GROUPS,
    IndexBlock,
    SourceBook,
    directory,
    invalid,
    section_schema,
    size,
)
from app.modules.imports.job.service.grouped_prompts import GROUP_PROMPT, INDEX_PROMPT
from app.modules.imports.job.service.stage_cache import GroupedStageCache

logger = logging.getLogger(__name__)
KEYS = (*GROUPS, "binding_gaps")
REPAIRABLE = {"LLM_CONTRACT_INVALID", "LLM_SOURCE_REFERENCE_INVALID", "LLM_OUTPUT_TRUNCATED"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def grouped_contract():
    settings = get_settings()
    return digest(
        [
            "grouped-v1",
            INDEX_PROMPT,
            GROUP_PROMPT,
            IndexBlock.model_json_schema(),
            WorkbookProposals.model_json_schema(),
            field_strategy.contract_payload(),
            settings.rulebook_group_input_chars,
            settings.rulebook_group_objects,
            settings.llm_model_name,
            settings.rulebook_llm_reasoning_effort,
            settings.rulebook_group_field_reasoning_effort,
        ]
    )


class GroupedImport:
    def __init__(self, validator, sheets, scenario_code, operation_no, source_path, fingerprint):
        self.validator = validator
        self.sheets = sheets
        self.book = SourceBook(sheets)
        self.scenario = scenario_code
        self.op = operation_no
        self.source = source_path
        self.input_key = fingerprint
        self.cache = GroupedStageCache()
        self.key = self.cache.build_fingerprint(
            fingerprint, digest([s.model_input() for s in sheets.values()])
        )
        self.settings = get_settings()
        self.schema = WorkbookProposals.model_json_schema()
        self.state = {"blocks": {}, "parts": {}, "split_index": [], "split_parts": []}

    def save(self):
        self.cache.save_initial(self.source, self.key, self.input_key, self.state)

    def fits(self, payload, prompt):
        return size(payload) + len(prompt) <= self.settings.rulebook_group_input_chars

    def ask(self, payload, prompt, stage):
        if not self.fits(payload, prompt):
            raise BusinessError(
                "IMPORT_GROUP_CONTEXT_LIMIT",
                "必要原文和契约超过分组输入预算，未截断原文",
                details={"stage": stage, "input_chars": size(payload) + len(prompt)},
            )
        logger.info(
            "规则库分组模型请求 operation_no=%s stage=%s input_chars=%s",
            self.op,
            stage,
            size(payload) + len(prompt),
        )
        effort = self.settings.rulebook_llm_reasoning_effort
        if payload.get("response_schema", {}).get("required") == ["fields"]:
            effort = self.settings.rulebook_group_field_reasoning_effort or effort
        return call_json(
            payload,
            prompt,
            reasoning_effort=effort,
            idempotency_key=f"{self.op}:{stage}",
            on_content=lambda text: save_raw_response(self.op, text, stage=stage, source_path=self.source),
        )

    def index(self, ids):
        packed = self.book.pack(ids)
        payload = {
            "scenario_code": self.scenario,
            "owned": ids,
            "workbook": packed,
            "schema": IndexBlock.model_json_schema(),
        }
        key = f"{ids[0]}_{ids[-1]}"
        if key in self.state["split_index"] or (len(ids) > 1 and not self.fits(payload, INDEX_PROMPT)):
            return self.split_index(ids)
        cached = self.state["blocks"].get(key)
        if cached is not None:
            try:
                block = IndexBlock.model_validate(cached)
                self.book.validate_index(block, ids, packed)
            except (ValidationError, BusinessError) as exc:
                raise BusinessError(
                    "IMPORT_INITIAL_STAGE_UNAVAILABLE",
                    "缓存目录与原文不符，需要整份重新解析",
                    status_code=409,
                ) from exc
            return block.entries
        for attempt in range(2):
            try:
                raw = self.ask(payload, INDEX_PROMPT, f"index_{key}_{attempt}")
                block = IndexBlock.model_validate(raw)
                self.book.validate_index(block, ids, packed)
                break
            except (ValidationError, BusinessError) as exc:
                if isinstance(exc, BusinessError) and exc.code not in REPAIRABLE:
                    raise
                if isinstance(exc, BusinessError) and exc.code == "LLM_OUTPUT_TRUNCATED" and len(ids) > 1:
                    self.state["split_index"].append(key)
                    self.save()
                    return self.split_index(ids)
                if attempt:
                    raise invalid("规则库目录未通过分块校验", block=key, error=str(exc)[:800]) from exc
                payload["correction"] = self.feedback(exc)
        self.state["blocks"][key] = block.model_dump(mode="json", exclude_defaults=True)
        self.save()
        return block.entries

    def split_index(self, ids):
        # 优先在工作表边界或同表的空行切开，跨边界标题和合并单元格锚点随原行补回。
        mid = len(ids) // 2
        boundaries = [
            i
            for i in range(1, len(ids))
            if self.book.origins[ids[i]][0] != self.book.origins[ids[i - 1]][0]
            or self.book.origins[ids[i]][1] > self.book.origins[ids[i - 1]][1] + 1
        ]
        cut = min(boundaries, key=lambda n: abs(n - mid)) if boundaries else mid
        return self.index(ids[:cut]) + self.index(ids[cut:])

    @staticmethod
    def feedback(exc):
        if isinstance(exc, ValidationError):
            return exc.errors(include_input=False, include_url=False, include_context=False)[:6]
        return {"reason": exc.message, "details": exc.details}

    def payload(self, owners, extra):
        related = set(owners) | extra
        for ref in owners:
            related.update(self.edges[ref])
        rows = {rid for ref in related for rid in (self.entries[ref].row, *self.entries[ref].related)}
        groups = {self.entries[ref].group for ref in owners}
        if groups & {"rules", "calculations"}:
            groups.add("binding_gaps")
        result = {
            "owned": owners,
            "catalog": [
                row
                for row in self.catalog
                if groups != {"fields"} or row[1] == "materials" or row[0] in related
            ],
            "workbook": self.book.pack(rows),
            "response_schema": section_schema(self.schema, groups),
        }
        if "fields" in groups:
            result["field_contract"] = field_strategy.contract_payload()
        return result

    def check_part(self, owners, payload, raw):
        wanted = set(payload["response_schema"]["required"])
        if (
            not isinstance(raw, dict)
            or not wanted.issubset(raw)
            or any(k not in KEYS or raw[k] != [] for k in raw.keys() - wanted)
        ):
            raise invalid("分组返回的顶层属性不符合契约", required=sorted(wanted))
        part = WorkbookProposals.model_validate({**{k: [] for k in KEYS}, **raw}).model_dump(mode="json")
        origins = {(s["sheet"], r[1]): r[2] for s in payload["workbook"] for r in s["rows"]}
        self.validator._validate_sources(part, origins, attach=False)
        refs = []
        for group in GROUPS:
            for obj in part[group]:
                ref = obj.get("support_ref") if group == "supporting_content" else obj["candidate_ref"]
                refs.append(ref)
                if ref not in owners or self.entries[ref].group != group:
                    raise invalid("分组返回了未分配的对象", reference=ref)
                origin = obj["origin"]
                source = self.book.origins[self.entries[ref].row]
                if source not in {(r["sheet_name"], r["row_no"]) for r in [origin, *origin["related_rows"]]}:
                    raise invalid("分组结果没有引用目录认定的原始定义行", reference=ref, expected=source)
                if group == "calculations" and obj["result_code"] != self.codes[ref]:
                    raise invalid("计算输出标识与跨组目录不一致", reference=ref, expected=self.codes[ref])
        if Counter(refs) != Counter(owners):
            raise invalid("分组遗漏或重复返回目录对象", expected=owners, actual=refs)
        return part

    def part(self, owners, hint=None):
        key = ",".join(owners)
        payload = self.payload(owners, set())
        if hint:
            payload["correction"] = hint
        if key in self.state["split_parts"] or (not self.fits(payload, GROUP_PROMPT) and len(owners) > 1):
            return self.split_part(owners, hint)
        saved = self.state["parts"].get(key)
        if saved is not None and not hint:
            try:
                self.check_part(owners, self.payload(owners, set(saved["extra"])), saved["raw"])
                return [(key, saved["raw"])]
            except (BusinessError, ValidationError, KeyError, TypeError):
                logger.warning("分组检查点不可复用，重新生成当前组 owners=%s", owners)
                del self.state["parts"][key]
                self.save()
        extra = set()
        searched = set()
        for attempt in range(3):
            try:
                raw = self.ask(
                    payload,
                    GROUP_PROMPT,
                    f"group_{owners[0]}_{len(owners)}_{attempt}" + ("_fix" if hint else ""),
                )
                if isinstance(raw, dict) and "need_names" in raw:
                    names = raw["need_names"]
                    if (
                        not isinstance(names, list)
                        or not names
                        or not all(isinstance(n, str) and n.strip() for n in names)
                        or attempt == 2
                        or set(names) <= searched
                    ):
                        raise invalid("补读名称请求不完整或重复", requested=names)
                    searched.update(names)
                    matches = {
                        name: [
                            ref
                            for ref, item in self.entries.items()
                            if name == item.source_id or name in item.name
                        ]
                        for name in names
                    }
                    extra.update(ref for candidates in matches.values() for ref in candidates)
                    payload = self.payload(owners, extra)
                    payload["lookup_results"] = matches
                    if hint:
                        payload["correction"] = hint
                    continue
                if isinstance(raw, dict) and "need_refs" in raw:
                    needs = raw["need_refs"]
                    if (
                        not isinstance(needs, list)
                        or not needs
                        or not all(isinstance(n, str) and n in self.entries for n in needs)
                        or set(needs) <= extra
                        or attempt == 2
                    ):
                        raise invalid("跨表补读未能定位必要原文", requested=needs)
                    extra.update(needs)
                    payload = self.payload(owners, extra)
                    if hint:
                        payload["correction"] = hint
                    continue
                if isinstance(raw, dict) and raw.get("index_problem"):
                    raise BusinessError(
                        "IMPORT_GROUP_INDEX_CONFLICT",
                        "目录需要重新核对：" + str(raw["index_problem"])[:400],
                        status_code=422,
                    )
                self.check_part(owners, payload, raw)
                compact = {k: raw[k] for k in payload["response_schema"]["required"]}
                self.state["parts"][key] = {"raw": compact, "extra": sorted(extra)}
                self.save()
                return [(key, compact)]
            except (ValidationError, BusinessError) as exc:
                if (
                    isinstance(exc, BusinessError)
                    and exc.code in {"LLM_OUTPUT_TRUNCATED", "IMPORT_GROUP_CONTEXT_LIMIT"}
                    and len(owners) > 1
                ):
                    self.state["split_parts"].append(key)
                    self.save()
                    return self.split_part(owners, hint)
                if isinstance(exc, BusinessError) and exc.code not in REPAIRABLE:
                    raise
                if attempt >= 1:
                    raise invalid(
                        "分组校验失败；已完成组已保留", owners=owners, diagnostic=self.feedback(exc)
                    ) from exc
                payload["correction"] = self.feedback(exc)

    def split_part(self, owners, hint):
        mid = len(owners) // 2
        return self.part(owners[:mid], hint) + self.part(owners[mid:], hint)

    def assemble(self, parts):
        result = WorkbookProposals.model_validate(
            {k: [v for _, part in parts for v in part.get(k, [])] for k in KEYS}
        ).model_dump(mode="json")
        self.validator._validate_content_references(result)
        field_material = {obj["candidate_ref"]: obj["material_ref"] for obj in result["fields"]}
        problems = [
            {
                "owner": rule["candidate_ref"],
                "kind": "rule.inputs.field_material",
                "reference": inp,
                "expected_material": field_material[inp["field_ref"]],
            }
            for rule in result["rules"]
            for inp in rule["inputs"]
            if inp["field_ref"] in field_material and inp["material_ref"] != field_material[inp["field_ref"]]
        ]
        if problems:
            raise invalid("规则输入与字段实际归属的材料不一致", references=problems)
        self.validator._validate_proposal_references(result)
        self.validator._validate_sources(result, self.validator._source_rows(self.sheets), attach=True)
        self.validator._attach_supporting_context(result)
        return result

    def run(self, resume):
        if resume:
            self.state = self.cache.load_initial(self.source, self.key)
            if (
                self.state is None
                or not all(isinstance(self.state.get(k), dict) for k in ("blocks", "parts"))
                or not all(isinstance(self.state.get(k), list) for k in ("split_index", "split_parts"))
            ):
                raise BusinessError(
                    "IMPORT_INITIAL_STAGE_UNAVAILABLE", "分组检查点不可用，需要整份重新解析", status_code=409
                )
        else:
            self.save()  # FULL 不继承上次成功组。
        self.entries, self.edges = directory(self.index(list(self.book.rows)))
        if not self.entries:
            raise invalid("整本非空工作簿未识别出任何业务对象或辅助资料，不能生成空提案")
        counts = Counter(e.source_id for e in self.entries.values() if e.group == "calculations")
        self.codes = {
            r: (e.source_id if e.source_id and counts[e.source_id] == 1 else r)
            for r, e in self.entries.items()
            if e.group == "calculations"
        }
        self.catalog = [
            [r, e.group, e.name, e.source_id, *self.book.origins[e.row], self.codes.get(r)]
            for r, e in self.entries.items()
        ]
        parts = []
        limit = self.settings.rulebook_group_objects
        refs = list(self.entries)
        if len(refs) <= limit and self.fits(self.payload(refs, set()), GROUP_PROMPT):
            parts.extend(self.part(refs))
        else:
            for group in GROUPS:
                refs = [r for r, e in self.entries.items() if e.group == group]
                for start in range(0, len(refs), limit):
                    parts.extend(self.part(refs[start : start + limit]))
        try:
            return self.assemble(parts)
        except BusinessError as exc:
            problems = exc.details.get("references", [])
            owners = {
                p.get("owner") for p in problems if isinstance(p, dict) and isinstance(p.get("owner"), str)
            }
            affected = {key for key, _ in parts if owners.intersection(key.split(","))}
            if not affected:
                raise
            for key in affected:
                self.state["parts"].pop(key, None)
            self.save()
            repaired = []
            for key, data in parts:
                if key in affected:
                    repaired.extend(self.part(key.split(","), self.feedback(exc)))
                else:
                    repaired.append((key, data))
            try:
                return self.assemble(repaired)
            except BusinessError:
                # 不把仍然有错的组当作下次续跑的可信结果。
                for group_key, _ in repaired:
                    if any(r in owners for r in group_key.split(",")):
                        self.state["parts"].pop(group_key, None)
                self.save()
                raise
