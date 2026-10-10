# baoli-v4

FastAPI + Vue 3 的规则库驱动审核系统。管理员创建场景、导入规则库并配置草稿，核对后显式发布为生效版本。业务用户生成方案、创建任务、上传材料并查看结论。业务流程与当前限制分别见 [业务逻辑](docs/业务逻辑.md) 和 [状态台账](docs/当前状态与问题台账.md)。

业务计算与字段校验交给配置的 LLM：Excel 定义计算方法与判断标准，场景和方案保存 JSON，审核从 CSV 读取本次字段值并保存 AI 的结果、输入引用和计算说明。程序负责引用、结果格式及流程。旧四则公式引擎和程序区间判定已删除；历史 PROGRAM 或未声明执行方式的计算配置需重新解析发布，已有结果保留。本轮仅静态实现，未运行真实模型或审核验证。

固定 Excel 模板配置及其下载、表头匹配、旧格式转换路径已删除。`backend/config/node_profile.json` 只保存节点能力类型和默认依赖，场景字段、规则和计算由 LLM 根据实际规则库生成；版本统一导出完整业务 JSON，停用来源使用模型增量解析、预览和确认追加。

## 本地配置

后端统一读取仓库根目录 `.env`，以 `.env.example` 为配置说明。至少填写 `BAOLI_DATABASE_URL`，数据库名允许 `baoli_v4` 或开发验证库 `baoli_v4_dev`；相对的 `BAOLI_DATA_DIR` 也相对于仓库根目录。环境变量优先于 `.env`，修改配置后需重启后端。

当前处于开发验证阶段，数据库结构直接维护在 `backend/database/schema.sql`。结构变更后清空开发库，再从空库执行该文件；不备份开发数据、不维护 Alembic 或增量升级脚本。建表遇到同名表直接报错，应用启动只读核对实际结构，不自动建库、建表或迁移。数据库密码中的 URL 特殊字符需按 SQLAlchemy URL 规则编码。

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
| `BAOLI_EMBEDDING_MODEL_PATH` | 本地印章主体检索模型目录 |

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

## 当前边界

Excel 是各场景自己的规则库，LLM 理解完整单元格并生成动态字段与规则 JSON，不要求统一表头。场景草稿及方案业务定义集中存入 `config_json`；审核任务抽取值、计算结果和规则结论写入 CSV，后续执行从 CSV 读取，数据库保留证据关联与结果索引。保存场景只更新草稿，发布后生成不可变版本；任务仍冻结方案配置。执行能力的具体范围见[规则库与审核执行契约](docs/规则库与审核执行契约.md)。

本轮结构变更新增 scenario_draft.config_json、audit_plan.config_json、audit_task.intermediate_csv_json，并移除旧配置明细表及分散的计算/上下文列。应用不会迁移旧库，需由用户按项目约定重建；本轮未操作数据库。

场景身份表已移除逻辑删除列；工作表导入表只保存实际工作表身份、选择状态和来源内容，已移除固定模板角色、表级节点及表头行绑定。前后端与结构检查同步使用此结构。

LangGraph 保留 `checkpoints`、`checkpoint_blobs`、`checkpoint_writes` 三张运行状态表，用于人工确认和复核后的流程恢复；不创建 `checkpoint_migrations`，不维护迁移版本记录，也不调用 saver.setup()。发布版本、任务执行配置及工具输入记录属于现有业务执行数据，不是开发库备份。

规则库模型完整返回在 JSON 解析及候选校验前原样保存于 `data/llm_responses/<operation_no>/response_<唯一编号>.json`，`background_operation.raw_response_path` 独立提交文件索引，失败时保留，供调试解析错误。该列已包含在统一 `schema.sql` 中。本轮未执行数据库脚本，旧失败返回无法补回。

当前没有身份认证、角色或数据范围控制。大量新改动仅完成静态实现，真实业务材料、外部服务、浏览器及数据库链路尚未完整复验；不能把旧验证结果当作当前系统验收，详情见[状态台账](docs/当前状态与问题台账.md)。
