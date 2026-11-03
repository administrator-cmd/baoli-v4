# 开发环境

## 技术栈

- 后端：Python 3.12、FastAPI、SQLAlchemy、Pydantic、MySQL 8.4。
- 编排与处理：LangGraph（无持久化 checkpoint）、openpyxl、httpx、jsonschema；已移除本地向量模型与检索链。
- 前端：Vue 3、TypeScript、Vite、Vue Router、Element Plus、lucide-vue-next。
- 准确依赖版本分别见 `backend/requirements.txt` 和 `frontend/package.json`，不要以本文代替依赖清单。

## 本机工具

- 开发分支：`dev`；`main` 为稳定分支。操作前以 `git branch --show-current` 核对实际分支。
- 现在又多了一个 dev2 的测试开发分支
- Python 环境：Conda `dd`，解释器 `C:\Users\29550\miniconda3\envs\dd\python.exe`。
- Node/npm：本机记录为 Node 23.11.1、npm 10.9.2；以实际命令输出为准。
- MySQL 工具目录：`D:\develop\mysql-8.4.11\bin\`。
- 后端从仓库根目录读取 `.env`；启动时只读校验数据库结构，不自动迁移。

## 常用命令

后端：

```powershell
conda activate dd
cd backend
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

前端：

```powershell
cd frontend
npm run dev
```

数据库初始化与清库工具 `script/reset_database.py` 的用法见根目录 `README.md`。结构变更走 Alembic：修改实体后生成并人工审查迁移，命令见 `backend/migrations/README`；`backend/resource/schema.sql` 是当前完整结构，空库执行后 `stamp head`。结构校验实现在 `backend/app/runtime/schema_check.py`。

仅在用户明确要求时运行测试、类型检查、构建、浏览器、服务、数据库、模型或外部接口；未授权时只做静态核对。

后端按实体组织在 `app/modules/<业务>/<实体>/`，每个实体包含 `controller.py`、`dto.py`、`schema.py`、`model.py`、`crud.py` 和 `service/`。路由汇总在 `app/api/routes.py`；配置、数据库会话、CRUD 基类、ORM 基类和响应基类放在 `app/core/`；跨业务共用能力放在 `app/services/shared/`。数据库操作只写在 crud，service 不直接查库。包目录不写 `__init__.py`，调用方从具体模块导入，不做符号重导出。目录导航见 `docs/后端分层约定.md`。


数据库：
表结构变更使用 Alembic 迁移：先改实体，再在 `backend/` 下执行 `python -m alembic revision --autogenerate -m "..."`，人工审查生成列、唯一键和复合外键后 `upgrade head`，最后从当前库重新导出并更新 `schema.sql`。迁移文件在 `backend/migrations/versions/`，基线为 `0001_baseline`，当前 head 为 `0005_field_review_confirmation`；`schema.sql` 与 head 保持同步。


系统业务：
现在设想的是 该系统面向两类用户使用，第一类是管理员 管理员主要负责创建场景 第二类是 用户 用户主要负责把管理员的场景转化为自己的方案，再转化的过程中可用进行适量的业务场景定制，比如说禁止几个
规则 减少几个字段的抽取

本地测试文件在
D:\29550\OneDrive\桌面\Agent\baoli_material\baoli-v4 下面的pdf excel 等都是测试文件

前端数据展示：
1 没有信息价值的长编码不要展示，1 用户不懂系统逻辑和系统设计，他肯定看不懂这些没有意义的编码 
2 前端展示数据尽量横向展示 不要纵向展示，纵向展示 信息获取的体验差，用户观感差

## 当前执行设计

Excel → LLM 规则库 JSON → 用户方案中的生效规则。LLM 直接抽取、计算和判断，按需调用真实工具；单条无法执行时保存原因，其余继续。不要添加业务合理性校验、预置业务阶段、历史业务协议兼容或模拟成功路径。

RULE_EXECUTION 仅为执行记录技术容器，不是可编排业务节点。rule_key 是当前内部唯一身份，rule_code 保留来源业务编号；任务版本、文件版本、规则结果身份、原文来源属于当前执行功能。执行期不维护独立事实快照、工具调用账本或检索索引，也没有 LangGraph checkpoint；中断恢复基于已提交的规则结果，不恢复模型消息。业务取值和原文依据保存在任务 CSV，数据库保留索引、状态、结论摘要、执行诊断和报告，不再维护独立 Evidence 表或证据编号；审核原始模型响应当前不落盘，新输入摘要只保留引用信息。工具按当前真实目录提供，不存逐规则授权矩阵。

执行计划按规则声明的依赖分成抽取段与核对后段：抽取跑完停在字段核对等待点（任务与批次同为 WAITING_FIELD_REVIEW，不是失败），人工确认字段后才执行计算、检查、风险和报告。核对修改只追加字段事实版本与人工记录，不失效批次、不重抽其他字段；确认记录绑定执行批次、输入修订与字段版本摘要。抽取段结束后不再重跑抽取规则。通用人工裁决流程仍不启用。

按 docs/README.md 选择与任务相关的文档；执行语义以 docs/规则库与审核执行契约.md 为依据，未解决问题和验证边界见 docs/当前状态与问题.md。文档维护当前结论，不追加逐轮操作日志，也不把讨论中的方案写成已实现能力。
