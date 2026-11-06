"""后台操作与操作日志共用的业务对象类型。

取值必须与 `background_operation.object_type`、`operation_log.object_type` 里
已落库的值完全一致——它们是按对象类型做幂等与互斥的键，改一个字符就会让
「同一对象的活跃操作」查不出来。因此只在这里维护一份，各服务引用它。"""




OBJECT_SCENARIO = "SCENARIO"


OBJECT_PLAN = "AUDIT_PLAN"


OBJECT_AUDIT_TASK = "AUDIT_TASK"


OBJECT_IMPORT_JOB = "IMPORT_JOB"


# AI 辅助生成规则库是独立作业，不与导入作业共用对象类型与幂等身份。
OBJECT_RULEBOOK_GENERATION = "RULEBOOK_GENERATION"


OBJECT_REPORT_TEMPLATE = "REPORT_TEMPLATE"


OBJECT_REPORT = "AUDIT_REPORT"
