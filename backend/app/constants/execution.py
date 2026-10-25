"""审核执行的跨模块固定标识：节点、事实种类、来源与主体范围。

这些值同时出现在编译、持久化和执行上下文中；集中声明后，新增节点或来源时只改一处。
"""

# 规则执行容器节点。它只是执行批次的技术归属，不是可编排的业务阶段。
NODE_RULE_EXECUTION = "RULE_EXECUTION"

# TaskFact 的事实种类。
FACT_KIND_EXTRACTED = "EXTRACTED"
FACT_KIND_DERIVED = "DERIVED"

# 人工纠错与文档抽取是两类不同来源：重跑失效只清模型产出，人工值必须保留。
ORIGIN_DOCUMENT_EXTRACTION = "DOCUMENT_EXTRACTION"
ORIGIN_HUMAN_CORRECTION = "HUMAN_CORRECTION"

# 事实主体范围的缺省值；具体业务主体由字段或计算声明。
ENTITY_SCOPE_SELF = "SELF"

# 规则由模型执行（没有本地算子）时的 operator 标识。
OPERATOR_LLM_EXECUTE = "LLM_EXECUTE"
