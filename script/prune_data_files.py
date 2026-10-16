#!/usr/bin/env python
"""清理 data/ 下数据库已不再引用的文件：有记录的留下，没记录的删除。

引用来源只有数据库里真实存在的路径列，共 5 处：

- `background_operation.raw_response_path`：规则库解析的模型原始返回
- `import_job.source_path`：上传的规则库 Excel
- `document_upload.relative_path`：任务上传的材料（含已被替换的历史上传版本）
- `stored_artifact.relative_path`：文件处理 / OCR / 印章 / 材料识别产物
- `audit_task.value_file_json`：取值主表、全部业务明细表及显式转换保留的原文件

除这 5 处指向的文件外，data/ 下的其它文件都是孤儿，属于本脚本的清理范围。

安全约束：
- 默认只列清单，不做任何修改；必须显式加 --yes 才会真正删除。
- 只删除解析到存储根目录以内的文件，越界路径直接报错。
- 数据库指向但磁盘上不存在的文件单独列出，属于数据不一致，不算孤儿，也不掩盖。
- 删除文件后只回收变空的目录，绝不动存储根目录本身。

用法（在项目根目录或任意位置均可）：

    python script/prune_data_files.py            # 只看会删哪些文件
    python script/prune_data_files.py --yes      # 真正删除

删除不可恢复。确认清单里的文件确实不需要保留后再加 --yes。
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
        "autocommit": True,
    }


def _referenced_paths(connection: pymysql.Connection) -> dict[str, str]:
    """数据库引用的全部相对路径 → 来源说明。同一路径被多处引用时保留第一个来源。"""
    queries = (
        ("background_operation.raw_response_path", "SELECT raw_response_path FROM background_operation WHERE raw_response_path IS NOT NULL"),
        ("import_job.source_path", "SELECT source_path FROM import_job"),
        ("document_upload.relative_path", "SELECT relative_path FROM document_upload"),
        ("stored_artifact.relative_path", "SELECT relative_path FROM stored_artifact"),
    )
    referenced: dict[str, str] = {}
    with connection.cursor() as cursor:
        for source, sql in queries:
            cursor.execute(sql)
            for row in cursor.fetchall():
                value = row[0]
                if value:
                    referenced.setdefault(str(value), source)
        # 明细文件同样是正式业务数据，不能只保护 relative_path 指向的主表。
        cursor.execute("SELECT value_file_json FROM audit_task WHERE value_file_json IS NOT NULL")
        for (raw,) in cursor.fetchall():
            metadata = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            paths = list((metadata.get("files") or {}).values()) + list(metadata.get("legacy_files") or [])
            for batch in metadata.get("batches") or []:
                paths.extend(batch["files"].values())
            if metadata.get("relative_path"):
                paths.append(metadata["relative_path"])
            for relative in paths:
                referenced.setdefault(str(relative), "audit_task.value_file_json")
    return referenced


def _storage_files(root: Path) -> list[Path]:
    """存储根目录下的全部真实文件（不含目录本身）。"""
    return sorted((path for path in root.rglob("*") if path.is_file()), key=lambda item: item.as_posix())


def _relative_of(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def _remove_empty_dirs(root: Path) -> list[Path]:
    """回收删除文件后变空的目录，从最深层往上；不动存储根目录。"""
    removed: list[Path] = []
    directories = sorted(
        (path for path in root.rglob("*") if path.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    )
    for directory in directories:
        if directory == root:
            continue
        try:
            directory.rmdir()
        except OSError:
            continue
        removed.append(directory)
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description="删除 data/ 下数据库不再引用的文件（不可恢复）")
    parser.add_argument("--yes", action="store_true", help="确认执行；不加此参数时只列出将要删除的文件")
    args = parser.parse_args()

    settings = get_settings()
    root = settings.storage_root.resolve()
    if not root.is_dir():
        raise SystemExit(f"存储根目录不存在：{root}")

    url = _validated_database_url()
    print(f"目标数据库：{url.host or '127.0.0.1'}:{url.port or 3306}/{url.database}（用户 {url.username}）")
    print(f"存储根目录：{root}")

    try:
        connection = pymysql.connect(**_connection_kwargs(url))
    except pymysql.MySQLError as exc:
        raise SystemExit(f"数据库连接失败：{exc}") from exc

    try:
        referenced = _referenced_paths(connection)
    finally:
        connection.close()

    files = _storage_files(root)
    by_relative = {_relative_of(root, path): path for path in files}

    orphans: list[Path] = []
    missing: list[tuple[str, str]] = []
    for relative, path in by_relative.items():
        if relative not in referenced:
            orphans.append(path)
    for relative, source in referenced.items():
        if relative not in by_relative:
            missing.append((relative, source))

    keep_bytes = sum(path.stat().st_size for relative, path in by_relative.items() if relative in referenced)
    orphan_bytes = sum(path.stat().st_size for path in orphans)

    print()
    print(f"数据库引用路径 {len(referenced)} 条，磁盘文件 {len(files)} 个。")
    print(f"保留 {len(by_relative) - len(orphans)} 个（{_human_size(keep_bytes)}），孤儿 {len(orphans)} 个（{_human_size(orphan_bytes)}）。")

    if missing:
        print()
        print(f"数据库指向但磁盘上不存在的文件 {len(missing)} 个（数据不一致，不按孤儿处理）：")
        for relative, source in sorted(missing):
            print(f"  ! {relative}  ← {source}")

    if not orphans:
        print()
        print("没有需要清理的孤儿文件。")
        return 0

    print()
    print("孤儿文件（数据库没有任何记录引用）：")
    for path in orphans:
        print(f"  - {_relative_of(root, path)}  ({_human_size(path.stat().st_size)})")

    if not args.yes:
        print()
        print("当前是只读预览，未删除任何文件。确认清单无误后加 --yes 重新执行：")
        print("    python script/prune_data_files.py --yes")
        return 0

    deleted = 0
    for path in orphans:
        path.unlink()
        deleted += 1
    removed_dirs = _remove_empty_dirs(root)

    remaining = _storage_files(root)
    leftover = [path for path in remaining if _relative_of(root, path) not in referenced]
    print()
    print(f"已删除 {deleted} 个文件，回收空目录 {len(removed_dirs)} 个，释放 {_human_size(orphan_bytes)}。")
    if leftover:
        raise SystemExit(f"清理未完成，仍有 {len(leftover)} 个孤儿文件存在")
    print(f"现在 data/ 下 {len(remaining)} 个文件全部有数据库记录引用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
