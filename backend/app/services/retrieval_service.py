from typing import Any

from sqlalchemy.orm import Session

from ..entities import EvidenceRetrieval
from . import rag_service
from .task_lookup import task_or_404


def retrieve(db: Session, task_no: str, payload: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """执行取证并在服务层结束事务。"""
    task = task_or_404(db, task_no)
    result = rag_service.retrieve_evidence(
        db,
        task,
        query_text=payload["query_text"],
        material_keys=payload["material_keys"],
        rule_key=payload.get("rule_key"),
        top_k=payload["top_k"],
    )
    db.commit()
    return task, result


def decide(
    db: Session,
    task_no: str,
    retrieval_no: str,
    payload: dict[str, Any],
) -> tuple[Any, EvidenceRetrieval]:
    """保存人工取证结论并在服务层结束事务。"""
    task = task_or_404(db, task_no)
    record = rag_service.record_human_decision(
        db,
        task,
        retrieval_no=retrieval_no,
        decision=payload["decision"],
        note=payload.get("note"),
        selected=payload.get("selected"),
        expected_input_revision=payload.get("expected_input_revision"),
    )
    db.commit()
    return task, record

