"""请求与后台工作单元的事务边界，以及依赖提交结果的外部动作。"""

from collections.abc import Callable

from sqlalchemy.orm import Session

_AFTER_COMMIT = "transaction_after_commit"
_BEFORE_ROLLBACK = "transaction_before_rollback"
_AFTER_ROLLBACK = "transaction_after_rollback"


def after_commit(session: Session, action: Callable[[], None]) -> None:
    session.info.setdefault(_AFTER_COMMIT, []).append(action)


def before_rollback(session: Session, action: Callable[[], None]) -> None:
    session.info.setdefault(_BEFORE_ROLLBACK, []).append(action)


def after_rollback(session: Session, action: Callable[[], None]) -> None:
    session.info.setdefault(_AFTER_ROLLBACK, []).append(action)


def commit_transaction(session: Session) -> None:
    """提交成功才启动后台任务或清理暂存文件。"""
    session.commit()
    session.info.pop(_BEFORE_ROLLBACK, None)
    session.info.pop(_AFTER_ROLLBACK, None)
    actions = session.info.pop(_AFTER_COMMIT, [])
    for action in actions:
        action()


def rollback_transaction(session: Session) -> None:
    """先恢复外部文件，再回滚并释放数据库及 CSV 锁。"""
    session.info.pop(_AFTER_COMMIT, None)
    actions = session.info.pop(_BEFORE_ROLLBACK, [])
    cleanup = session.info.pop(_AFTER_ROLLBACK, [])
    try:
        for action in reversed(actions):
            action()
    finally:
        session.rollback()
    for action in cleanup:
        action()
