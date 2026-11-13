---
name: baoli-v4-dev
description: baoli-v4 全栈开发指南，覆盖 FastAPI 后端分层、Vue 前端、规则库编制与导入、审核执行、事务及任务 CSV 契约。在本仓库新增功能、修改接口或页面、排查问题、准备迁移和验证时使用。
---

# baoli-v4 全栈开发指南

在本仓库开发时，用本指南定位代码和关键约束，再按任务阅读对应文档。具体开发环境与操作限制见 [AGENT.md](AGENT.md)，业务执行语义以 [规则库与审核执行契约](docs/规则库与审核执行契约.md) 为依据；实现现状与验收范围见 [当前状态与问题](docs/当前状态与问题.md)。发现文档与源码不一致时先核对，不能把讨论方案或历史验证结论当作当前能力。

## 1. 工程地图

| 位置 | 职责与技术 |
| --- | --- |
| `backend/` | Python 3.12、FastAPI、SQLAlchemy 2、Pydantic 2、MySQL 8.4、Alembic |
| `backend/app/main.py` | 应用创建、路由注册、日志、启动检查与中断标记 |
| `backend/app/api/routes.py` | 集中汇总各模块 router，默认统一挂载到 `/api` |
| `backend/app/modules/<业务>/<实体>/` | 按实体组织 controller、dto、schema、model、crud、service |
| `backend/app/core/` | 配置、数据库会话、事务回调、CRUD/ORM/响应基类 |
| `backend/app/domain/<职责>/` | 按职责组织配置编译、业务契约与纯判定；不访问数据库 |
| `backend/app/graphs/` | LangGraph 固定任务图与单条规则执行器，无持久化 checkpoint |
| `backend/app/integrations/` | LLM 网关、请求节流、文档处理、OCR 等外部服务适配 |
| `backend/app/operators/`、`backend/app/services/shared/` | 本地工具算子，以及存储、文件锁、命令等共用能力 |
| `backend/app/common/`、`backend/app/middleware/`、`backend/app/runtime/` | 响应与异常、请求上下文、启动结构检查 |
| `backend/migrations/`、`backend/resource/schema.sql` | 增量迁移与当前完整数据库结构 |
| `frontend/` | Vue 3、TypeScript、Vite、Vue Router、Element Plus、lucide-vue-next；npm 管理 |
| `backend/test/`、`frontend/test/` | Python unittest 与 Node 测试文件，执行前确认具体用例依赖 |
| `script/`、`tmp/`、`output/` | 长期维护脚本、临时脚本、本地验收产物；后两者不入库 |
| `data/`、`logs/backend.log` | 本地业务文件与后端滚动日志，不入库 |

准确版本以 [后端依赖](backend/requirements.txt)、[开发依赖](backend/requirements-dev.txt)、[前端依赖与脚本](frontend/package.json) 为准。

### 后端业务入口

以下路径均相对 `backend/app/modules/`：

| 位置 | 修改时关注 |
| --- | --- |
| `imports/generation/` | AI 编制、对话修改、版本历史、冻结导出与转入场景导入 |
| `imports/job/` | Excel 两轮解析、检查点、提案、写入场景草稿 |
| `scenario/template/`、`scenario/draft/` | 场景生命周期、发布版本、材料/字段/计算/规则/章节配置 |
| `plan/plan/`、`plan/config/` | 基于场景版本生成方案、启停配置、创建任务与冻结配置 |
| `audit/task/`、`audit/artifact/` | 任务生命周期、上传版本、材料质量限制与处理产物 |
| `audit/execution/` | 命令、继续执行、批次状态及 `service/flow/` 执行编排 |
| `audit/field/`、`audit/rule/` | 字段事实、人工核对、CSV 取值、规则工具循环与结论校验 |
| `audit/report/`、`audit/report_template/` | 报告、风险结果、报告风格模板 |
| `system/operation/`、`system/console/` | 持久化后台操作、操作日志、服务状态与目录查询 |

### 前端入口（frontend/src）

| 位置 | 职责 |
| --- | --- |
| `api/`、`api/contracts/` | 按业务划分请求函数与 TypeScript 接口契约 |
| `api/http.ts` | 统一请求、上传、文件响应、错误解析、操作查询与轮询 |
| `views/`、`components/` | 业务页面、公共组件及按业务分组的子组件 |
| `composables/` | 请求状态、后台操作追踪、分页、材料处理等复用逻辑 |
| `constants/`、`utils/` | 状态词表、分页与业务常量，以及展示和转换函数 |
| `router/index.ts`、`layouts/AppShell.vue` | 静态路由、页面元信息与菜单分组 |
| `style.css`、`main.ts` | 公共布局样式、Element Plus 与应用初始化 |

## 2. 本地配置与命令

**命令是操作参考，不代表自动执行授权。** 遵循 `AGENT.md`：仅在用户明确要求时运行测试、类型检查、构建、浏览器、服务、数据库、模型或外部接口；否则只做静态核对，并说明验证边界。已获得本次任务所需授权时直接在该范围内执行。

后端使用 Conda `dd`，解释器位置见 `AGENT.md`。前端使用现有 `package-lock.json`，不另引入包管理器或锁文件。

首次准备环境时，在根目录按 [.env.example](.env.example) 创建 `.env`，填写真实配置；不覆盖已有配置，不提交凭据。后端总是读取根目录 `.env`，进程环境变量优先，相对 `BAOLI_DATA_DIR` 也以根目录为基准；修改后需重启。

后端启动（从仓库根目录开始）：

```powershell
conda activate dd
python -m pip install -r backend/requirements-dev.txt
cd backend
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

前端另开终端（从仓库根目录开始）：

```powershell
cd frontend
npm ci
npm run dev
```

Vite 默认开发端口为 5173，实际以终端输出为准；`/api` 代理到 `http://127.0.0.1:8000`。`VITE_API_BASE_URL` 是前端请求前缀，默认 `/api`。修改后端 `BAOLI_API_PREFIX` 时，同时核对前端请求基址、Vite 代理和跨域配置。

获得相应验证授权后，按改动范围选择：

| 工作目录 | 命令 | 用途 |
| --- | --- | --- |
| 根目录，已激活 `dd` | `python -m ruff check backend/app` | Python 静态规则与导入检查 |
| 根目录，已激活 `dd` | `python -m ruff format --check backend/app` | Python 格式检查 |
| `backend/`，已激活 `dd` | `python -m unittest discover -s test -p test_field_review_fixes.py -v` | 字段核对相关回归示例；其他改动选择对应文件 |
| `frontend/` | `npm run typecheck` | Vue/TypeScript 类型检查 |
| `frontend/` | `npm run lint`、`npm run format:check` | ESLint 与 Prettier 检查 |
| `frontend/` | `node --test test/generationReview.test.mjs` | 编制相关 Node 回归示例 |
| `frontend/` | `npm run build` | 类型检查并构建到 `frontend/dist/` |

仓库没有 `npm test` 脚本，后端测试也不预设使用 pytest。部分测试会访问 MySQL，先看文件头、fixture 和启用条件；不为跑测试自动设置数据库测试开关。历史代码可能存在格式问题，定向检查优先，不顺手全仓格式化。

## 3. 后端分层与接口约定

完整说明见 [后端分层约定](docs/后端分层约定.md)。依赖方向为 `controller → service → crud → model`。

- `controller.py` 处理 HTTP 入参、依赖注入与响应；复杂实体可以拆成多个按职责命名的 controller，并在 `app/api/routes.py` 注册。
- `dto.py` 放 `XxxRequest`，`schema.py` 放 `XxxResponse`；列表筛选与分页参数复用 `app/schema/query/`。
- SQL 只写在 `crud.py`。Service 编排业务，跨实体读写调用对应 CRUD，不直接执行 SQL；CRUD 查询、写入和 flush，不代理 commit/rollback。
- 配置条目使用普通数据类，序列化到 `config_json`；不要把 JSON 内的材料、字段等配置项声明为虚拟 SQL 表。
- `ConfigItem` 只作读取视图；固定字段修改使用 `services/shared/configuration.py` 的 `update_material/field/rule/section`，允许字段在 `domain/configuration/config_changes.py` 声明。编译派生规则通过 `update_generated_rule` 同步身份与来源，动态业务元数据通过 `update_extensions` 保存；引用检查沿用所属业务 Service。
- 执行计划、核对边界、规则结果和图/单规则状态复用 `domain/audit/audit_state.py`；调度结构有明确类型，冻结业务配置、模型消息与取值内容保持动态。主执行与续跑共用类型，不增加持久化 checkpoint。
- `domain` 不导入 `modules`，不读取数据库；外部服务适配沿已有 service/端口调用，graph 调用 flow 函数，不把业务堆进图定义。
- domain 按 `configuration/`、`generation/`、`imports/`、`audit/`、`materials/`、`tools/`、`shared/` 归属；完整文件导航见 [domain 文件归属](docs/后端分层约定.md#domain-文件归属)。新文件按业务职责放置，不在根目录平铺，不增设杂项工具文件；跨业务复用仍从所属模块导入，不因调用方多而移入 `shared/`。搬迁同步源码、测试、脚本和文档引用，不保留旧路径转发层。
- 包目录不新增 `__init__.py`，从具体模块导入，不做包级符号重导出；不额外搭建空接口、repository 层或纯转发包装。
- JSON 接口统一使用 `app/common/response.py` 的 `ResponseSchema[T]` 和 `ok(data, request)`，信封为 `{code, message, data, request_id, success}`。文件响应、流与 204 按现有独立契约处理。
- 业务错误抛 `BusinessError(code, message, status_code=..., details=...)`，复用统一异常处理；保留真实错误，不吞异常或构造成功结果。敏感配置、原始请求与响应不放进客户端错误详情。
- 分页统一 `page`、`page_size`，返回 `total`、`page`、`page_size`、`items`；总数来自服务端同一筛选范围，不用当前页长度代替。

### 事务与文件联动

- 普通请求由 `get_db` / `request_session` 成功后提交、失败时回滚，业务 Service 和 CRUD 不提前结束外层事务。
- 后台执行编排层按工作单元提交：命令认领、远端调用前占位、进度、逐条结果、等待点和失败记录。修改时先定位已有提交边界，不把普通业务方法改成到处提交。
- 复用 `app/core/transactions.py` 的事务回调；后台线程在请求提交成功后启动。目录删除先暂存，回滚前恢复、提交后清理；上传回滚后按真实引用清理。
- 唯一键冲突用保存点隔离，不撤销整个请求。CSV 的锁、文件快照和恢复继续参与 Session 事件及保存点，不另建一套提交机制。

## 4. 前端页面与交互约定

- API 放 `api/<业务>.ts`，类型放 `api/contracts/<业务>.ts`；复用 `request<T>`，其返回值已经是信封中的 `data`。下载用 `requestBlob` 等已有封装，不能把错误 JSON 保存成文件。
- 服务端返回的任务、节点、字段统一使用 `AuditTask/AuditNode/AuditField`，不在 API 文件中追加平行 View 类型。请求参数也在契约目录声明；纯页面派生状态留在组件或 composable。
- Vue API 显式 import，沿用 `<script setup lang="ts">` 与 `@/` 别名；组件与类型从现有实现复用，不假定有自动导入、动态菜单或其他项目的 CRUD 套件。
- 新页面核对 `router/index.ts` 的 `meta.title/group/menu`；需要菜单入口时同步 `AppShell.vue`，详情页归属现有菜单。
- 请求状态复用 `useRequestRunner`，后台操作用 `useOperationTracker`，列表优先复用 `usePagedList`；复用 `PageHeader`、`FailureNotice`、`StateBlock`、`StatusBadge`、`OperationPanel`、`DrawerPanel` 等组件。
- 后台受理只代表开始处理。按 `operation_no` 查询真实状态后展示成功、失败或中断，不用计时器制造进度；查询失败只提供再次查询，不自动重新提交命令。
- 超时、取消或断网不能证明写入失败，先查询真实状态。同一次动作重发保留原幂等键，明确发起新尝试才生成新键；切换对象用 tracker 的 `reset()`，防止旧轮询回写新页面。
- 页面卸载清理轮询、监听和计时器；避免重复注册。刷新数据时保留必要的草稿、查看位置与未保存提示，旧响应不能覆盖新对象。
- 状态展示复用 `constants/` 现有词表和展示函数，不在多个页面分别猜测状态含义。没有数据、加载中、失败、正常等待分别表达。
- 面向业务用户显示名称、正文、真实状态和有效操作。无信息价值的内部长编码不占主视图；数量与操作优先横向排列，小屏按需换行。
- 表格操作复用 `table-actions` 并留足列宽，状态标签完整显示；长正文限行预览，详情或弹窗能读取全文。避免堆砌灰色小字、重复状态、教学步骤和技术流程说明。
- 同类表格中，名称和业务语义一致的字段跨页面使用统一默认列宽，复用 `frontend/src/constants/tableColumns.ts`，不在各页面重复写数值。新增或调整列宽时检索相关表格并同步修改；固定宽度字段统一 `width`，需要伸展的长文本统一 `min-width` 基准。确因布局或用途不同需要例外时，在代码中注明原因；操作列仍按实际按钮数量留宽，不因同名强行统一。当前抽取策略为 128px，产物来源为 120px。

## 5. 业务边界与执行契约

主链路：`场景草稿 → 发布版本 → 用户方案 → 冻结配置的任务 → 材料处理与字段抽取 → 人工核对 → 后续审核与报告`。

### 规则库编制与导入

- **AI 编制面向人阅读。** 依据业务 Excel 整理中文表格、完整条款、真实关联和待明确口径；不要求用户先建立系统字段绑定、五类执行定义或工具授权。具体维护约束见 [编制功能开发约束](docs/AI辅助生成规则库_开发执行计划.md)。
- 业务已有自身编号沿用原值和规律，规则、材料确实缺号时才由模型补充；不能按排序重新编号或另造一套业务编号。编号栏按实际内容区分自身身份（`code_role=IDENTITY`）、对象引用（`REFERENCE`）和批次分组（`GROUP`），显式语义优先于表头推断；引用号和分组号可重复，无独立编号的辅助资料、历史记录保留空 `business_code`，不强行补号。自身编号默认在文档内唯一，仅原文明示各表独立编号时用 `business_code_scope=SHEET`，不能为绕过重复检查缩小范围。首次编制、助手编辑、人工保存、页面显示与导出保持同一语义，不按表名或编号前缀排除整表。`rule_key` 是执行内部身份，`rule_code` 等保留来源编号，展示优先业务编号。
- 编制按现有固定工作流、有限编辑操作、独立复核和版本冲突控制实现。首轮候选稿经局部补修仍有复核疑点时，明确列为待核对事项并原子保存；对话修改仍须通过复核。交流与不可变修订分开保存，询问或没有实际修改不增版本，失败保留成功文档和历史导出。
- 用户补充真实业务答案后，由 AI 同步相关正文、备注、关联与事项；不增设手工问题登记入口，不把只保存答案或状态当成修复完成。
- **导入解析负责转换执行配置。** 全部非空工作表进入现有两轮 LLM 解析；第一轮识别对象，第二轮整理引用和完整配置。编制进入导入需要用户显式选择场景，不自动写草稿或发布。
- 区分对象定义、真实业务动作与辅助资料；说明、字典、映射保存为 `supporting_content`，不因表名或编号造规则；其中独立业务要求仍保留。缺业务口径如实表达，不编造阈值、算法或自行停用。
- 导入页面面向业务人员，只展示五类业务配置，不单列辅助资料页签。资料已有明确要求由解析落实到对应材料、字段和规则，相关说明与原始来源在条目详情按需展开；字典、映射结构及全局资料留在后台供 AI 使用，不要求用户逐条维护，也不因精简页面删除资料。
- 导入失败不拿不完整结果替代成功。沿用现有检查点重试，不新建运行时 Skill 引擎或自主 Agent 框架。

### 任务执行与恢复

- 场景发布、方案与任务配置按既有版本冻结；后续修改不得回写已有任务，也不能静默改用当前最新版本。
- `RULE_EXECUTION` 只是技术执行记录，不是可编排业务节点。任务图是固定生命周期，规则依赖计划生成一次，每条规则有独立模型/工具循环，不用图步数累计规则或模型轮次。
- LLM 根据生效规则抽取、计算和判断，按需使用真实工具。程序核对结构、身份、产出与真实引用，不额外裁定业务合理性，不以硬编码默认值填补业务口径。
- 上传只保存原件；执行时先做适用的原文件质量检查。失败或无法判定的文件停止分类、去章、印章、OCR 及相关依赖规则，无关材料继续；技术处理失败与业务质量不合格分开表达。
- 获准处理的文件按既有流水线执行：8018 类型校验后，8010 去章与印章识别并行，去章完成进入 OCR；四项成功才准备就绪。重试、补值、续跑不能绕过质量限制。
- 抽取结束停在 `WAITING_FIELD_REVIEW`，这是正常等待；人工确认后同批次执行计算、检查、风险和报告。保存字段修改不等于确认或重跑；人工值追加事实版本，保留原值与依据，不重抽覆盖。
- 核对依赖本批次保存的 `field_review.extraction_rule_keys`，空数组合法，缺属性明确报错，不能按当前配置补造历史清单。确认绑定批次、输入修订与字段版本；换文件不能沿用旧质量结论。
- 继续执行复用原批次，保留可信的已完成项，补跑未完成项及受影响下游；全部重新执行另建批次。旧结果失效时保留原结局与原因，页面不混淆执行完成和审核通过。
- 图没有持久化 checkpoint，恢复基于已提交规则结果，被中断单条从头执行；不能声称恢复了模型会话或实现了持久化断点。

### 取值与证据

完整格式见 [任务取值 CSV 维护说明](docs/任务取值CSV维护说明.md)。

- 任务格式版本为 6，在 `tasks/{task_no}/value_batches/current/` 维护 `values.csv`、`rule_results.csv`、`rule_items.csv`、`value_details.csv`。业务值、结构化明细和原文依据沿现有 CSV 保存；数据库保存索引、版本、状态、结论摘要、诊断和报告，不加重复取值副本或独立 Evidence 表。
- CSV 用 UTF-8 BOM、中文列名；写入按记录身份合并，四表逐个原子替换完成后才更新清单，与业务索引同事务提交。数据库与文件系统没有分布式事务，强制终止或断电不在回滚保证内。缺文件、缺索引记录或旧格式均明确报错，不伪造空结果或静默兼容。
- 金额与十进制不经 float，零值和 false 是实际值；未保存、null、空字符串分开处理。结构化值按现有明细树编码，原文依据按既有 JSON 列契约保存。
- 来源按结果实际引用的上传版本与产物解析，不用当前文件顶替旧文件。实际识别到的期间、口径、主体、单位必须对应本字段原文片段，不与配置声明混用；补取保留原文数值和单位，换算由计算步骤说明。

## 6. 数据库、外部服务与报告

数据库操作说明见 [数据库迁移](backend/migrations/README) 和 [README](README.md)。实体实际位于 `backend/app/modules/**/model.py`，不要按旧文档路径寻找 `app/entity/`。

- `BAOLI_DATABASE_URL` 明确指向 `baoli_v4` 或 `baoli_v4_dev`；启动只检查连接及必需表、实体声明列，不检查全部唯一键或生成列表达式，不自动建库、建表、迁移。
- 空库先执行完整 `backend/resource/schema.sql`，再登记 `stamp head`；基线迁移本身不建表。已有库按实际版本 `upgrade head`，不要把完整 schema 再覆盖已有库。
- 结构变更先改实体，再生成迁移并审查生成列、唯一键、复合外键和数据影响；获得数据库执行授权后应用，再同步完整 `schema.sql`。不通过删库重建代替迁移。

以下命令均在 `backend/`、Conda `dd` 中按授权使用：

```powershell
python -m alembic current
python -m alembic revision --autogenerate -m "describe change"
python -m alembic upgrade head
python -m alembic check
```

`stamp head` 仅用于已具备完整结构的新库登记；重置工具 `script/reset_database.py` 会删除业务数据，不作为开发或验证的默认步骤。

- 模型请求经过 `integrations/llm/gateway.py` 与 `pacing.py`，保持同进程串行、冷却、重试与总时限语义；不从新入口绕过统一节流。它不提供分布式配额控制。
- LLM、OCR 提交结果未知时不自动重新 POST；OCR 已有作业沿原标识续查，已完成产物复用。普通请求 60 秒超时与后端模型/OCR 总时限分别处理，不把前端等待结束判为后台失败。
- 外部服务“已配置，未验证”不代表连通；状态查询不擅自触发模型或供应商调用。新增配置同步 `.env.example`，真实 `.env` 不进入文档、日志或提交。
- 上传策略来自 `/api/system/upload-policy`，规则库与材料上限独立，后端最终校验；本系统上限不能当成外部服务或代理的承诺。
- 报告风格模板只影响表达，业务规则与事实仍来自任务冻结配置和结果。模板不提供新事实；维护前读 [报告模板](docs/报告模板.md)，沿用冻结档案和已有无效档案处理，不能偷换为模板当前版本。
- 当前提供 Markdown、纯文本、HTML 报告及规则库 Excel 下载；不要把 PDF 模板上传等同于 PDF/Word 报告生成。当前无身份认证、角色或数据范围控制，不把管理员/用户业务分工描述为已实现的权限系统。

## 7. 开发与交付流程

1. 查看实际分支和工作区差异，阅读 `AGENT.md` 及相关契约；保留已有未提交修改。先找相近功能与公共能力，再决定新增文件。
2. 后端沿 DTO、响应模型、Service、CRUD、实体和路由补齐；前端同步 API 契约、页面、路由及必要菜单。涉及后台操作时一并处理幂等、真实状态、失败与再次查询。
3. 涉及配置冻结、字段核对、CSV、材料质量或继续执行时，逐项核对本指南第 5 节及对应文档；涉及结构变更时准备迁移和完整 schema 更新，不默认执行数据库操作。
4. 按已获授权执行适合本次改动的验证，否则只静态核对导入、调用、字段、路径与前后端契约。明确区分替身测试、真实数据库、真实模型、浏览器与构建，不把历史测试通过写成本次验收。
5. 更新受影响的当前实现文档，不追加逐轮工作日志。交付说明写清改了什么、验证了什么、仍有什么限制；未授权部署时不把本地源码或构建结果描述为线上已生效。

排障优先沿 `request_id → logs/backend.log → 后台操作/执行记录 → 任务 CSV 与真实产物` 定位，先分清配置、业务缺项、模型渠道、材料处理、程序与存储故障，再决定修复位置。定位前端行为时核对实际服务入口和所用产物，不能只凭源码判断部署版本。
