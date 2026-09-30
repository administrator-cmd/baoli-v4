"""工具执行上下文：一次工具调用的运行期信封。

与 `ToolSpec` 的关系
--------------------
`ToolSpec`（`domain/tool_catalog.py`）声明**工具需要什么**：可挂哪些节点、
适用哪些场景、需要哪些产物、参数长什么样。
本模块声明**这次调用实际提供了什么**：哪个任务、哪条规则实例、
哪一份事实快照、调用方是谁、材料范围到哪里。
`services/tool_service` 把两者对照，不匹配就明确拒绝。

为什么数据库会话在这里、却绝不能进 State
------------------------------------------
`database_session` 是运行期依赖，和 `graphs/audit_graph.py` 的
`AuditGraphContext["db"]` 同一性质：它随一次调用存在，**绝不写入图 state、
checkpoint 或任何持久化记录**。

同理，`HTTP client`、密钥与不可序列化的业务对象都不进本结构。

与「事实快照」的绑定
--------------------
`fact_snapshot_ref` 与 `fact_snapshot_hash` 标识**本次调用依据的是哪一版事实**。
规则评估开始时冻结输入事实集合，评估期间不能每次工具调用都读「最新事实」——
否则同一条规则的两次工具调用可能基于不同的数。快照身份随结果一起留存，
事后才能回答「这个结论是按哪一版输入算出来的」。

工具策略版本
------------
`tool_policy_version` 与目录身份是两个维度：后者（`tool_catalog_version` /
`tool_catalog_hash`）是任务冻结时那一版**工具目录**的版本与内容指纹，前者是
**本次发布允许 Agent 在什么范围内选工具**。策略版本只能与 `TOOL_POLICY_VERSION`
比较；拿目录版本当策略版本比，会让一次与策略无关的目录升级把既有任务的工具调用
全部拒掉，而真正放宽策略时又拦不住旧任务。目录身份与当前目录不一致时，需要
**真正重新执行**的那次调用会显式报版本不兼容（见 `services/tool_service`），
已成功的旧调用仍复用账本结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolExecutionContext:
    """一次工具调用的运行期上下文。

    所有字段都可以为空：任务尚未落库时没有 `task_id`，旧任务没有快照身份。
    为空表示「这一项本次没有」，不表示「按默认值处理」——
    需要它的校验会明确报出缺什么，不自行补默认值。
    """

    # 业务身份。它们进持久化记录，用于回答「这次调用属于哪个任务、哪条规则实例」。
    task_id: int | None = None
    # 规则评估批次。一次评估内的事实集合是冻结的，工具调用都属于同一个批次。
    evaluation_id: str | None = None
    # 规则实例。同一条规则定义作用于不同材料组合时是不同实例，结果不能互相覆盖。
    rule_instance_id: str | None = None
    scene_code: str | None = None
    node_code: str | None = None

    # 材料范围：本次调用允许触及的材料位。模型请求的输入超出这个范围即拒绝。
    # 空元组表示「本次未限定材料范围」，由调用方明确决定，不在这里默认放开或收紧。
    material_refs: tuple[str, ...] = ()

    # 事实快照身份。它把工具结果绑到当时冻结的输入版本上。
    fact_snapshot_ref: str | None = None
    fact_snapshot_hash: str | None = None

    # 工具目录身份：任务冻结时那一版工具目录的版本与**内容指纹**。
    # 两者都取自冻结记录（manifest / 快照），不由当前常量代替：用当前目录身份
    # 覆盖旧任务的冻结身份，会让同一个 tool_call_id 在恢复时被判成协议冲突。
    # 指纹由目录内容算出，因此能发现「版本号没改但 Schema 变了」。
    tool_catalog_version: str | None = None
    tool_catalog_hash: str | None = None

    # 工具策略版本：**本次发布允许 Agent 在什么范围内选工具**。
    # 它与目录版本是两件事，只能与 `TOOL_POLICY_VERSION` 比较（见 services.tool_service）。
    tool_policy_version: str | None = None

    # 调用方身份，用于审计追溯（例如节点执行、Agent 循环、人工重试）。
    # 它只做记录，不参与权限判断——权限由场景、节点、材料范围与策略版本决定。
    caller_identity: str | None = None

    # 运行期依赖：**不进 State、不进 checkpoint、不进持久化记录**。
    # 需要查库的工具（读事实、读产物、读材料）用它；纯计算工具不需要它。
    # 用 Any 声明是为了让本模块保持叶子：domain 层不导入 SQLAlchemy。
    database_session: Any = field(default=None, compare=False, hash=False, repr=False)

    @property
    def scoped_materials(self) -> frozenset[str]:
        """本次限定的材料范围。空集表示未限定。"""
        return frozenset(item for item in self.material_refs if item)
