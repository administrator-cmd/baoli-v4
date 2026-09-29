#!/usr/bin/env python
"""历史任务材料类型状态修复：把旧自动识别留下的 TYPE_MISMATCH 归一为 TYPE_REVIEW。

## 背景

`TYPE_MISMATCH` 只有一个来源：2026-09-16 之前由已被下线替换的 8018 自动识别判定写入。
当前业务语义把「类型不可用」分成两种，且必须区分：

    TYPE_REVIEW   材料类型待复核（尚未由人定案）
    TYPE_REJECTED 人工已确认与材料位不符（确定的人工结论）

旧值无法表达“是否已由人裁决”，所以一律按**待复核**读取。本脚本只做这一件事：
把库里的旧值显式写成 `TYPE_REVIEW`，让记录本身与当前语义一致，而不是长期依赖读取时的归一。

## 适用范围（执行前必须逐条核对）

1. `task_document.status` = `TYPE_MISMATCH`，且该材料位**当前上传**没有任何未失效的
   `MATERIAL_TYPE_MISMATCH` 人工复核记录（PENDING / RESOLVED 都算）。
   这类状态由自动识别写入、从未由人裁决，属历史缺陷留下的状态。
2. 该材料位当前上传存在可用的 `MATERIAL_CLASSIFY` 产物时，把识别结论写入
   `stored_artifact.metadata_json.review_backfill` 作为可追溯信息；不据此改材料状态，
   也不替人下结论。

**不处理**已有人工裁决记录的材料位：那种情况下材料状态是人工结论，脚本改状态值会丢掉
“这条是人定的”这一事实。脚本会逐条列出跳过原因。

## 安全性

- 只改状态字段值（`TYPE_MISMATCH` → `TYPE_REVIEW`）并追加一段追溯信息，
  不删除任何行、不改人工裁决、不改规则结果。
- 默认只读预览；必须显式加 `--yes` 才写入。每条的写入独立提交，失败即中止并保留现场。
- 材料的真实业务结论仍由人通过既有复核入口给出：脚本不改 `PREPARED`、不代填结论、
  也不新建复核待办（待办由代码在进入复核链路时自动补齐）。

## 用法（项目根目录）

    python script/migrate_type_mismatch_status.py            # 只读预览
    python script/migrate_type_mismatch_status.py --yes      # 真正执行

执行后按提示重新执行受影响任务的「文件处理」节点：该节点会按新的材料类型状态重算文件
质量结论，并为这些材料位补齐“材料类型复核”待办，供人工给出真实结论。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pymysql  # noqa: E402
from sqlalchemy.engine import URL  # noqa: E402

from app.config import get_settings, validated_database_url  # noqa: E402

LEGACY_STATUS = "TYPE_MISMATCH"
REVIEW_STATUS = "TYPE_REVIEW"
REVIEW_TYPE = "MATERIAL_TYPE_MISMATCH"


def _validated_database_url() -> URL:
    """与后端相同的目标校验：驱动与库名都不能被改到别处。"""
    settings = get_settings()
    try:
        return validated_database_url(settings.database_url)
    except ValueError as exc:
        raise SystemExit(f"拒绝执行：数据库目标无效（{exc}）") from exc


def _connection_kwargs(url: URL) -> dict[str, object]:
    return {
        "host": url.host or "127.0.0.1",
        "port": url.port or 3306,
        "user": url.username,
        "password": url.password or "",
        "database": url.database,
        "charset": "utf8mb4",
        "autocommit": False,
    }


_CANDIDATE_SQL = """
SELECT d.id, d.document_no, d.material_key, d.material_name, t.task_no, t.task_name,
       u.id AS upload_id, u.upload_no, u.original_filename
  FROM task_document d
  JOIN audit_task t ON t.id = d.audit_task_id
  LEFT JOIN document_upload u ON u.id = d.current_upload_id
 WHERE d.status = %s
 ORDER BY t.id, d.id
"""

_REVIEW_SQL = """
SELECT r.status, COUNT(*) AS n
  FROM manual_review r
 WHERE r.document_upload_id = %s
   AND r.review_type = %s
   AND r.status <> 'INVALIDATED'
 GROUP BY r.status
"""

_ARTIFACT_SQL = """
SELECT artifact_no, status, metadata_json
  FROM stored_artifact
 WHERE document_upload_id = %s AND artifact_type = 'MATERIAL_CLASSIFY'
 ORDER BY id DESC
 LIMIT 1
"""


def _classify(cursor: pymysql.cursors.Cursor, document: dict) -> tuple[bool, str, dict | None]:
    """该材料位是否属于本次修复范围，返回 (是否修复, 原因, 追溯信息)。"""
    upload_id = document["upload_id"]
    if upload_id is None:
        return False, "当前没有上传文件，类型状态没有对应的识别产物可追溯", None
    cursor.execute(_REVIEW_SQL, (upload_id, REVIEW_TYPE))
    by_status = {str(row["status"]): int(row["n"]) for row in cursor.fetchall()}
    if by_status:
        detail = "、".join(f"{key} {value} 条" for key, value in sorted(by_status.items()))
        return False, f"该上传已有人工类型裁决记录（{detail}），状态是人工结论，不覆盖", None
    cursor.execute(_ARTIFACT_SQL, (upload_id,))
    artifact = cursor.fetchone()
    trace = {
        "normalized_from": LEGACY_STATUS,
        "normalized_to": REVIEW_STATUS,
        "reason": "旧自动识别写入、从无人工类型裁决记录的材料位状态，按当前业务语义归一",
    }
    if artifact is None:
        trace["material_classify_artifact_no"] = None
        trace["material_classify_note"] = "该上传没有 MATERIAL_CLASSIFY 产物，无法提供识别结论供复核参考"
        return True, "旧自动识别状态；该上传无识别产物，仍需人工定案", trace
    trace["material_classify_artifact_no"] = str(artifact["artifact_no"])
    try:
        payload = artifact["metadata_json"]
        content = (json.loads(payload) if isinstance(payload, str) else payload).get("content") or {}
    except (TypeError, ValueError):
        content = {}
    trace["material_classify_result"] = {
        key: content.get(key)
        for key in ("declared_material_type", "detected_material_type", "matches_declared_material", "confidence")
    }
    return True, "旧自动识别状态，从未由人裁决", trace


def main() -> int:
    parser = argparse.ArgumentParser(description="把历史遗留的 TYPE_MISMATCH 材料状态归一为 TYPE_REVIEW")
    parser.add_argument("--yes", action="store_true", help="确认写入；不加此参数时只做只读预览")
    args = parser.parse_args()

    url = _validated_database_url()
    print(f"目标数据库：{url.host or '127.0.0.1'}:{url.port or 3306}/{url.database}（用户 {url.username}）")

    try:
        connection = pymysql.connect(**_connection_kwargs(url))
    except pymysql.MySQLError as exc:
        raise SystemExit(f"数据库连接失败：{exc}") from exc

    try:
        with connection.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(_CANDIDATE_SQL, (LEGACY_STATUS,))
            candidates = list(cursor.fetchall())

            if not candidates:
                print(f"没有状态为 {LEGACY_STATUS} 的材料位，无需修复。")
                return 0

            to_fix: list[tuple[dict, dict]] = []
            skipped: list[tuple[dict, str]] = []
            for document in candidates:
                fix, reason, trace = _classify(cursor, document)
                label = f"{document['task_no']} / {document['document_no']}（{document['material_name']}）"
                if fix and trace is not None:
                    to_fix.append((document, trace))
                    print(f"  将修复 {label}：{reason}")
                else:
                    skipped.append((document, reason))
                    print(f"  跳过   {label}：{reason}")

            print()
            print(f"待修复 {len(to_fix)} 条，跳过 {len(skipped)} 条。")
            if not args.yes:
                print("当前是只读预览，未做任何修改。确认执行时请加 --yes 重新运行：")
                print("    python script/migrate_type_mismatch_status.py --yes")
                return 0

            if to_fix:
                for document, trace in to_fix:
                    with connection.cursor() as write_cursor:
                        # 只在状态仍是旧值时写入，避免覆盖执行期间发生的其他变更。
                        updated = write_cursor.execute(
                            "UPDATE task_document SET status = %s WHERE id = %s AND status = %s",
                            (REVIEW_STATUS, document["id"], LEGACY_STATUS),
                        )
                        if updated != 1:
                            connection.rollback()
                            raise SystemExit(
                                f"写入未生效：{document['document_no']} 的状态在本次执行期间已被其他操作改变，"
                                "已回滚本条，请重新运行脚本核对"
                            )
                        artifact_no = trace.get("material_classify_artifact_no")
                        if artifact_no:
                            write_cursor.execute(
                                """
                                UPDATE stored_artifact
                                   SET metadata_json = JSON_SET(
                                       metadata_json, '$.review_backfill', CAST(%s AS JSON))
                                 WHERE artifact_no = %s
                                """,
                                (json.dumps(trace, ensure_ascii=False), artifact_no),
                            )
                    connection.commit()
                    print(f"  已修复 {document['document_no']}（{document['material_name']}）")

        print()
        print(f"完成：已修复 {len(to_fix)} 条材料位，跳过 {len(skipped)} 条。")
        print("后续动作（需人工执行）：")
        print("  1. 对每条已修复的任务重新执行「文件处理」节点，使文件质量结论按新的材料类型状态重算；")
        print("     该节点会同时为这些材料位补齐“材料类型复核”待办，供人工给出真实结论。")
        print("  2. 在人工复核入口逐条给出类型结论；系统不会代填结论，也不改已有人工裁决。")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
