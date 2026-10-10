"""业务对象的对外编号与场景内稳定 key。

两者语义不同：`public_no` 是给人看的对外编号（任务号、上传号、证据号……），只要求
不重复、不承载业务含义，因此用随机值生成；`stable_key` 是场景内稳定引用（材料 key、
字段 key、规则 key），必须由「命名空间 + 来源编号」确定性派生，同一份来源重复编译要
得到同一个 key。中文名称、文件名和 Excel 行号不能充当来源编号——它们会随改名和重排而变。

放在 domain 是因为 manifest 在编译期就要派生稳定 key。
"""

import hashlib
import secrets


def public_no(prefix: str) -> str:
    """生成一个对外编号，形如 `TASK_1a2b3c4d5e6f7a8b`。"""
    return f"{prefix}_{secrets.token_hex(8)}"


def stable_key(prefix: str, namespace: str, source_ref: str) -> str:
    """由命名空间与来源编号确定性派生稳定 key。

    同一 (prefix, namespace, source_ref) 必须永远得到同一个 key；换来源编号即换 key，
    这是「来源变了，引用就要跟着变」的显式表达，不做任何兼容映射。
    """
    digest = hashlib.sha256(f"{namespace}:{source_ref}".encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"
