"""业务对象的对外编号与场景内稳定 key。

两者语义不同，不能混用：

- `public_no`：给人和页面看的**对外编号**（任务号、上传号、证据号……）。它只要求
  不重复，不承载任何业务含义，因此用随机值生成，不能由它反推业务对象。
- `stable_key`：场景内**稳定引用**（材料 key、字段 key、规则 key）。它必须由
  「命名空间 + 来源编号」确定性派生：同一份来源重复编译要得到同一个 key，
  否则已发布配置与历史任务会指向不同的对象。中文名称、文件名和 Excel 行号都不能
  充当来源编号——它们会随改名和重排而变。

放在 domain 是因为 manifest 与 node_rulebook 在编译期就要派生稳定 key；
它们不能依赖 services 层。
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
