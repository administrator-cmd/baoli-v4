#!/usr/bin/env python
"""清理 data/ 下数据库已不再引用的文件：有记录的留下，没记录的删除。

引用来源包括数据库路径列、JSON 内的路径与业务约定的配套文件：

- `background_operation.raw_response_path`：规则库解析的模型原始返回
- `import_job.source_path`：上传的规则库 Excel
- `document_upload.relative_path`：任务上传的材料（含已被替换的历史上传版本）
- `stored_artifact.relative_path`：文件处理 / OCR / 印章 / 材料识别产物
- `stored_artifact.artifact_type`：与产物同目录的信封 JSON（二进制产物的正文是 PDF）
- `import_job` + `imports/{job_no}/llm/initial.json`：第二轮重试要用的第一轮解析目录
- `audit_task.value_file_json`：取值四表
- `rule_result.result_json.verdict_errors[].diagnostics_path`：审核失败的模型响应诊断

`background_operation.raw_response_path` 只登记最后一次调用，同一操作的两轮响应
写在同一目录，因此该目录整体受保护。

固定锁文件以及 .rollback/.restoring 恢复文件不属于业务孤儿，自动清理时保留；
恢复文件须结合失败日志人工核对。删除任务中途终止留下的 `.deleting-*` 暂存目录
可能持有唯一副本，同样保留并单独列出。其余文件按以上数据库引用判断。

安全约束：
- 默认只列清单，不做任何修改；必须显式加 --yes 才会真正删除。
- 只删除解析到存储根目录以内的文件，越界路径直接报错。
- 数据库指向但磁盘上不存在的文件单独列出，属于数据不一致，不算孤儿，也不掩盖；
  非 READY 产物的占位路径（*.processing / *.submitting / *.pending）、配套信封和尚未
  生成的第一轮检查点不保证存在，不计入该项；READY 产物的正文与信封仍检查缺失。
- 删除文件后只回收变空的目录，绝不动存储根目录本身。
- 实际删除只用于离线维护：先停止所有访问该存储目录的后端及后台工作进程，完成
  停止后用 --offline 显式确认。此参数由操作者确认，不自动检测进程；删除期间不得
  重启服务。仅重查数据库无法排除“文件先落盘、引用后提交”的竞态，预览不提供此保证。

用法（在项目根目录或任意位置均可）：

    python script/prune_data_files.py            # 只看会删哪些文件
    python script/prune_data_files.py --yes --offline  # 停止后端及后台进程后真正删除

删除不可恢复。确认清单里的文件确实不需要保留后再加 --yes。
"""

from __future__ import annotations

import argparse
import errno
import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pymysql  # noqa: E402
from sqlalchemy.engine import URL  # noqa: E402

from app.core.config import get_settings, validated_database_url  # noqa: E402
from app.services.imports.stage_cache import initial_relative_path  # noqa: E402


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


def _referenced_paths(
    connection: pymysql.Connection, root: Path,
) -> tuple[dict[str, str], set[str], set[str]]:
    """返回引用来源、受保护目录与必须存在的路径。

    同一路径被多处引用时保留第一个来源。`background_operation.raw_response_path`
    只记录最后一次调用，而同一操作的两轮响应写在同一目录，因此该目录整体保护。
    """
    queries = (
        ("background_operation.raw_response_path", "SELECT raw_response_path FROM background_operation WHERE raw_response_path IS NOT NULL"),
        ("import_job.source_path", "SELECT source_path FROM import_job"),
        ("document_upload.relative_path", "SELECT relative_path FROM document_upload"),
    )
    referenced: dict[str, str] = {}
    protected_dirs: set[str] = set()
    required_paths: set[str] = set()

    def add_path(relative: str, source: str, *, required: bool = True) -> str:
        normalized = _relative_of(root, root / relative)
        referenced.setdefault(normalized, source)
        if required:
            required_paths.add(normalized)
        return normalized

    with connection.cursor() as cursor:
        for source, sql in queries:
            cursor.execute(sql)
            for row in cursor.fetchall():
                value = row[0]
                if value:
                    relative = add_path(str(value), source)
                    if source == "background_operation.raw_response_path":
                        protected_dirs.add(Path(relative).parent.as_posix())
        # 产物正文与同名信封都要保护：二进制产物（8010）的 relative_path 指向 PDF，
        # 信封保存在同目录的 {artifact_type}.json，也是读取产物时的必需文件。
        cursor.execute("SELECT relative_path, artifact_type, status FROM stored_artifact")
        for relative, artifact_type, status in cursor.fetchall():
            if not relative:
                continue
            relative = str(relative)
            add_path(relative, "stored_artifact.relative_path",
                     required=status == "READY" or not relative.endswith(_OPTIONAL_SUFFIXES))
            envelope = Path(relative).parent / f"{str(artifact_type).lower()}.json"
            add_path(envelope.as_posix(), "stored_artifact.envelope", required=status == "READY")
        # 导入作业的第一轮解析检查点：第二轮重试要用，不能当孤儿删除。
        cursor.execute("SELECT job_no FROM import_job")
        for (job_no,) in cursor.fetchall():
            add_path(initial_relative_path(str(job_no)), "import_job.initial_stage", required=False)
        # 明细文件同样是正式业务数据，不能只保护 relative_path 指向的主表。
        cursor.execute("SELECT value_file_json FROM audit_task WHERE value_file_json IS NOT NULL")
        for (raw,) in cursor.fetchall():
            metadata = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            paths = list((metadata.get("files") or {}).values())
            for batch in metadata.get("batches") or []:
                paths.extend(batch["files"].values())
            if metadata.get("relative_path"):
                paths.append(metadata["relative_path"])
            for relative in paths:
                add_path(str(relative), "audit_task.value_file_json")
        # 只读取诊断引用所在的数组，不把整份规则诊断/业务快照载入清理进程。
        cursor.execute("SELECT JSON_EXTRACT(result_json, '$.verdict_errors') FROM rule_result WHERE result_json IS NOT NULL")
        for (raw,) in cursor.fetchall():
            errors = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            for error in errors or []:
                relative = error.get("diagnostics_path")
                if relative:
                    add_path(str(relative), "rule_result.result_json.verdict_errors[].diagnostics_path")
    return referenced, protected_dirs, required_paths


# 非 READY 产物的占位路径不保证存在；信封是否必需由产物状态单独决定。
_OPTIONAL_SUFFIXES = (".processing", ".submitting", ".pending")


def _is_pending_delete(relative: str) -> bool:
    """删除任务先把目录改名为 .deleting-*；中途终止时唯一副本在这里，不能当孤儿删除。"""
    return any(part.startswith(".deleting-") for part in Path(relative).parts)


def _is_protected(relative: str, protected_dirs: set[str]) -> bool:
    return _is_pending_delete(relative) or any(
        prefix == "." or relative == prefix or relative.startswith(f"{prefix}/")
        for prefix in protected_dirs
    )


def _storage_files(root: Path) -> list[Path]:
    """参与业务引用检查的文件；固定锁身份和事务恢复文件不能按孤儿删除。"""
    return sorted((path for path in root.rglob("*")
                   if path.is_file() and path.relative_to(root).parts[0] != ".locks"
                   and path.suffix not in {".rollback", ".restoring"}), key=lambda item: item.as_posix())


def _relative_of(root: Path, path: Path) -> str:
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise SystemExit(f"拒绝执行：存储路径越界：{path} → {resolved}")
    return resolved.relative_to(root).as_posix()


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def _remove_empty_dirs(root: Path, protected_dirs: set[str]) -> list[Path]:
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
        relative = _relative_of(root, directory)
        if relative == "." or relative == ".locks" or relative.startswith(".locks/") or _is_protected(relative, protected_dirs):
            continue
        try:
            directory.rmdir()
        except OSError as exc:
            if exc.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
                raise
            continue
        removed.append(directory)
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description="删除 data/ 下数据库不再引用的文件（不可恢复）")
    parser.add_argument("--yes", action="store_true", help="确认执行；不加此参数时只列出将要删除的文件")
    parser.add_argument("--offline", action="store_true", help="确认已停止所有访问此存储目录的后端及后台进程，删除期间保持停止")
    args = parser.parse_args()
    if args.yes and not args.offline:
        parser.error("实际删除必须先停止后端及后台进程，再使用 --yes --offline；脚本无法与业务文件写入互斥")

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
        referenced, protected_dirs, required_paths = _referenced_paths(connection, root)
    finally:
        connection.close()

    files = _storage_files(root)
    by_relative = {_relative_of(root, path): path for path in files}
    pending_delete = sorted({
        Path(*Path(relative).parts[:index + 1]).as_posix()
        for relative in by_relative
        for index, part in enumerate(Path(relative).parts)
        if part.startswith(".deleting-")
    })

    orphans: list[Path] = []
    missing: list[tuple[str, str]] = []
    for relative, path in by_relative.items():
        if _is_protected(relative, protected_dirs):
            continue
        if relative not in referenced:
            orphans.append(path)
    for relative, source in referenced.items():
        if relative not in by_relative and relative in required_paths:
            missing.append((relative, source))

    keep_bytes = sum(path.stat().st_size for relative, path in by_relative.items()
                     if relative in referenced or _is_protected(relative, protected_dirs))
    orphan_bytes = sum(path.stat().st_size for path in orphans)

    print()
    print(f"数据库引用路径 {len(referenced)} 条，磁盘文件 {len(files)} 个。")
    print(f"保留 {len(by_relative) - len(orphans)} 个（{_human_size(keep_bytes)}），孤儿 {len(orphans)} 个（{_human_size(orphan_bytes)}）。")

    if missing:
        print()
        print(f"数据库指向但磁盘上不存在的文件 {len(missing)} 个（数据不一致，不按孤儿处理）：")
        for relative, source in sorted(missing):
            print(f"  ! {relative}  ← {source}")

    if pending_delete:
        print()
        print(f"删除任务中途终止留下的暂存目录 {len(pending_delete)} 个（保留，需人工核对后处理）：")
        for relative in pending_delete:
            print(f"  ? {relative}")

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
        print("当前是只读预览，未删除任何文件。确认清单并停止后端及后台进程后执行：")
        print("    python script/prune_data_files.py --yes --offline")
        return 0

    deleted = 0
    for path in orphans:
        _relative_of(root, path)  # 删除前再次核对解析后的根目录边界。
        path.unlink()
        deleted += 1
    removed_dirs = _remove_empty_dirs(root, protected_dirs)

    remaining = _storage_files(root)
    leftover = [path for path in remaining
                if (relative := _relative_of(root, path)) not in referenced
                and not _is_protected(relative, protected_dirs)]
    print()
    print(f"已删除 {deleted} 个文件，回收空目录 {len(removed_dirs)} 个，释放 {_human_size(orphan_bytes)}。")
    if leftover:
        raise SystemExit(f"清理未完成，仍有 {len(leftover)} 个孤儿文件存在")
    print(f"本次检查的 {len(remaining)} 个业务文件均有数据库引用或属于受保护目录；锁文件及事务恢复文件未清理。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
