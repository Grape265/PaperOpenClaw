"""Memory layer: embeddings, vector store, cascade retrieval and OKF filesystem."""

from __future__ import annotations

from .embeddings import (
    EmbeddingProvider,
    LexicalEmbedding,
    LocalTransformerEmbedding,
    RemoteEmbedding,
    build_embedding_provider,
)
from .lexical import BM25Index, hashed_embedding, reciprocal_rank_fusion
from .okf_fs import (
    OKFError,
    build_index_markdown,
    build_log_markdown,
    build_tree,
    concept_id_for,
    dump_concept,
    extract_links,
    iter_concepts,
    load_concept,
    parse_concept,
    split_frontmatter,
    validate_bundle,
)
from .retriever import PyramidRetriever
from .vector_store import VectorStore, VectorStoreError

__all__ = [
    "EmbeddingProvider",
    "LexicalEmbedding",
    "LocalTransformerEmbedding",
    "RemoteEmbedding",
    "build_embedding_provider",
    "BM25Index",
    "hashed_embedding",
    "reciprocal_rank_fusion",
    "OKFError",
    "build_index_markdown",
    "build_log_markdown",
    "build_tree",
    "concept_id_for",
    "dump_concept",
    "extract_links",
    "iter_concepts",
    "load_concept",
    "parse_concept",
    "split_frontmatter",
    "validate_bundle",
    "PyramidRetriever",
    "VectorStore",
    "VectorStoreError",
]
