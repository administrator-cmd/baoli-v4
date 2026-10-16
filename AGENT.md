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

数据库初始化与清库工具 `script/reset_database.py` 的用法见根目录 `README.md`。结构变更直接修改 `backend/database/schema.sql`；结构校验实现在 `backend/app/runtime/schema_check.py`。

仅在用户明确要求时运行测试、类型检查、构建、浏览器、服务、数据库、模型或外部接口；未授权时只做静态核对。


数据库：
在用户确认数据库的表结构需要新增的时候 直接修改 schema.sql 然后由用户来进行删库重新建表


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

RULE_EXECUTION 仅为执行记录技术容器，不是可编排业务节点。rule_key 是当前内部唯一身份，rule_code 是原始业务编号；任务版本、文件版本、规则结果身份、证据来源属于当前执行功能。执行期不维护独立事实快照、工具调用账本或检索索引，也没有 LangGraph checkpoint。业务取值（字段值、计算结果、规则结论）的读取来源是本次任务的 CSV；数据库保留索引、状态、执行诊断、证据和报告。当前诊断仍包含模型原始响应和输入取值，不能称为业务值完全不落库，具体问题见状态台账。工具按当前真实目录提供，不存逐规则授权矩阵。

以 docs/规则库与审核执行契约.md 和 docs/当前状态与问题台账.md 为当前设计与实现依据。
