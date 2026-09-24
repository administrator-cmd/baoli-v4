# 开发环境

## 技术栈

- 后端：Python 3.12、FastAPI、SQLAlchemy、Pydantic、MySQL 8.4。
- 编排与处理：LangGraph、MySQL checkpoint、openpyxl、httpx；本地向量模型使用 sentence-transformers 和 torch。
- 前端：Vue 3、TypeScript、Vite、Vue Router、Element Plus、lucide-vue-next。
- 准确依赖版本分别见 `backend/requirements.txt` 和 `frontend/package.json`，不要以本文代替依赖清单。

## 本机工具

- 开发分支：`dev`；`main` 为稳定分支。操作前以 `git branch --show-current` 核对实际分支。
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
