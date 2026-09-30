"""文件处理检查（02_文件处理规则）的配置事实与确定性判定。

这个模块是**叶子模块**：不导入 manifest、node_rulebook、capabilities 或任何 services。
它同时承载两类内容，且必须放在一起，否则「导入期校验」与「运行期判定」会各写一份
单位表/格式表而漂移：

- 配置事实：有哪些检查项目、各自依赖哪种产物、允许哪些合格要求、中文标签、
  哪些已实现。导入期与能力目录都引用这里。
- 确定性判定：已实现的检查项按真实输入判定（见各 `check_*` 函数）。

未实现的检查项（图像清晰度方差、页码连续、有效签章、骑缝章完整）缺少已确认的判定依据，
本模块**不实现**成默认判断：调用方按能力缺口返回「不可判定」，绝不因为文件处理成功
就当作通过。缺少标准值、单位或口径时同样明确报错，不用默认值补齐。

判定只基于已经持久化的真实数据（上传记录、OCR 产物页结构），不调用模型、
不看文件名猜内容、不把数量当成质量结论。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from ..common.exceptions import BusinessError


# 检查项目。与 02_文件处理规则 的「检查项目」下拉取值一一对应。
CHECK_FILE_FORMAT = "FILE_FORMAT"
CHECK_FILE_SIZE = "FILE_SIZE"
CHECK_FILE_DUPLICATE = "FILE_DUPLICATE"
CHECK_IMAGE_CLARITY = "IMAGE_CLARITY"
CHECK_PAGE_INTEGRITY = "PAGE_INTEGRITY"
CHECK_SEAL_PARTY_A = "SEAL_PARTY_A"
CHECK_SEAL_PARTY_B = "SEAL_PARTY_B"
CHECK_SEAL_CROSS_PAGE = "SEAL_CROSS_PAGE"

# 已具备**真实输入**、由代码确定性判定的检查项目。
#   判定依据是已持久化的真实数据：原始上传的格式与字节数、同任务内上传的内容哈希。
IMPLEMENTED_QUALITY_ITEMS = frozenset({CHECK_FILE_FORMAT, CHECK_FILE_SIZE, CHECK_FILE_DUPLICATE})

# 检查项目依赖的产物类型。它是「这条检查能不能执行」的事实前提：
# 产物缺失时明确不可判定，不默认通过。
QUALITY_ITEM_ARTIFACT: dict[str, str] = {
    CHECK_FILE_FORMAT: "MATERIAL_CLASSIFY",
    CHECK_FILE_SIZE: "MATERIAL_CLASSIFY",
    CHECK_FILE_DUPLICATE: "MATERIAL_CLASSIFY",
    CHECK_PAGE_INTEGRITY: "OCR",
    CHECK_SEAL_PARTY_A: "SEAL",
    CHECK_SEAL_PARTY_B: "SEAL",
    CHECK_SEAL_CROSS_PAGE: "SEAL",
    CHECK_IMAGE_CLARITY: "DOCUMENT_PARSE",
}

# 每个检查项目允许的「合格要求」。项目与判断方式必须匹配，不匹配属于配置错误。
QUALITY_ITEM_REQUIREMENTS: dict[str, frozenset[str]] = {
    CHECK_FILE_FORMAT: frozenset({"IN"}),
    CHECK_FILE_SIZE: frozenset({"LTE"}),
    CHECK_FILE_DUPLICATE: frozenset({"NOT_DUPLICATE"}),
    CHECK_IMAGE_CLARITY: frozenset({"GTE"}),
    CHECK_PAGE_INTEGRITY: frozenset({"CONTINUOUS_NO_MISSING"}),
    CHECK_SEAL_PARTY_A: frozenset({"VALID_SEAL_EXISTS"}),
    CHECK_SEAL_PARTY_B: frozenset({"VALID_SEAL_EXISTS"}),
    CHECK_SEAL_CROSS_PAGE: frozenset({"COMPLETE"}),
}

QUALITY_ITEM_LABELS: dict[str, str] = {
    CHECK_FILE_FORMAT: "文件格式",
    CHECK_FILE_SIZE: "文件大小",
    CHECK_FILE_DUPLICATE: "文件内容",
    CHECK_IMAGE_CLARITY: "图像清晰度方差",
    CHECK_PAGE_INTEGRITY: "页码",
    CHECK_SEAL_PARTY_A: "甲方签章",
    CHECK_SEAL_PARTY_B: "乙方签章",
    CHECK_SEAL_CROSS_PAGE: "骑缝章",
}

# 「合格要求」列取值的中文口径。它与检查项目共同构成这条检查的判据，
# 因此与检查项目放在同一处，由导入期与结果展示共用，避免各写一套而漂移。
QUALITY_REQUIREMENT_LABELS: dict[str, str] = {
    "IN": "属于",
    "LTE": "不大于",
    "NOT_DUPLICATE": "不重复",
    "GTE": "不小于",
    "CONTINUOUS_NO_MISSING": "连续且无缺页",
    "VALID_SEAL_EXISTS": "有效签章存在",
    "COMPLETE": "完整",
}

# 已实现 + 声明了但未实现 = 当前已知的全部检查项目。
# 不在这个集合里的取值不是「证据不足」，而是未知能力：必须显式报错。
KNOWN_QUALITY_ITEMS = frozenset(QUALITY_ITEM_ARTIFACT)

# 缺口的机器码。只有**声明了但未实现**的检查项才构成能力缺口。
GAP_CRITERION_UNCONFIRMED = "QUALITY_CRITERION_UNCONFIRMED"
# 印章识别只返回印章数量与位置，不能证明盖章正确——它即使将来实现判定，
# 也只能形成“是否检出印章”的证据，需要与上面一类分开表达。
GAP_SEAL_COUNT_IS_NOT_QUALITY = "SEAL_COUNT_IS_NOT_SEAL_QUALITY"

# 未实现的检查项统一归结为「判定口径未确认」；印章类单独使用印章口径的缺口码。
SEAL_QUALITY_ITEMS = frozenset({CHECK_SEAL_PARTY_A, CHECK_SEAL_PARTY_B, CHECK_SEAL_CROSS_PAGE})

# 字节换算：MB 指 1,000,000 字节，MiB 指 1,048,576 字节，不混用（填写规范明确）。
SIZE_UNITS = {
    "B": Decimal(1),
    "KB": Decimal(1000),
    "KIB": Decimal(1024),
    "MB": Decimal(1000) ** 2,
    "MIB": Decimal(1024) ** 2,
    "GB": Decimal(1000) ** 3,
    "GIB": Decimal(1024) ** 3,
}

# 格式标准值里的写法 → 该格式可接受的文件后缀与 content_type。
# 只把标准值列出的格式视为匹配：不按“看起来像 PDF”猜格式。
FORMAT_SUFFIXES = {
    "PDF": {".pdf"},
    "JPG": {".jpg", ".jpeg"},
    "JPEG": {".jpg", ".jpeg"},
    "PNG": {".png"},
    "TIF": {".tif", ".tiff"},
    "TIFF": {".tif", ".tiff"},
    "BMP": {".bmp"},
    "DOC": {".doc"},
    "DOCX": {".docx"},
    "XLS": {".xls"},
    "XLSX": {".xlsx"},
}
FORMAT_CONTENT_TYPES = {
    "PDF": {"application/pdf"},
    "JPG": {"image/jpeg"},
    "JPEG": {"image/jpeg"},
    "PNG": {"image/png"},
    "TIF": {"image/tiff"},
    "TIFF": {"image/tiff"},
    "BMP": {"image/bmp"},
}
# 后缀族：一个后缀可能同时属于多个格式名（.jpg 既是 JPG 也是 JPEG）。
# 冲突判定按“族”比较，而不是按单个格式名，否则 JPG/JPEG 这类同义写法会互相误判。
FORMAT_EXTENSIONS = {ext for exts in FORMAT_SUFFIXES.values() for ext in exts}
# 客户端可能上报的通用/占位内容类型：它们不代表真实的文件内容类型，
# 因此不能用来判定格式，也不能作为“格式相符”的证据。口径必须明确说明。
GENERIC_CONTENT_TYPES = {
    "",
    "application/octet-stream",
    "binary/octet-stream",
    "application/unknown",
    "unknown",
    "application/x-download",
}


def gap_code_for(check_item: str) -> str:
    """未实现的检查项对应的缺口码。已实现项没有缺口。"""
    if check_item in SEAL_QUALITY_ITEMS:
        return GAP_SEAL_COUNT_IS_NOT_QUALITY
    return GAP_CRITERION_UNCONFIRMED


def is_implemented(check_item: str) -> bool:
    """该检查项目是否已有真实判定实现；未知取值返回 False（不当作已实现）。"""
    return check_item in IMPLEMENTED_QUALITY_ITEMS


def label_of(check_item: str) -> str:
    """检查项目的中文名；未知取值原样返回编码。"""
    return QUALITY_ITEM_LABELS.get(check_item, check_item)


def _failed(code: str, message: str, details: dict[str, Any] | None = None) -> BusinessError:
    return BusinessError(code, message, status_code=500, details=details or {})


def parse_size_limit(standard_value: str | None, unit: str | None) -> Decimal:
    """把「标准值 + 单位」换算成字节数上限。

    缺少标准值、单位不受支持或标准值不是非负数值都明确报错：宁可让配置问题暴露，
    也不用默认上限放行文件。导入期与运行期共用这一个换算口径。
    """
    if standard_value is None or not str(standard_value).strip():
        raise _failed("RULE_QUALITY_STANDARD_VALUE_REQUIRED", "文件大小检查缺少「标准值」，无法判定")
    raw = str(standard_value).strip()
    try:
        number = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise _failed("RULE_QUALITY_STANDARD_VALUE_INVALID", f"文件大小检查的「标准值」“{raw}”不是有效数值") from exc
    if not number.is_finite() or number < 0:
        raise _failed("RULE_QUALITY_STANDARD_VALUE_INVALID", f"文件大小检查的「标准值」“{raw}”必须是不小于 0 的数值")
    if unit is None or not str(unit).strip():
        raise _failed("RULE_QUALITY_UNIT_REQUIRED", "文件大小检查缺少「单位」，无法把标准值换算成字节数")
    factor = SIZE_UNITS.get(str(unit).strip().upper())
    if factor is None:
        raise _failed(
            "RULE_QUALITY_UNIT_UNSUPPORTED",
            f"文件大小检查的「单位」“{unit}”无法识别，只支持 {'、'.join(sorted(SIZE_UNITS))}",
        )
    return number * factor


def parse_format_set(standard_value: str | None) -> set[str]:
    """把「标准值」里的格式清单解析成受支持的格式名集合。"""
    if standard_value is None or not str(standard_value).strip():
        raise _failed("RULE_QUALITY_STANDARD_VALUE_REQUIRED", "文件格式检查缺少「标准值」，无法判定")
    entries = [
        item.strip().upper()
        for item in str(standard_value).replace(",", "；").split("；")
        if item.strip()
    ]
    if not entries:
        raise _failed("RULE_QUALITY_STANDARD_VALUE_REQUIRED", "文件格式检查的「标准值」为空，无法判定")
    unsupported = [item for item in entries if item not in FORMAT_SUFFIXES]
    if unsupported:
        raise _failed(
            "RULE_QUALITY_FORMAT_UNSUPPORTED",
            "文件格式检查的「标准值」包含系统无法核对的格式：" + "、".join(unsupported),
        )
    return set(entries)


def check_file_format(upload: Any, *, standard_value: str | None) -> dict[str, Any]:
    """文件格式：上传文件的真实后缀属于标准值列出的格式集合，且不与已声明的内容类型冲突。

    口径说明（必须如实表达，不能用兜底话术掩盖）：

    - 后缀是**客户端上报的文件名**，不是服务端对内容的鉴定。
    - `content_type` 同样是客户端上报的声明值，系统不读取文件字节做类型嗅探。
    - 因此本项能确定的是「声明一致 / 声明冲突 / 声明不足」，不能声称做过真实内容鉴定：
      后缀与声明明确冲突时判不合格；后缀无法用于核对时不能因此判通过；
      内容类型缺失或为通用占位时通过，但结论里必须说明未做内容鉴定。
    """
    allowed = parse_format_set(standard_value)
    filename = str(getattr(upload, "original_filename", "") or "")
    lowered = filename.lower()
    suffix = next(
        (ext for ext in sorted(FORMAT_EXTENSIONS) if lowered.endswith(ext)),
        "",
    )
    allowed_extensions = {
        ext for name in allowed for ext in FORMAT_SUFFIXES.get(name, set())
    }
    allowed_content_types = {
        value for name in allowed for value in FORMAT_CONTENT_TYPES.get(name, set())
    }
    content_type = str(getattr(upload, "content_type", "") or "").strip().lower()
    declared_type = content_type not in GENERIC_CONTENT_TYPES
    suffix_known = bool(suffix) and suffix in FORMAT_EXTENSIONS
    suffix_allowed = bool(suffix) and suffix in allowed_extensions
    type_matches_suffix = bool(suffix) and content_type in {
        value for name in FORMAT_SUFFIXES if suffix in FORMAT_SUFFIXES[name]
        for value in FORMAT_CONTENT_TYPES.get(name, set())
    }
    type_allowed = content_type in allowed_content_types

    if not suffix_allowed:
        detail = f"文件“{filename}”的格式不在允许范围（{'、'.join(sorted(allowed))}）"
        if declared_type and not type_allowed:
            detail += f"，且声明的内容类型“{content_type}”也不匹配"
        return {"status": "ABNORMAL", "message": detail}

    if declared_type and not type_matches_suffix and not type_allowed:
        # 声明的后缀与声明的内容类型互相矛盾，二者都不符合允许格式：
        # 这是明确冲突，不能因为后缀看起来对就直接判通过。
        return {
            "status": "ABNORMAL",
            "message": (
                f"文件“{filename}”声明的后缀“{suffix}”与声明的内容类型“{content_type}”互相矛盾，"
                f"且内容类型不属于允许格式（{'、'.join(sorted(allowed))}）"
            ),
            "declared_content_type": content_type,
        }

    if not suffix_known:
        # 后缀无法用于核对（例如无后缀或未知后缀），只能靠声明的内容类型：
        # 声明相符也不等于已验证，如实记为无法确定。
        return {
            "status": "EVIDENCE_INSUFFICIENT",
            "message": (
                f"文件“{filename}”没有可用作核对的已知后缀，只有声明的内容类型“{content_type}”；"
                "系统不读取文件内容做类型鉴定，无法确认格式"
            ),
            "declared_content_type": content_type,
        }

    if not declared_type:
        return {
            "status": "PASSED",
            "message": (
                f"文件格式“{suffix}”在允许范围内；上传未声明可核对的内容类型"
                f"（{'未声明' if not content_type else f'通用值 {content_type}'}），本项按声明后缀判定，未经内容鉴定"
            ),
            "declared_content_type": content_type or None,
        }
    if type_allowed and not type_matches_suffix:
        # 后缀与内容类型都落在允许集合内，只是彼此不是同一格式族（例如 .jpg 声明 image/tiff）。
        # 不判不合格，但必须指出二者不一致，由业务判断。
        return {
            "status": "EVIDENCE_INSUFFICIENT",
            "message": (
                f"文件“{filename}”的声明后缀“{suffix}”与声明的内容类型“{content_type}”不属于同一格式，"
                "二者都在允许范围内但不一致，无法确认格式"
            ),
            "declared_content_type": content_type,
        }
    return {
        "status": "PASSED",
        "message": (
            f"文件格式“{suffix}”在允许范围内，声明的内容类型“{content_type}”与之一致（按声明值判定，未经内容鉴定）"
        ),
        "declared_content_type": content_type,
    }


def check_file_size(upload: Any, *, standard_value: str | None, unit: str | None) -> dict[str, Any]:
    """文件大小：真实字节数不大于标准值换算后的字节数。"""
    limit = parse_size_limit(standard_value, unit)
    size = getattr(upload, "size_bytes", None)
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise _failed("RULE_QUALITY_SIZE_UNKNOWN", "上传记录没有可用的文件字节数，无法判定文件大小")
    actual = Decimal(size)
    passed = actual <= limit
    return {
        "status": "PASSED" if passed else "ABNORMAL",
        "message": (
            f"文件大小 {size} 字节在允许上限内（{standard_value}{unit}）"
            if passed
            else f"文件大小 {size} 字节超过允许上限（{standard_value}{unit}）"
        ),
        "actual_bytes": size,
        "limit_bytes": str(limit),
    }


def check_file_duplicate(
    *,
    upload_id: int,
    sha256: str,
    other_uploads: list[dict[str, Any]],
) -> dict[str, Any]:
    """文件内容：同一任务内当前上传文件的内容哈希不得重复。

    重复范围按填写规范仍需业务明确；这里只比对**同一任务内当前上传版本**之间，
    不跨任务、不回看历史版本，也不按文件名相似度判断。

    比对对象按上传 ID 排除自身：按哈希排除会把「自己」也当成重复项，
    从而让任何一份单独上传的文件恒判不合格，也会掩盖真正与其他材料位重复的情况。
    """
    if not sha256:
        raise _failed("RULE_QUALITY_HASH_MISSING", "上传记录缺少内容哈希，无法判定文件是否重复")
    duplicates = [
        str(item.get("label") or item.get("material_key") or item.get("upload_no") or "")
        for item in other_uploads
        if item.get("id") != upload_id and item.get("sha256") == sha256
    ]
    labels = sorted({label for label in duplicates if label})
    if labels:
        return {
            "status": "ABNORMAL",
            "message": f"文件内容与同任务内其他上传文件重复：{'、'.join(labels)}",
            "duplicates": labels,
        }
    return {"status": "PASSED", "message": "文件内容与同任务内其他上传文件不重复"}
