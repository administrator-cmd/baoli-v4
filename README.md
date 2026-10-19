# baoli-v4

FastAPI + Vue 3 的规则库驱动审核系统。管理员创建场景、导入规则库并配置草稿，核对后显式发布为生效版本。业务用户生成方案、创建任务、上传材料并查看结论。业务流程与当前限制分别见 [业务逻辑](docs/业务逻辑.md) 和 [状态台账](docs/当前状态与问题台账.md)。

Excel 由 LLM 解析为规则库 JSON，任务执行方案中生效的规则。模型直接抽取、计算或判断，按需调用实际工具；单条无法执行时说明原因，其余继续。程序核对身份、工具调用与产出结构，不校验业务合理性。

审核材料为图片或 PDF。业务规则执行前按流水线处理：8018 原件校验完成后，8010 去章与 8003 原件印章识别并行；去章完成立即进入 PaddleOCR 正文识别。各材料交错推进，OCR 等待不占用下一份材料的本地服务队列。全部已上传材料四项均成功后才开始抽取、计算和校验；模型读取正文与印章产物，不再决定是否调用这些服务。失败依赖分支跳过，其余独立处理收尾后统一阻断任务并保留原因。

执行容器名称与版本是程序常量；RULE_EXECUTION 只关联执行记录。系统不预置业务阶段，不提供节点编排或历史执行兼容。只提供报告下载。

## 本地配置

后端统一读取仓库根目录 `.env`，以 `.env.example` 为配置说明。至少填写 `BAOLI_DATABASE_URL`，数据库名允许 `baoli_v4` 或开发验证库 `baoli_v4_dev`；相对的 `BAOLI_DATA_DIR` 也相对于仓库根目录。环境变量优先于 `.env`，修改配置后需重启后端。

当前处于开发验证阶段，数据库结构直接维护在 `backend/database/schema.sql`。结构变更后清空开发库，再从空库执行该文件；不备份开发数据、不维护 Alembic 或增量升级脚本。建表遇到同名表直接报错；应用启动只读核对必需表是否存在，不校验列、唯一键或生成列表达式，也不自动建库、建表或迁移。修改过生成列（如 `TaskFact.current_fact_scope`）后必须重建空库，否则旧约束会继续生效且启动不会报错。数据库密码中的 URL 特殊字符需按 SQLAlchemy URL 规则编码。

开发库需要重建时，可用 `python script/reset_database.py` 先预览将清空的表，确认后再加 `--yes` 执行；随后重新运行 `backend/database/schema.sql`。这会删除业务数据，不用于需要保留数据的库。

上传限制分别由 `BAOLI_MAX_RULEBOOK_BYTES`（默认 30 MiB）和 `BAOLI_MAX_DOCUMENT_BYTES`（默认 128 MiB）配置。页面从 `GET /api/system/upload-policy` 读取策略，后端写入时最终校验；外部服务和反向代理的限制独立且未经确认。

## 外部依赖

| 配置 | 用途 |
| --- | --- |
| `BAOLI_LLM_BASEURL`、`BAOLI_LLM_APIKEY`、`BAOLI_LLM_MODEL_NAME` | 规则转换、字段抽取、业务计算和校验 |
| `BAOLI_SERVICE_8018_URL` | 材料类型目录与核对 |
| `BAOLI_SERVICE_8010_URL` | 文件处理 |
| `BAOLI_SEAL_SERVICE_URL` | 印章识别 |
| `BAOLI_OCR_SERVICE_URL`、`BAOLI_OCR_TOKEN`、`BAOLI_OCR_MODEL` | OCR 提交与查询 |

外部服务可暂不配置以查看已有任务和系统状态；依赖该服务的操作会明确报错。“已配置，未验证”仅表示配置齐备，不表示服务已连通。OCR 提交状态未知时不会自动重发。

## 启动

在已安装 `backend/requirements.txt` 的 Conda `dd` 环境中启动后端：

```powershell
conda activate dd
cd backend
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

另开终端启动前端：

```powershell
cd frontend
npm run dev
```

前端开发代理指向 `http://127.0.0.1:8000`。默认首页为审核任务列表；服务配置详情在“系统管理 / 服务状态”。

## 代码格式与静态检查

后端保留 `api`、`services`、`repositories` 三层，每层内部按场景 `scenario/`、方案 `plan/`、审核 `audit/`、材料 `material/`、报告 `report/`、导入 `imports/`、系统 `system/` 分业务目录，再拆分具体职责文件。仓储目录按实际需要建立，共用能力归入 `shared/`。代码导航与职责说明见[后端分层约定](docs/后端分层约定.md)。

编辑器基础格式由 `.editorconfig` 统一。Python 使用根目录 `pyproject.toml` 中的 Ruff 配置，开发依赖位于 `backend/requirements-dev.txt`；前端使用 ESLint、Prettier 和现有 TypeScript 配置。前端新增 `npm run lint`、`npm run typecheck`、`npm run format:check` 和 `npm run format` 命令。Python 对应 `ruff check backend/app`、`ruff format --check backend/app` 和 `ruff format backend/app`。

这些命令按需手动执行，未加入启动流程或自动修改提交的钩子。本次整理未执行测试、类型检查、Lint、构建或全仓格式化；配置接入不表示历史代码已经全部符合规则。

## 当前边界

规则库导入包含两轮 LLM 请求：第一轮读取 Excel 的全部非空 Sheet，第二轮整理第一轮 JSON。第二轮结果存入数据库，原 Excel 保留文件路径，两轮原始响应保存在 data/llm_responses 下；不提供选表或工作表绑定。

场景草稿和方案保存在 config_json，发布版本和任务冻结执行配置。任务字段值、计算结果及业务结论写入本次任务的 CSV，程序先查数据库索引，再按记录 ID 从 CSV 取值。数据库还保存状态、执行诊断、证据和报告；当前原始模型响应及输入摘要仍含业务值副本，具体问题见[状态台账](docs/当前状态与问题台账.md)。

取值 CSV 使用中文业务列，字段/计算取值与审核结论分表，依据、产出、缺项逐行保存；文件结构、维护生效范围和旧三列文件转换见[维护说明](docs/任务取值CSV维护说明.md)。旧文件未转换时明确报错，不混写新旧结构。

审核图无持久化 checkpoint，一次后台调用执行生效规则。人工审核暂不启用，字段纠错保留；不能把任务执行结束等同于全部业务检查通过。

任务图固定为八步，依赖计划中的规则由程序串行执行，每条独立运行模型与工具循环。规则数量与模型轮次不累计到 LangGraph 图步数；BAOLI_LLM_TOOL_MAX_ROUNDS 按单条规则计数，默认 12，设为 0 关闭轮次限制。批次结束汇总真实结果和未完成原因。

报告正文保存和下载支持 Markdown、纯文本及 HTML，PDF、Word、Excel 文件生成未实现。系统不提供列表 CSV、中间值或上传原材料的下载、预览入口。

结构变更直接维护 schema.sql，实际数据库是否已同步需以用户重建情况为准。当前没有身份认证、角色或数据范围控制，完整业务链路尚未完成当前版本验收。详细说明见[执行契约](docs/规则库与审核执行契约.md)和[状态台账](docs/当前状态与问题台账.md)。
