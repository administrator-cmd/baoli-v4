from contextlib import asynccontextmanager
import logging
from logging.handlers import RotatingFileHandler
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from .api import audits, imports, materials, operations, plans, reports, reviews, scenarios, system
from .config import PROJECT_DIR, get_settings
from .domain import node_profile
from .integrations import http_services
from .services.audit import document_ports
from .database import SessionLocal, check_database_connection
from .common.exceptions import (
    BusinessError,
    business_error_handler,
    http_error_handler,
    request_validation_error_handler,
    unhandled_error_handler,
)
from .runtime.checkpoint import check_checkpoint_tables
from .runtime.schema_check import verify_schema
from .middleware.request_context import attach_request_id
from .services.audit import recover_interrupted_executions
from .services.background_service import recover_interrupted_operations


def _configure_logging(level_name: str) -> None:
    """日志同时输出到控制台与文件。

    只输出到控制台时，日志随进程终止而消失：一次未预期失败的完整堆栈就再也找不回来
    （落库的 failure_message 只是摘要，不含完整堆栈）。文件处理器按大小滚动，
    只保留最近几份，避免长期运行把磁盘写满。
    """
    level = getattr(logging, level_name)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    # 日志放在项目根目录的 logs/，不放进 data/：data 是业务上传与处理产物，
    # 日志跟它们混在一起会让备份与清理都难以区分。
    log_dir = PROJECT_DIR / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                log_dir / "backend.log",
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
        )
    except OSError:
        # 目录不可写（例如只读挂载）时只保留控制台输出，并在日志里说明原因；
        # 不因此拒绝启动——审核流程本身不依赖日志文件。
        logging.getLogger(__name__).warning("日志目录不可写，本次只输出到控制台：%s", log_dir)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
        force=True,
    )
    logging.getLogger().setLevel(level)
    logging.getLogger("app").setLevel(level)


settings = get_settings()
# 执行契约目录路径从配置注入，domain 不直接读取全局配置。
node_profile.configure_node_profile(settings.resolved_node_profile_path)
# 外部文档服务的供应商实现注册进审核层端口：审核层不导入 integrations，只认端口。
document_ports.register_document_service(http_services)
_configure_logging(settings.log_level)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        check_database_connection()
    except (OSError, SQLAlchemyError):
        logger.error("数据库连接检查失败")
        raise RuntimeError("数据库连接失败，请检查 BAOLI_DATABASE_URL、网络和账号权限") from None
    # 只读校验实际表、列、唯一键、外键及触发器，不依赖迁移记录。
    # 应用绝不执行 DDL、不调用 create_all()；结构变更由用户从空库执行 schema.sql。
    try:
        verify_schema()
    except RuntimeError as exc:
        logger.error("数据库结构校验失败：%s", exc)
        raise
    # 只检查 checkpoint 表是否存在，不执行任何 DDL。缺表时把库名与缺失表名一并打出来，
    # 便于判断是没执行建表脚本，还是脚本执行到了另一个库。
    try:
        check_checkpoint_tables()
    except BusinessError as exc:
        logger.error(
            "checkpoint 表结构检查失败 code=%s message=%s details=%r",
            exc.code,
            exc.message,
            exc.details,
        )
        raise
    # 上次进程被杀时可能有节点停在 RUNNING、或后台操作停在活跃状态；
    # 这里显式标记中断原因，不自动重放外部调用。等待中的任务不受影响。
    with SessionLocal() as session:
        recovered = recover_interrupted_executions(session)
        operations = recover_interrupted_operations(session)
    if recovered["interrupted_executions"] or recovered["blocked_tasks"] or operations["interrupted_operations"]:
        logger.warning("已标记上次运行中断：%s %s", recovered, operations)
    yield


app = FastAPI(title=settings.app_name, version="4.0.0-verification", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.middleware("http")(attach_request_id)


app.add_exception_handler(BusinessError, business_error_handler)
app.add_exception_handler(RequestValidationError, request_validation_error_handler)
app.add_exception_handler(StarletteHTTPException, http_error_handler)
app.add_exception_handler(Exception, unhandled_error_handler)

for router in (
    system.router,
    imports.router,
    scenarios.router,
    plans.router,
    audits.router,
    reviews.router,
    reviews.executions_router,
    materials.router,
    materials.evidence_router,
    reports.router,
    reports.risks_router,
    operations.router,
):
    app.include_router(router, prefix=settings.api_prefix)
