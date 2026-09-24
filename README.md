# baoli-v4

FastAPI + Vue 3 的规则库驱动审核系统。管理员创建场景、导入规则库并配置场景；完整配置保存后自动生效。业务用户生成方案、创建任务、上传材料并查看结论。业务流程与当前限制分别见 [业务逻辑](docs/业务逻辑.md) 和 [状态台账](docs/当前状态与问题台账.md)。

## 本地配置

后端统一读取仓库根目录 `.env`，以 `.env.example` 为配置说明。至少填写 `BAOLI_DATABASE_URL`，数据库名必须是 `baoli_v4`；相对的 `BAOLI_DATA_DIR` 也相对于仓库根目录。环境变量优先于 `.env`，修改配置后需重启后端。

应用启动只读校验数据库结构，不自动建库或迁移。结构变更直接修改 `backend/database/schema.sql`，从空库执行该文件；`CREATE TABLE IF NOT EXISTS` 不会升级现有表。数据库密码中的 URL 特殊字符需按 SQLAlchemy URL 规则编码。

开发库需要重建时，可用 `python script/reset_database.py` 先预览将清空的表，确认后再加 `--yes` 执行；随后重新运行 `backend/database/schema.sql`。这会删除业务数据，不用于需要保留数据的库。

上传限制分别由 `BAOLI_MAX_RULEBOOK_BYTES`（默认 30 MiB）和 `BAOLI_MAX_DOCUMENT_BYTES`（默认 128 MiB）配置。页面从 `GET /api/system/upload-policy` 读取策略，后端写入时最终校验；外部服务和反向代理的限制独立且未经确认。

## 外部依赖

| 配置 | 用途 |
| --- | --- |
| `BAOLI_LLM_BASEURL`、`BAOLI_LLM_APIKEY`、`BAOLI_LLM_MODEL_NAME` | 规则转换、字段抽取和语义校验 |
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

系统使用 `backend/config/unified_profile.json` 作为唯一模板协议；场景选择并绑定需要的 Sheet，完整配置保存后生成不可变版本，任务创建时冻结方案完整配置。具体规则见[规则库与审核执行契约](docs/规则库与审核执行契约.md)。

当前没有身份认证、角色或数据范围控制。大量新改动仅完成静态实现，真实业务材料、外部服务、浏览器及数据库链路尚未完整复验；不能把旧验证结果当作当前系统验收，详情见[状态台账](docs/当前状态与问题台账.md)。
