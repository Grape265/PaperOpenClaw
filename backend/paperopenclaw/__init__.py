"""PaperOpenClaw - OpenClaw-style agentic research paper knowledge base.

Layered architecture:
    gateway  -> HTTP/SSE entry point (FastAPI)
    agent    -> agent runtime, tool registry, skills, sub-agents
    ingest   -> PDF -> OKF -> RAG pyramid -> knowledge graph pipeline
    memory   -> RAG retrieval, vector store, embeddings, OKF filesystem
    kg       -> fishbone knowledge graph
    storage  -> SQLite repositories and content-addressed file layout
    llm      -> provider abstraction (OpenAI-compatible)
"""

from __future__ import annotations

__version__ = "1.0.0"

__all__ = ["__version__"]
