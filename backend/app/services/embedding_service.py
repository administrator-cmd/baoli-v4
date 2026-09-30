"""本地 Embedding 的加载与推理。

范围与边界：

- 只使用本机**已经存在**的本地模型目录（`BAOLI_EMBEDDING_MODEL_PATH`）。系统不会自动下载
  权重，也不会切换到远程模型或云 Embedding；权重缺失、依赖缺失或模型不可读时抛出明确错误。
- 向量以十进制文本保存（`format(Decimal, 'f')`），避免浮点二进制在不同进程/平台上的
  不可复现差异；余弦相似度在检索端用 Decimal 计算。
- 查询与分块必须使用**同一个模型实例与同一份预处理**：本模块只暴露一个加载入口，
  查询与建索引用它取到的是同一份对象。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..config import get_settings
from ..common.exceptions import BusinessError


logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class EmbeddingRuntime:
    """已加载的嵌入运行时：模型标识、维度、切分口径与编码函数来自同一个真实对象。

    向量编码与分块必须使用同一份 tokenizer：切分依据它的真实 token 边界与最大序列长度，
    不按中文字符数估算，也不静默截断。因此 `tokenize` 与 `encode` 由同一个模型实例提供。
    """

    model_id: str
    model_revision: str
    dimension: int
    max_seq_length: int
    special_token_count: int
    model_dir: Path
    encode: Any
    tokenize: Any


_lock = threading.Lock()
_runtime: EmbeddingRuntime | None = None


def _revision_files(model_dir: Path) -> list[Path]:
    """模型目录中可能记录修订号的文件：HuggingFace 缓存快照与本地导出目录各有一种布局。"""
    candidates = [model_dir / "refs" / "main", model_dir / ".revision", model_dir / "revision.txt"]
    # 目录本身如果就叫 `<commit>/`（HF snapshot 布局），目录名就是修订号。
    return [item for item in candidates if item.is_file()]


def _assert_revision_matches(model_dir: Path, configured_revision: str) -> None:
    """核对配置的模型修订与实际加载的文件目录。

    `BAOLI_EMBEDDING_MODEL_REVISION` 参与索引版本标识，如果它与磁盘上的模型不一致，
    新索引会带着一个错误的“模型修订”入库，事后无法判断当初用的到底是哪份权重。
    因此这里做两件事：
      1. 目录名本身就是修订号（HF snapshot 布局 `<...>/snapshots/<rev>/`）时直接比对；
      2. 目录里存在 `refs/main` / `.revision` / `revision.txt` 时比对其中记录的修订号。

    两者都取不到时**不阻断**启动，但明确记录一次告警：本地导出目录（例如手动复制出的
    `text2vec-base-chinese/`）通常没有修订记录，此时无法自动核对。
    这属于“无法核对”而不是“核对通过”，不会伪造通过结论，也不会因为缺少记录就拒绝可用模型。
    """
    detected: str | None = None
    if model_dir.parent.name == "snapshots" and model_dir.name:
        detected = model_dir.name
    else:
        for path in _revision_files(model_dir):
            content = path.read_text(encoding="utf-8").strip()
            if content:
                detected = content
                break
    if detected is None:
        logger.warning(
            "本地嵌入模型目录 %s 没有可读取的修订记录，无法核对 BAOLI_EMBEDDING_MODEL_REVISION=%s；"
            "索引版本中的模型修订按配置值记录，请在部署说明中自行保证两者一致",
            model_dir,
            configured_revision,
        )
        return
    if detected != configured_revision:
        raise BusinessError(
            "EMBEDDING_REVISION_MISMATCH",
            "配置的模型修订与实际加载的模型文件不一致："
            f"目录记录为 {detected}，配置为 {configured_revision}。"
            "模型修订参与索引版本标识，不一致会让索引带着错误的模型版本入库，"
            "请修正 BAOLI_EMBEDDING_MODEL_REVISION 或改用对应目录后重试",
            status_code=503,
            details={"path": str(model_dir), "detected": detected, "configured": configured_revision},
        )


def _model_files_present(model_dir: Path) -> list[str]:
    """本地模型目录必须包含的最小文件集合；缺失项原样返回给用户。"""
    required = ("config.json", "vocab.txt", "tokenizer_config.json")
    missing = [name for name in required if not (model_dir / name).is_file()]
    if not any(model_dir.glob("*.safetensors")) and not any(model_dir.glob("*.bin")):
        missing.append("模型权重（*.safetensors 或 *.bin）")
    return missing


def _load_runtime() -> EmbeddingRuntime:
    settings = get_settings()
    model_dir = settings.resolved_embedding_model_path
    if model_dir is None:
        raise BusinessError(
            "EMBEDDING_MODEL_UNCONFIGURED",
            "本地嵌入模型未配置（BAOLI_EMBEDDING_MODEL_PATH）：需要检索取证的规则无法执行，"
            "系统不会自动下载模型或改用远程 Embedding",
            status_code=503,
        )
    if not model_dir.is_dir():
        raise BusinessError(
            "EMBEDDING_MODEL_MISSING",
            "本地嵌入模型目录不存在，需要检索取证的规则无法执行；请核对 BAOLI_EMBEDDING_MODEL_PATH",
            status_code=503,
            details={"path": str(model_dir)},
        )
    missing = _model_files_present(model_dir)
    if missing:
        raise BusinessError(
            "EMBEDDING_MODEL_INCOMPLETE",
            "本地嵌入模型目录缺少必要文件（" + "、".join(missing) + "），不能用于索引或检索",
            status_code=503,
            details={"path": str(model_dir), "missing": missing},
        )
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise BusinessError(
            "EMBEDDING_DEPENDENCY_MISSING",
            "缺少本地 Embedding 依赖 sentence-transformers / torch，无法建立索引或检索",
            status_code=503,
        ) from exc
    try:
        model = SentenceTransformer(str(model_dir), device="cpu")
    except Exception as exc:  # noqa: BLE001 - 加载失败必须原样暴露，不能静默换用别的模型
        raise BusinessError(
            "EMBEDDING_MODEL_LOAD_FAILED",
            "本地嵌入模型加载失败，索引与检索均不可用",
            status_code=503,
            details={"path": str(model_dir), "reason": exc.__class__.__name__},
        ) from exc
    dimension = int(model.get_sentence_embedding_dimension() or 0)
    if dimension <= 0:
        raise BusinessError(
            "EMBEDDING_DIMENSION_INVALID",
            "本地嵌入模型未能给出有效的向量维度",
            status_code=503,
        )
    max_seq_length = int(getattr(model, "max_seq_length", 0) or 0)
    if max_seq_length <= 0:
        raise BusinessError(
            "EMBEDDING_SEQ_LENGTH_INVALID",
            "本地嵌入模型未能给出有效的最大序列长度，无法确定切分口径",
            status_code=503,
        )
    special_token_count = int(model.tokenizer.num_special_tokens_to_add(pair=False))
    if max_seq_length <= special_token_count:
        raise BusinessError(
            "EMBEDDING_SEQ_LENGTH_INVALID",
            "本地嵌入模型的最大序列长度不足以容纳正文 token",
            status_code=503,
        )
    _assert_revision_matches(model_dir, settings.embedding_model_revision)

    def tokenize(text: str) -> dict[str, Any]:
        """按模型自己的 tokenizer 编码，返回带字符偏移的 encoding。

        偏移映射是分块能回到原文位置的唯一依据；tokenizer 不提供它时由调用方明确报错，
        不允许用字符数估算代替。
        """
        return dict(model.tokenizer(text, add_special_tokens=False, return_offsets_mapping=True))

    def encode(texts: list[str]) -> list[list[Decimal]]:
        if not texts:
            return []
        settings_now = get_settings()
        try:
            raw = model.encode(
                texts,
                batch_size=settings_now.embedding_batch_size,
                convert_to_numpy=True,
                normalize_embeddings=False,
                show_progress_bar=False,
            )
        except Exception as exc:  # noqa: BLE001 - 推理失败必须明确报错，不能返回空向量
            raise BusinessError(
                "EMBEDDING_INFERENCE_FAILED",
                "本地嵌入模型推理失败，索引或检索未完成",
                status_code=503,
                details={"reason": exc.__class__.__name__},
            ) from exc
        vectors: list[list[Decimal]] = []
        for row in raw:
            vector = [_vector_decimal(value) for value in row]
            if len(vector) != dimension:
                raise BusinessError(
                    "EMBEDDING_DIMENSION_MISMATCH",
                    "本地嵌入模型返回的向量维度与声明的维度不一致",
                    status_code=503,
                    details={"expected": dimension, "actual": len(vector)},
                )
            vectors.append(vector)
        return vectors

    return EmbeddingRuntime(
        model_id=settings.embedding_model_id,
        model_revision=settings.embedding_model_revision,
        dimension=dimension,
        max_seq_length=max_seq_length,
        special_token_count=special_token_count,
        model_dir=model_dir,
        encode=encode,
        tokenize=tokenize,
    )


def _vector_decimal(value: Any) -> Decimal:
    """把模型输出的单个分量转成十进制文本可表达的 Decimal。

    浮点在这里只做一次一次性转换：之后所有保存、范数与余弦相似度都用 Decimal 计算，
    保证同一份输入在同一进程内可复现。
    """
    try:
        number = Decimal(repr(float(value)))
    except (TypeError, ValueError) as exc:
        raise BusinessError(
            "EMBEDDING_VECTOR_INVALID",
            "本地嵌入模型返回了无法解析的向量分量",
            status_code=503,
        ) from exc
    if not number.is_finite():
        raise BusinessError(
            "EMBEDDING_VECTOR_INVALID",
            "本地嵌入模型返回了非有限数值的向量分量",
            status_code=503,
        )
    return number


def embedding_runtime() -> EmbeddingRuntime:
    """取得（并按需加载）本地嵌入运行时；加载失败抛出明确错误，不做静默降级。"""
    global _runtime
    if _runtime is not None:
        return _runtime
    with _lock:
        if _runtime is None:
            _runtime = _load_runtime()
    return _runtime


def embedding_status() -> dict[str, Any]:
    """只读的配置状态，供系统状态接口展示；不触发模型加载，也不返回路径细节之外的敏感信息。"""
    settings = get_settings()
    model_dir = settings.resolved_embedding_model_path
    if model_dir is None:
        return {"configuration_status": "UNCONFIGURED", "missing": ["BAOLI_EMBEDDING_MODEL_PATH"]}
    if not model_dir.is_dir():
        return {
            "configuration_status": "CONFIGURATION_INCOMPLETE",
            "missing": ["BAOLI_EMBEDDING_MODEL_PATH 指向的目录不存在"],
        }
    missing = _model_files_present(model_dir)
    if missing:
        return {"configuration_status": "CONFIGURATION_INCOMPLETE", "missing": missing}
    return {
        "configuration_status": "CONFIGURED_UNVERIFIED",
        "missing": [],
        "model_id": settings.embedding_model_id,
        "model_revision": settings.embedding_model_revision,
    }


def vector_to_json(vector: list[Decimal]) -> list[str]:
    """向量保存为十进制文本数组：不引入新的二进制依赖，也不做有损压缩。"""
    return [format(value, "f") for value in vector]


def vector_from_json(values: list[str]) -> list[Decimal]:
    """把持久化的向量分量还原成 Decimal 列表（全程不用 float）。"""
    return [Decimal(item) for item in values]


def vector_norm(vector: list[Decimal]) -> Decimal:
    """向量的欧几里得范数。

    零范数（全零向量）说明这份向量不是有效的嵌入结果：模型不会输出全零向量，
    出现它意味着写入或读取环节出了问题。这里返回 0 由调用方显式处理，
    不在这里静默改写向量。
    """
    total = sum((value * value for value in vector), Decimal("0"))
    if total <= 0:
        return Decimal("0")
    return total.sqrt()


def cosine_similarity(
    left: list[Decimal],
    left_norm: Decimal,
    right: list[Decimal],
    right_norm: Decimal,
    *,
    left_label: str = "查询向量",
    right_label: str = "候选向量",
) -> Decimal:
    """余弦相似度：只用于候选排序，不构成主体一致或审核通过的证明。

    维度不一致、零范数与非有限分量都**明确报错**，不返回 0 分：
    0 分会被当成“相似度最低的合法候选”参与排序并进入候选列表，
    把一份损坏或维度不符的向量伪装成一次正常检索的结果。
    """
    if len(left) != len(right):
        raise BusinessError(
            "EMBEDDING_VECTOR_DIMENSION_MISMATCH",
            "检索向量与候选向量的维度不一致，无法计算相似度；索引与当前模型不兼容",
            status_code=500,
            details={"left_label": left_label, "right_label": right_label, "left_dim": len(left), "right_dim": len(right)},
        )
    for label, vector in ((left_label, left), (right_label, right)):
        if any(not value.is_finite() for value in vector):
            raise BusinessError(
                "EMBEDDING_VECTOR_INVALID",
                "向量包含非有限分量，无法计算相似度",
                status_code=500,
                details={"vector": label},
            )
    if left_norm <= 0 or right_norm <= 0:
        zero_label = left_label if left_norm <= 0 else right_label
        raise BusinessError(
            "EMBEDDING_VECTOR_ZERO_NORM",
            "向量范数为 0（全零向量），不是有效的嵌入结果，无法计算相似度",
            status_code=500,
            details={"vector": zero_label, "left_norm": str(left_norm), "right_norm": str(right_norm)},
        )
    dot = sum((a * b for a, b in zip(left, right)), Decimal("0"))
    return dot / (left_norm * right_norm)


# 索引版本标识的格式与长度上限。
#   长度必须由本模块自己保证：模型标识与修订号是部署配置，直接拼接会随配置变长，
#   可能超过列宽而被数据库静默截断——截断后的版本会把两份不同的索引当成同一份。
#   这里改为「格式名 + 维度 + 决定因素的 SHA-256」，长度固定且可校验；
#   模型标识、修订号、维度与切分版本仍然各自单独存列，可随时据其重算并核对。
INDEX_VERSION_FORMAT = "idx1"
INDEX_VERSION_MAX_LENGTH = 80


def index_version_of(model_id: str, model_revision: str, dimension: int, chunk_version: str) -> str:
    """索引版本标识：模型、模型修订、向量维度与切分方式共同决定它。

    任何一个变化都必须产生新的索引版本，旧索引保留为历史，不被当成当前索引。
    标识长度固定，不依赖部署配置的长度；超出上限属于实现错误，直接报错而不是被截断。
    """
    parts = {
        "model_id": str(model_id),
        "model_revision": str(model_revision),
        "dimension": int(dimension),
        "chunk_version": str(chunk_version),
    }
    canonical = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    version = f"{INDEX_VERSION_FORMAT}-d{parts['dimension']}-{digest}"
    if len(version) > INDEX_VERSION_MAX_LENGTH:
        raise BusinessError(
            "EMBEDDING_INDEX_VERSION_TOO_LONG",
            "索引版本标识超出列宽上限，不能继续建立索引",
            status_code=500,
            details={"length": len(version), "max": INDEX_VERSION_MAX_LENGTH},
        )
    return version
