"""复核草稿的持久化暂存。

任务书要求：未提交的复核答案按作业/待办版本暂存，刷新不清空，也不把旧草稿
套到变化后的待办。实现方式：
- 每条草稿绑定 (任务, review_no) 与当时的待办版本（pending_review_hash）；
- 读取只返回与当前待办版本一致的草稿，旧版本的草稿不会被套用；
- 保存草稿是独立的持久化动作，与正式提交（后台命令）分开；提交本身仍走
  operation_gateway 的统一受理/幂等/互斥机制。
"""

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..common.exceptions import BusinessError
from ..entity import ReviewDraft
from .task_lookup import task_or_404

logger = logging.getLogger(__name__)


def _current_hash(task) -> str:
    # 待办版本：任务进入人工复核等待点时会保存 pending_review_hash；
    # 没有等待点时草稿不能保存（没有可绑定的待办版本）。
    return task.pending_review_hash or ""


def load_drafts(db: Session, task_no: str) -> dict[str, dict[str, Any]]:
    """读取当前待办版本下的复核草稿；旧版本的草稿不返回。"""
    task = task_or_404(db, task_no)
    current_hash = _current_hash(task)
    if not current_hash:
        return {}
    rows = db.scalars(
        select(ReviewDraft).where(ReviewDraft.audit_task_id == task.id, ReviewDraft.pending_hash == current_hash)
    ).all()
    return {
        row.review_no: {
            "review_no": row.review_no,
            "decision": row.decision,
            "business_note": row.business_note,
            "field_value": row.field_value,
            "basis_type": row.basis_type,
            "updated_at": row.updated_at,
        }
        for row in rows
    }


def save_drafts(db: Session, task_no: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """保存复核草稿（整体覆盖本次提交的条目）。

    - 必须在人工复核等待点：没有待办版本时明确拒绝，不把草稿挂到不确定的版本上。
    - 保存会清理更早待办版本遗留的草稿：旧草稿永远不套用到新待办。
    - 草稿引用必须属于当前待办集合：给不存在/已处理的待办存草稿会明确报错。
    """
    task = task_or_404(db, task_no, lock=True)
    current_hash = _current_hash(task)
    if not current_hash or task.status != "WAITING_REVIEW":
        raise BusinessError(
            "REVIEW_DRAFT_NOT_WAITING",
            "任务当前不在人工复核等待点，不能保存复核草稿",
            status_code=409,
            details={"task_no": task_no, "status": task.status},
        )
    from ..entity import ManualReview

    pending_nos = {
        row.review_no
        for row in db.scalars(
            select(ManualReview).where(ManualReview.audit_task_id == task.id, ManualReview.status == "PENDING")
        ).all()
    }
    existing = {
        row.review_no: row
        for row in db.scalars(select(ReviewDraft).where(ReviewDraft.audit_task_id == task.id)).all()
    }
    # 清理其他待办版本的旧草稿：旧草稿永远不套用到新待办。
    # 必须先 flush 再插入新行：同一 (audit_task_id, review_no) 的 DELETE 与 INSERT 落在同一次
    # flush 时，SQLAlchemy 固定先发 INSERT 后发 DELETE（保证 row-switch 语义），会直接撞上
    # 唯一键 uq_review_draft_task_review 而报 1062，且失败后旧行随事务回滚保留、之后每次保存都会失败。
    removed = False
    for review_no, row in list(existing.items()):
        if row.pending_hash != current_hash:
            db.delete(row)
            del existing[review_no]
            removed = True
    if removed:
        db.flush()
    saved: list[str] = []
    seen: set[str] = set()
    for item in items:
        review_no = str(item.get("review_no") or "")
        if review_no not in pending_nos:
            raise BusinessError(
                "REVIEW_DRAFT_TARGET_INVALID",
                "复核草稿引用了当前不存在的待办项，可能待办已被重算或处理",
                status_code=409,
                details={"task_no": task_no, "review_no": review_no},
            )
        # 同一请求里重复出现同一个待办时只写一次：否则同一批次会对同一键插入两行。
        if review_no in seen:
            continue
        seen.add(review_no)
        row = existing.get(review_no)
        if row is None:
            row = ReviewDraft(audit_task_id=task.id, review_no=review_no, pending_hash=current_hash)
            db.add(row)
            existing[review_no] = row
        row.decision = item.get("decision") or None
        row.business_note = item.get("business_note") or None
        row.field_value = item.get("field_value") or None
        row.basis_type = item.get("basis_type") or None
        saved.append(review_no)
    db.commit()
    logger.info("复核草稿已保存 task_no=%s count=%s", task_no, len(saved))
    return {"task_no": task_no, "pending_hash": current_hash, "saved_review_nos": saved}
