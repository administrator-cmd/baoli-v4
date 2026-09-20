# CLAUDE.md

保理合同审核验证版（baoli-v4）：FastAPI 后端 + Vue 3 前端，Excel 规则库驱动的审核流程。

## 环境：Conda `dd`

项目跑在 Conda `dd` 环境里，由 `pip install -r backend/requirements.txt` 装配。

| | |
|---|---|
| 环境名 | `dd`（`conda activate dd`） |
| 解释器 | `C:\Users\29550\miniconda3\envs\dd\python.exe` |
| Python | 3.12.14（Anaconda 打包版，MSC v.1942 64bit） |
| 包数量 | 85 |

`dd` 里已安装的依赖（实际版本，与 `requirements.txt` 一致）：

| 包 | 版本 | 用途 |
|---|---|---|
| fastapi | 0.116.1 | 后端框架 |
| uvicorn | 0.35.0 | ASGI 服务器 |
| sqlalchemy | 2.0.43 | ORM |
| pymysql | 1.1.2 | MySQL 驱动 |
| pydantic | 2.11.7 | 数据契约 |
| pydantic-settings | 2.10.1 | `.env` 配置加载 |
| python-multipart | 0.0.20 | 文件上传 |
| openpyxl | 3.1.5 | Excel 规则库解析 |
| httpx | 0.28.1 | 异步外部服务调用 |
| requests | 2.34.2 | 同步外部服务调用 |
| langgraph | 0.6.6 | 导入/编译/审核流程编排 |
| langgraph-checkpoint | 2.1.2 | checkpoint 接口 |
| langgraph-checkpoint-mysql | 3.0.0 | checkpoint 持久化到 `baoli_v4` 库 |
| sentence-transformers | 3.4.1 | 印章主体检索的本地 Embedding |
| torch | 2.14.0 | Embedding 推理后端（CPU 版） |

本机另有 conda 环境 `base` / `gamehub` / `langgraph_learn`，以及系统 Python
3.8 / 3.10 / 3.12；**它们都不是本项目环境**，不要用它们跑本项目的代码。

在 Git Bash 里非交互式调用（本仓库不是 git 仓库，路径含中文）：

```bash
PYTHONIOENCODING=utf-8 /c/Users/29550/miniconda3/envs/dd/python.exe <脚本或 -c "...">
```

`PYTHONIOENCODING=utf-8` 必须加，否则中文输出在 Windows 控制台会乱码。

其它工具链：Node v23.11.1 / npm 10.9.2；MySQL 8.4.11（`D:\develop\mysql-8.4.11\bin\`）。

## 常用命令

后端（在项目根目录或 `backend/` 下启动等价，`.env` 都读项目根目录那一份）：

```bash
conda activate dd && cd backend && uvicorn app.main:app --host 127.0.0.1 --port 8000
```

前端（代理固定指向 `http://127.0.0.1:8000`）：

```bash
cd frontend && npm run dev
```

数据库结构初始化与升级、`script/reset_database.py` 的用法见 `README.md`，不要凭记忆执行。
应用启动时**只读校验**库结构，缺列/缺迁移记录会拒绝启动——这是预期行为，不是配置错误。

## 仓库里没有测试框架

`requirements.txt` 不含 pytest，也没有 `tests/` 目录。现有唯一验证脚本
`backend/test_import_job_list.py` 是纯标准库脚本，直连真实 MySQL、自带前置数据并自清理。

**只有用户明确要求时才运行测试、类型检查、构建、浏览器、服务、数据库、模型或外部接口。**
未收到该要求时只做静态读取和核对，也不新增一次性测试脚本。

获准验证后必须使用真实依赖和用户提供的真实业务文件；本地缺少所需文件时由用户提供，
不得用空白文件、生成文件或其他材料槽位的文件替代。不要引入 mock、模拟成功或吞错路径。

## 架构要点

- `backend/app/domain/manifest.py`：候选 → 已发布配置的编译与校验。规则输入数量、字段类型、
  单位币种一致性、风险来源闭合都在这里兜底。
- `backend/app/domain/node_rulebook.py`：`contract_review_v3`（节点版，七张表）的确定性解析，
  不调用模型、不使用向量。
- `backend/app/services/audit_service.py`：审核任务执行。节点分发在 `_dispatch`，
  规则执行在 `_execute_rules`。
- `backend/app/operators/`：算子实现与 `REGISTRY`。
- `backend/config/rulebook_profiles/`：各格式版本的列协议与枚举。

当前工具与算子能力仍散落在 profile、`proposals.py`、`node_rulebook.py`、`quality_checks.py`、
`manifest.py`、`file_quality_service.py`、`operators/registry.py`、`audit_service.py` 和前端选项中；
这不是一份有限的“改四处”清单。
新增能力前必须沿导入、编译、发布、执行和页面完整追踪，漏一处就可能出现“能导入、运行时才失败”。
该问题及修复顺序见 `docs/当前状态与问题台账.md`；P1 完成后应以 `docs/系统设计基线.md` 规定的统一注册表为单一来源。

## 反复踩到的约定

- **业务配置状态三态**：`CLEAR`（已明确）/ `PENDING`（待确认）/ `DISABLED`（停用）。
  停用行在解析阶段就短路、不进候选；非停用的 `PENDING` 行**必须**带 `resolve`
  落成可作答的待确认事项，否则会静默进已发布配置。这条对材料、字段、规则、报告章节一体适用。
- **裁决身份必须同口径**：待确认事项的 `target_type` + `target_ref` + `property`
  同时用于落库的 `ImportIssue.source_json` 和 `import_decision_signature`。
  给同一个业务对象另造一套引用会让转换期与恢复期算出两个不同的 `decision_signature`。
- **节点的 `config_status` 不进 `enabled`**：`enabled` 只表达「非停用」，待确认由发布检查的
  未处理问题阻断，不在这里静默当成已确认。
- 文档里写的「未运行验证」是事实陈述。README 中「已知限制」一节长期有效。

## 工作前先读

`README.md`（配置、启动、已知限制）、`docs/README.md`（文档索引）、
`docs/当前状态与问题台账.md`（当前问题、执行边界与真实验收状态）。业务口径未确认的项按待确认处理，
不代填默认值、不自动停用绕过。
