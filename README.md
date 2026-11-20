# baoli-v4

FastAPI + Vue 3 的规则库驱动审核系统。管理员创建场景、导入规则库并配置草稿，核对后发布为生效版本。业务用户生成方案、创建任务、上传材料并查看结论。业务流程与当前限制分别见 [业务逻辑](docs/业务逻辑.md) 和 [当前状态与问题](docs/当前状态与问题.md)。

Excel 由 LLM 解析为规则库 JSON，任务执行方案中生效的规则。模型直接抽取、计算或判断，按需调用实际工具；单条无法执行时说明原因，其余继续。程序核对身份、工具调用与产出结构，不校验业务合理性。

审核材料为图片或 PDF，由程序完成校验、去章、OCR 和印章识别后再执行规则；处理流程见[业务逻辑](docs/业务逻辑.md)。

RULE_EXECUTION 只关联执行记录，系统不提供业务节点编排。文档职责与阅读入口见 [docs/README](docs/README.md)。

## 本地配置

后端统一读取仓库根目录 `.env`，以 `.env.example` 为配置说明。至少填写 `BAOLI_DATABASE_URL`，数据库名允许 `baoli_v4` 或开发验证库 `baoli_v4_dev`；相对的 `BAOLI_DATA_DIR` 也相对于仓库根目录。环境变量优先于 `.env`，修改配置后需重启后端。

当前处于开发验证阶段，数据库结构由 Alembic 迁移管理，`backend/resource/schema.sql` 是当前完整结构。新库执行该文件后，在 `backend/` 下执行 `python -m alembic stamp head`；已有库按当前版本执行 `python -m alembic upgrade head`。后续结构变更只改实体并生成迁移，再同步更新 `schema.sql`，不再通过删库重建。应用启动只读核对必需业务表及实体声明的列，不校验唯一键或生成列表达式，也不自动建库、建表或迁移。数据库密码中的 URL 特殊字符需按 SQLAlchemy URL 规则编码。迁移命令见[数据库迁移](backend/migrations/README)。

开发库需要重建时，可用 `python script/reset_database.py` 先预览将清空的表，确认后再加 `--yes` 执行；随后重新运行 `backend/resource/schema.sql`，并在 `backend/` 下执行 `python -m alembic stamp head`。这会删除业务数据，不用于需要保留数据的库。

上传限制分别由 `BAOLI_MAX_RULEBOOK_BYTES`（默认 30 MiB）和 `BAOLI_MAX_DOCUMENT_BYTES`（默认 128 MiB）配置。页面从 `GET /api/system/upload-policy` 读取策略，后端写入时最终校验；外部服务和反向代理的限制独立且未经确认。

## 文件定位

`data` 按业务对象组织。目录名由英文用途、UTC 创建时间（`YYYYMMDDTHHMMSSZ`）和完整业务编号组成，可按页面或日志中的编号查找。

| 模块 | 定位文件 |
| --- | --- |
| `rulebook_generations/generation_<时间>_<GEN编号>` | 上传原件与 `exports/v0001/` 等版本导出 |
| `rulebook_imports/import_<时间>_<IMP编号>` | 上传原件、`checkpoints/initial.json`、`operations/parse_<时间>_<OP编号>/llm_responses/` 下的各阶段响应 |
| `audit_tasks/audit_<时间>_<TASK编号>` | `materials/<材料编号>/<上传编号>/` 中的原件及其 `artifacts/`，`values/current/` 中的业务取值，`diagnostics/` 中的执行诊断 |
| `report_templates/template_<时间>_<模板编号>` | 报告模板原件；任务报告正文保存在 `audit_report`，下载时由正文生成 |

按操作编号查询 `GET /api/operations/{operation_no}`，`response_files` 按写入顺序给出阶段、路径与时间。`0010_operation_llm_response` 只升级结构，不搬迁历史目录或回填响应，采用新布局时使用空验证数据集。

## 外部服务配置

| 配置 | 用途 |
| --- | --- |
| `BAOLI_LLM_BASEURL`、`BAOLI_LLM_MODEL_NAME`、`BAOLI_LLM_APIKEY`（可选） | 规则转换、字段抽取、业务计算和校验；密钥留空时不发送认证头 |
| `BAOLI_SERVICE_8018_URL` | 材料类型目录与核对 |
| `BAOLI_SERVICE_8010_URL` | 文件处理 |
| `BAOLI_SEAL_SERVICE_URL` | 印章识别 |
| `BAOLI_OCR_SERVICE_URL`、`BAOLI_OCR_TOKEN`、`BAOLI_OCR_MODEL` | OCR 提交与查询 |
| `BAOLI_OCR_MAX_CONCURRENT_JOBS` | 每个后端进程共享的 OCR 并发上限，默认 5 |

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

后端按实体组织在 `app/modules/<业务>/<实体>/`，每个实体包含 controller、dto、schema、model、crud 和 service；数据库操作只写在 crud，service 负责业务编排。共用能力在 `app/services/shared/`，路由汇总在 `app/api/routes.py`。代码导航与职责说明见[后端分层约定](docs/后端分层约定.md)。

编辑器基础格式由 `.editorconfig` 统一。Python 使用根目录 `pyproject.toml` 中的 Ruff 配置，Ruff 与其他后端依赖统一列在 `backend/requirements.txt`；前端使用 ESLint、Prettier 和现有 TypeScript 配置。前端新增 `npm run lint`、`npm run typecheck`、`npm run format:check` 和 `npm run format` 命令。Python 对应 `ruff check backend/app`、`ruff format --check backend/app` 和 `ruff format backend/app`。

这些命令按需手动执行，未加入启动流程或自动修改提交的钩子；配置接入不表示历史代码已经全部符合规则，已有检查范围见[当前状态与问题](docs/当前状态与问题.md)。

前端请求统一通过 `frontend/src/api/http.ts` 的 Axios 实例发送，业务接口用 `data` 传入对象或 `FormData`，由 Axios 序列化并设置请求内容类型。普通请求与文件下载超时为 60 秒，带进度上传为 300 秒；统一解析响应信封和 `ApiError`，下载错误 JSON 不作为文件保存。超时或取消后仍按原有后台操作机制查询真实状态，不自动重发写请求。

任务详情页的结果读取与字段核对分别由 `useAuditTaskResults`、`useAuditFieldReview` 维护。前者负责风险、报告与执行记录的按需加载及重置，后者负责保存、确认和受理结果未知时的查询；页面负责展示与路由协调。切换任务或组件卸载后，旧请求不得回写新对象；同一结果的重复读取只采纳最新响应。

## 当前边界

- 导入使用两轮 LLM，请求失败不以不完整结果替代；第二轮可按检查点重试。任务冻结配置，后续场景或方案修改不影响已有任务。
- 新审核任务的业务取值采用格式版本 7：`values/current/values.csv` 只存可复用的当前字段值和计算结果，字段原文、候选和历史取值保存在 `values/history.json`。规则业务结论与引用通过原结果记录返回，不复制完整上游证据。版本 6 的旧任务继续使用四表；参见 [任务取值 CSV 维护说明](docs/任务取值CSV维护说明.md)。
- 删除证据表的结构已包含在当前 `schema.sql` 中，新库执行 `schema.sql` 后 stamp head 即可；后续变更走 Alembic 迁移并同步更新 `schema.sql`。代码不自动建表或迁移旧证据。旧版任务 CSV 不能直接按版本 6 读取，未对真实任务文件作转换。
- 固定审核图无持久化 checkpoint；支持按已提交规则结果继续中断批次，被中断的单条从头执行。重新尝试则创建新批次、全部重跑；两者语义见[恢复与继续执行](docs/规则库与审核执行契约.md#恢复与继续执行)。
- 符合条件的字段抽取可批量执行，其他规则按依赖计划串行运行。`BAOLI_LLM_TOOL_MAX_ROUNDS` 默认 12，0 关闭轮次限制；规则数量和模型轮次不占用图步数。
- 可下载规则库 Excel 原件和 Markdown、纯文本、HTML 报告；未实现 PDF、Word、Excel 报告文件生成，不提供审核材料原件、列表 CSV 或中间取值下载。
- 字段抽取完成后停在人工核对等待点（`WAITING_FIELD_REVIEW`），确认字段值后才继续计算、检查、风险与报告；通用人工审核等待点仍不启用，字段纠错保留；当前没有身份认证、角色或数据范围控制。当前问题与验收边界统一见[当前状态与问题](docs/当前状态与问题.md)，不能把执行结束或局部验证视为完整业务验收。
