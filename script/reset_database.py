#!/usr/bin/env python
"""清空已配置的 baoli_v4 或 baoli_v4_dev 数据库：删除库内全部数据表。

用途：本地反复验证导入/审核流程时，不必逐张手工删表。

安全约束（与后端 core/database.py 保持一致）：
- 只接受 mysql+pymysql，且库名必须是 baoli_v4 或 baoli_v4_dev；其它连接目标直接拒绝。
- 默认只列出将要删除的表，不做任何修改；必须显式加 --yes 才会真正执行。
- 只 DROP 库内的表，绝不 DROP DATABASE，也绝不创建任何表。

用法（在项目根目录或任意位置均可）：

    python script/reset_database.py            # 只看会删哪些表
    python script/reset_database.py --yes      # 真正执行

删完之后数据库是空的，需要重新执行唯一建表脚本：

    backend/resource/schema.sql

注意：本脚本只清数据库。data/ 目录下已经上传的文件不会被删除，
清库后会变成孤儿文件，需要时请自行清理。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = PROJECT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import pymysql  # noqa: E402
from sqlalchemy.engine import URL  # noqa: E402

from app.core.config import get_settings, validated_database_url  # noqa: E402


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


def _existing_tables(connection: pymysql.Connection) -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE()")
        return sorted(str(row[0]) for row in cursor.fetchall())


def _drop_tables(connection: pymysql.Connection, tables: list[str]) -> None:
    with connection.cursor() as cursor:
        # 先解除外键检查，避免删表顺序影响；删完必须恢复。
        cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
        try:
            for table in tables:
                safe_name = table.replace("`", "``")
                cursor.execute(f"DROP TABLE IF EXISTS `{safe_name}`")
        finally:
            cursor.execute("SET FOREIGN_KEY_CHECKS = 1")


def main() -> int:
    parser = argparse.ArgumentParser(description="删除当前配置的 baoli_v4 / baoli_v4_dev 库内全部数据表（不可恢复）")
    parser.add_argument("--yes", action="store_true", help="确认执行；不加此参数时只列出将要删除的表")
    args = parser.parse_args()

    url = _validated_database_url()
    print(f"目标数据库：{url.host or '127.0.0.1'}:{url.port or 3306}/{url.database}（用户 {url.username}）")

    try:
        connection = pymysql.connect(**_connection_kwargs(url))
    except pymysql.MySQLError as exc:
        raise SystemExit(f"数据库连接失败：{exc}") from exc

    try:
        tables = _existing_tables(connection)
        if not tables:
            print("库内没有任何表，无需清理。")
            return 0

        print(f"库内共有 {len(tables)} 张表：")
        for table in tables:
            print(f"  - {table}")

        if not args.yes:
            print()
            print("当前是只读预览，未做任何修改。确认要删除以上全部表时，请加 --yes 重新执行：")
            print("    python script/reset_database.py --yes")
            return 0

        _drop_tables(connection, tables)

        remaining = _existing_tables(connection)
        if remaining:
            raise SystemExit(f"清理未完成，仍存在 {len(remaining)} 张表：{', '.join(remaining)}")
        print()
        print(f"已删除 {len(tables)} 张表，库内现在是空的。")
        print("请重新执行唯一的建表脚本：")
        print("  backend/resource/schema.sql")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
