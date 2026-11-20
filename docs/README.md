# 开发文档入口

先按改动范围选一份主文档。业务流程看概览，动执行代码看执行契约，动文件读写看取值协议。启动命令、环境配置和数据库操作见[仓库 README](../README.md)；本地开发约定见 [AGENT.md](../AGENT.md)。

| 要做的事 | 主文档 | 到哪里核对实现 |
| --- | --- | --- |
| 理解场景、版本、方案、任务的关系 | [业务逻辑](业务逻辑.md) | [冻结配置](../backend/app/domain/configuration/manifest.py) |
| 修改 Excel 导入、规则执行、核对或续跑 | [规则库与审核执行契约](规则库与审核执行契约.md) | [导入作业](../backend/app/modules/imports/job/)、[审核执行](../backend/app/modules/audit/execution/) |
| 看节点顺序、等待点与规则循环 | [工作流说明](资料审核系统工作流.md)、[图册第 12、13 页](diagrams/资料审核系统图册.html) | [任务图与逐规则执行器](../backend/app/graphs/) |
| 修改当前值、证据、历史读写或清理 | [任务取值 CSV 维护说明](任务取值CSV维护说明.md) | [取值格式分派](../backend/app/modules/audit/field/service/value_store.py) |
| 修改表、外键或迁移 | [ER 与存储边界](资料审核系统ER图.md)、[迁移说明](../backend/migrations/README) | [目标 DDL](../backend/resource/schema.sql)、[迁移链](../backend/migrations/versions/) |
| 修改人读规则库的 AI 编制、编辑或导出 | [AI 辅助生成规则库](AI辅助生成规则库.md)；[产品约束与交付标准](AI辅助生成规则库_开发执行计划.md) | [编制服务](../backend/app/modules/imports/generation/) |
| 修改报告模板上传、风格冻结或降级 | [报告模板](报告模板.md) | [报告模板模块](../backend/app/modules/audit/report_template/) |
| 新增后端模块、调整依赖或事务 | [后端分层约定](后端分层约定.md) | [后端模块](../backend/app/modules/) |
| 了解已知限制、缺哪些运行证据 | [当前状态与问题](当前状态与问题.md) | 具体样本、版本和验证范围见对应专题 |

## 维护口径

- 同一条行为约定只在所属专题详细说明。概览和图册用于导航；旧数据格式只在兼容章节说明。
- 源码与文档不一致时，先定位实现和适用版本。当前行为看本分支源码，目标库结构看迁移与 DDL；实际部署参数、实际数据库版本及模型效果需要对应环境的证据。
- 区分“产品要求”“源码已实现”“已运行验证”。历史测试通过只覆盖当时的版本与样本；计划和验收用例本身不代表功能已经交付。
