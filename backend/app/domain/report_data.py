"""报告业务数据引用：字段事实、计算结果和规则结论，不限定业务场景。"""

from typing import Any


def report_data_refs(section: dict[str, Any]) -> list[str]:
    """业务引用与明确绑定的字段共同构成章节数据要求，保留配置顺序。"""
    return list(dict.fromkeys([
        *(section.get("data_refs") or []), *(section.get("field_keys") or []),
    ]))


def report_format_support(formats: list[str], content_type: str) -> list[dict[str, Any]]:
    """按真实正文列出已生成与未生成格式，不把格式声明当作文件产出。"""
    aliases = {"markdown": "text/markdown", "md": "text/markdown",
               "text": "text/plain", "txt": "text/plain", "html": "text/html"}
    requested = list(dict.fromkeys(str(value).strip().lower() for value in formats))
    if not any(aliases.get(value, value) == content_type for value in requested):
        requested.append(content_type)
    return [{"format": value, "supported": aliases.get(value, value) == content_type,
             "message": "已生成正文" if aliases.get(value, value) == content_type else "本次未生成该格式文件"}
            for value in requested]
