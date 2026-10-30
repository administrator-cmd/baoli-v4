# baoli-v4

FastAPI + Vue 3 的规则库驱动审核系统。管理员创建场景、导入规则库并配置草稿，核对后显式发布为生效版本。业务用户生成方案、创建任务、上传材料并查看结论。业务流程与当前限制分别见 [业务逻辑](docs/业务逻辑.md) 和 [状态台账](docs/当前状态与问题台账.md)。

Excel 由 LLM 解析为规则库 JSON，任务执行方案中生效的规则。模型直接抽取、计算或判断，按需调用实际工具；单条无法执行时说明原因，其余继续。程序核对身份、工具调用与产出结构，不校验业务合理性。

审核材料为图片或 PDF，由程序完成校验、去章、OCR 和印章识别后再执行规则；处理流程见[业务逻辑](docs/业务逻辑.md)。

RULE_EXECUTION 只关联执行记录，系统不提供业务节点编排。文档职责与阅读入口见 [docs/README](docs/README.md)。

## 本地配置

后端统一读取仓库根目录 `.env`，以 `.env.example` 为配置说明。至少填写 `BAOLI_DATABASE_URL`，数据库名允许 `baoli_v4` 或开发验证库 `baoli_v4_dev`；相对的 `BAOLI_DATA_DIR` 也相对于仓库根目录。环境变量优先于 `.env`，修改配置后需重启后端。

当前处于开发验证阶段，数据库结构由 Alembic 迁移管理，`backend/resource/schema.sql` 是当前完整结构。新库执行该文件后，在 `backend/` 下执行 `python -m alembic stamp head`；已有库按当前版本执行 `python -m alembic upgrade head`。后续结构变更只改实体并生成迁移，再同步更新 `schema.sql`，不再通过删库重建。应用启动只读核对必需业务表及实体声明的列，不校验唯一键或生成列表达式，也不自动建库、建表或迁移。数据库密码中的 URL 特殊字符需按 SQLAlchemy URL 规则编码。迁移命令见[数据库迁移](backend/migrations/README)。

开发库需要重建时，可用 `python script/reset_database.py` 先预览将清空的表，确认后再加 `--yes` 执行；随后重新运行 `backend/resource/schema.sql`，并在 `backend/` 下执行 `python -m alembic stamp head`。这会删除业务数据，不用于需要保留数据的库。

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

后端保留 `api`、`services` 两层，各自内部按场景 `scenario/`、方案 `plan/`、审核 `audit/`、材料 `material/`、报告 `report/`、导入 `imports/`、系统 `system/` 分业务目录，再拆分具体职责文件。共用能力归入 `services/shared/`；不设 repository 层，查询直接写在对应的 service 模块。代码导航与职责说明见[后端分层约定](docs/后端分层约定.md)。

编辑器基础格式由 `.editorconfig` 统一。Python 使用根目录 `pyproject.toml` 中的 Ruff 配置，开发依赖位于 `backend/requirements-dev.txt`；前端使用 ESLint、Prettier 和现有 TypeScript 配置。前端新增 `npm run lint`、`npm run typecheck`、`npm run format:check` 和 `npm run format` 命令。Python 对应 `ruff check backend/app`、`ruff format --check backend/app` 和 `ruff format backend/app`。

这些命令按需手动执行，未加入启动流程或自动修改提交的钩子；配置接入不表示历史代码已经全部符合规则，已有检查范围见[状态台账](docs/当前状态与问题台账.md)。

## 当前边界

- 导入使用两轮 LLM，请求失败不以不完整结果替代；第二轮可按检查点重试。任务冻结配置，后续场景或方案修改不影响已有任务。
- 业务取值采用格式版本 6 的任务四表 CSV。字段原文依据直接保存在 `values.csv` 的“原文依据”JSON 列；规则在 `rule_results.csv` 同列保存直接引文或当时字段/规则记录的引用，不复制完整上游证据。引用对象只含上传版本、产物、原文和可选字段属性，原文来源随任务详情返回；没有独立 Evidence 表、证据编号或查询接口。数据库保留索引、状态、结论摘要、执行诊断和报告；审核成功的原始模型响应不落盘，结论校验失败的响应写入任务诊断目录并记入 `verdict_errors`。四表已按数据库与 CSV 的单一来源原则收缩：数据库状态副本、只写不读的上下文列和重复结论列已删除。
- 删除证据表的结构已包含在当前 `schema.sql` 中，新库执行 `schema.sql` 后 stamp head 即可；后续变更走 Alembic 迁移并同步更新 `schema.sql`。代码不自动建表或迁移旧证据。旧版任务 CSV 不能直接按版本 6 读取，未对真实任务文件作转换。
- 固定审核图无持久化 checkpoint；支持按已提交规则结果继续中断批次，被中断的单条从头执行。重新尝试则创建新批次、全部重跑；两者语义见[恢复说明](docs/规则库与审核执行契约.md#恢复与重新尝试)。
- 符合条件的字段抽取可批量执行，其他规则按依赖计划串行运行。`BAOLI_LLM_TOOL_MAX_ROUNDS` 默认 12，0 关闭轮次限制；规则数量和模型轮次不占用图步数。
- 可下载规则库 Excel 原件和 Markdown、纯文本、HTML 报告；未实现 PDF、Word、Excel 报告文件生成，不提供审核材料原件、列表 CSV 或中间取值下载。
- 字段抽取完成后停在人工核对等待点（`WAITING_FIELD_REVIEW`），确认字段值后才继续计算、检查、风险与报告；通用人工审核等待点仍不启用，字段纠错保留；当前没有身份认证、角色或数据范围控制。当前问题与验收边界统一见[状态台账](docs/当前状态与问题台账.md)，不能把执行结束或局部验证视为完整业务验收。
