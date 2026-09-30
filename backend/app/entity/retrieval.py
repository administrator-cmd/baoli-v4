from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CHAR,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.mysql import DATETIME as MySQLDateTime
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from .base import TimestampMixin


# 审核任务材料证据索引与检索（本地 Embedding + 任务范围检索）。


class EvidenceIndexVersion(Base, TimestampMixin):
    """一个上传版本某个产物的一份索引版本。

    模型、向量维度或切分方式变化时必须新建索引版本，而不是原地覆盖：
    旧索引保留为历史，旧检索记录仍能指向当时使用的分块与模型。
    """

    __tablename__ = "evidence_index_version"
    __table_args__ = (
        # 一份索引由「上传 + 产物类型 + 产物内容版本 + 索引版本」唯一确定。
        # artifact_content_sha256 是产物**内容**的哈希：同一 PDF 重新识别后内容变了，
        # 新索引就是另一条记录，旧索引不会被当成当前产物的索引。
        UniqueConstraint(
            "document_upload_id", "artifact_type", "artifact_content_sha256", "index_version"
        ),
        UniqueConstraint("id", "audit_task_id"),
        ForeignKeyConstraint(
            ["document_upload_id", "audit_task_id"],
            ["document_upload.id", "document_upload.audit_task_id"],
        ),
        ForeignKeyConstraint(
            ["artifact_id", "audit_task_id"],
            ["stored_artifact.id", "stored_artifact.audit_task_id"],
        ),
        CheckConstraint("chunk_count >= 0"),
        Index("idx_evidence_index_upload_status", "document_upload_id", "status"),
        Index("idx_evidence_index_task_status", "audit_task_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    index_no: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    audit_task_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    document_upload_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    artifact_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(40), nullable=False)
    # 上传身份：这份索引是由哪个上传文件产生的。
    artifact_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    # 产物内容版本：索引实际依据的产物内容哈希。产物重新识别后内容哈希变化，
    # 旧索引即不再属于当前产物，必须重建而不是继续沿用。
    # 可空只用于表达“历史索引没有内容版本”：这类索引不再可检索，
    # 必须重建后才有内容版本；建索引时非空是硬要求。
    artifact_content_sha256: Mapped[str | None] = mapped_column(CHAR(64))
    # 索引版本标识：格式名 + 向量维度 + 决定因素摘要，长度固定（见 embedding_service）。
    index_version: Mapped[str] = mapped_column(String(80), nullable=False)
    model_id: Mapped[str] = mapped_column(String(160), nullable=False)
    model_revision: Mapped[str] = mapped_column(String(80), nullable=False)
    embedding_dim: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_version: Mapped[str] = mapped_column(String(80), nullable=False)
    # BUILDING=构建中；READY=整批成功、可检索；FAILED=构建失败（明确记录原因）；
    # INVALIDATED=上传版本被替换或产物重建，不再进入当前检索（历史保留）。
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    failure_code: Mapped[str | None] = mapped_column(String(80))
    failure_message: Mapped[str | None] = mapped_column(Text)
    built_at: Mapped[datetime | None] = mapped_column(MySQLDateTime(fsp=6))


class EvidenceChunk(Base, TimestampMixin):
    """一个可检索分块：原文、真实位置与向量。

    分块保留标题/表头上下文与原文位置；没有页码时记录原文行/字符区间并标注缺失，
    绝不把图片序号或 Markdown 图片路径当作 PDF 页码。
    """

    __tablename__ = "evidence_chunk"
    __table_args__ = (
        UniqueConstraint("index_version_id", "chunk_no"),
        UniqueConstraint("id", "audit_task_id"),
        ForeignKeyConstraint(
            ["index_version_id", "audit_task_id"],
            ["evidence_index_version.id", "evidence_index_version.audit_task_id"],
        ),
        CheckConstraint("chunk_no >= 1"),
        CheckConstraint("char_start >= 0 AND char_end >= char_start"),
        Index("idx_evidence_chunk_index", "index_version_id", "chunk_no"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    audit_task_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    index_version_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chunk_no: Mapped[int] = mapped_column(Integer, nullable=False)
    # 检索用文本（含标题/表头上下文）；原文位置指回它在源文本中的区间。
    chunk_text: Mapped[str] = mapped_column(MEDIUMTEXT, nullable=False)
    # 上下文是否由派生规则补全（例如补齐表头）：派生上下文不得伪装成连续原文。
    context_derived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("0"))
    heading_path: Mapped[str | None] = mapped_column(String(500))
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    # 页号与页号来源：PROVIDER=供应商返回的真实页号；SYNTHETIC_SEQUENCE=按结果项顺序的合成序号；
    # ABSENT=没有页码。合成序号只表示产物内的顺序，不等于原 PDF 页码。
    page_no: Mapped[int | None] = mapped_column(Integer)
    page_no_source: Mapped[str] = mapped_column(String(30), nullable=False, default="ABSENT", server_default=text("'ABSENT'"))
    # 印章条目：保留它的原始 JSON 条目引用（序号），可以回到原 JSON 条目，不只保存向量。
    source_entry_no: Mapped[int | None] = mapped_column(Integer)
    # 向量以十进制文本的 JSON 数组保存：不引入新的二进制依赖，也不做有损压缩。
    embedding_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    embedding_norm: Mapped[str] = mapped_column(String(80), nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))


class EvidenceRetrieval(Base, TimestampMixin):
    """一次任务内检索的完整记录。

    保存查询内容、限定范围、模型与索引版本、候选与相似度、最终采用的证据，
    以及它服务的规则执行。相似度只用于排序，不构成主体一致或审核通过的证明。
    """

    __tablename__ = "evidence_retrieval"
    __table_args__ = (
        UniqueConstraint("id", "audit_task_id"),
        ForeignKeyConstraint(
            ["audit_task_id", "scenario_version_id"],
            ["audit_task.id", "audit_task.scenario_version_id"],
        ),
        Index("idx_evidence_retrieval_task", "audit_task_id", "created_at"),
        Index("idx_evidence_retrieval_rule", "audit_task_id", "rule_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    retrieval_no: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    audit_task_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    scenario_version_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # 发起检索的规则键；程序侧检索没有规则键时为 NULL。
    rule_key: Mapped[str | None] = mapped_column(String(40))
    node_code: Mapped[str | None] = mapped_column(String(40))
    query_text: Mapped[str] = mapped_column(Text, nullable=False)
    # 限定范围（任务、材料、上传版本、产物、索引版本）：范围过滤发生在排序之前。
    scope_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    model_id: Mapped[str] = mapped_column(String(160), nullable=False)
    model_revision: Mapped[str] = mapped_column(String(80), nullable=False)
    embedding_dim: Mapped[int | None] = mapped_column(Integer)
    chunk_version: Mapped[str | None] = mapped_column(String(80))
    index_version: Mapped[str | None] = mapped_column(String(80))
    index_versions: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    # 候选结果：分块、相似度、产物归属与定位。这里保存的是**候选**，不是人工结论。
    candidates_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    # 实际送入后续步骤的正文条目（本次检索的上下文预算），与人工结论是两件事。
    adopted_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    # 人工结论：由工作人员在候选上选择一致 / 不一致 / 证据不足，并写明理由。
    # 与 candidates 分开保存：相似度不能代替审核结论，也不自动把前几条标为已采纳。
    human_decision: Mapped[str | None] = mapped_column(String(40))
    human_decision_note: Mapped[str | None] = mapped_column(Text)
    human_selected: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    human_decided_at: Mapped[datetime | None] = mapped_column(MySQLDateTime(fsp=6))
    human_decided_by: Mapped[str | None] = mapped_column(String(120))
    # 人工结论对应的任务输入修订：修订变化后旧结论不得套用到新的上传/事实。
    decision_input_revision: Mapped[int | None] = mapped_column(Integer)
    top_k: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    input_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))

