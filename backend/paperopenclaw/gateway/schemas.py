"""Request schemas for the gateway.

Mirrors docs/03-api-contract.md. Field names are part of the contract: the
frontend calls these endpoints with exactly these keys.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

class ProviderPatch(BaseModel):
    id: str
    name: str = ""
    base_url: str = ""
    api_key: str = ""
    models: List[str] = Field(default_factory=list)
    vision: bool = False
    timeout: Optional[float] = None
    max_retries: Optional[int] = None
    enabled: Optional[bool] = None


class SettingsPatch(BaseModel):
    data_dir: Optional[str] = None
    providers: Optional[List[ProviderPatch]] = None
    routing: Optional[Dict[str, Dict[str, Any]]] = None
    embedding: Optional[Dict[str, Any]] = None
    ingest: Optional[Dict[str, Any]] = None
    retrieval: Optional[Dict[str, Any]] = None
    agent: Optional[Dict[str, Any]] = None
    server: Optional[Dict[str, Any]] = None


class ProviderTestRequest(BaseModel):
    provider: str
    model: str = ""
    kind: str = "chat"  # chat | parse | embedding


# ---------------------------------------------------------------------------
# ingest / jobs
# ---------------------------------------------------------------------------

class IngestPathsRequest(BaseModel):
    paths: List[str]
    recursive: bool = True


class ReparseRequest(BaseModel):
    from_stage: str = "vision"
    force: bool = True


class PathsRequest(BaseModel):
    paths: List[str]
    recursive: bool = False


# ---------------------------------------------------------------------------
# RAG
# ---------------------------------------------------------------------------

class RagSearchRequest(BaseModel):
    query: str
    hashes: Optional[List[str]] = None
    layers: Optional[List[str]] = None
    top_k: Optional[Dict[str, int]] = None
    min_score: Optional[float] = None
    cascade: bool = True


class RagRebuildRequest(BaseModel):
    hashes: Optional[List[str]] = None
    layers: Optional[List[str]] = None


# ---------------------------------------------------------------------------
# agent
# ---------------------------------------------------------------------------

class QaRequest(BaseModel):
    question: str
    hashes: Optional[List[str]] = None
    top_k: Optional[Dict[str, int]] = None
    session_id: Optional[str] = None
    agent_id: str = "qa"


class BrainstormAgentSpec(BaseModel):
    id: str = ""
    profile_id: str = ""
    name: str = ""
    persona: str = ""
    provider: str = ""
    model: str = ""
    tools: Optional[List[str]] = None
    hashes: Optional[List[str]] = None


class ModeratorSpec(BaseModel):
    name: str = "主持人"
    provider: str = ""
    model: str = ""


class BrainstormRequest(BaseModel):
    topic: str
    hashes: Optional[List[str]] = None
    agents: Optional[List[BrainstormAgentSpec]] = None
    rounds: Optional[int] = None
    moderator: Optional[ModeratorSpec] = None


class SessionMessageRequest(BaseModel):
    content: str


class BrainstormContinueRequest(BaseModel):
    """User steering for the next brainstorm round; empty = 不发言."""

    message: Optional[str] = None


class AgentRunRequest(BaseModel):
    """Ad-hoc single-agent run, used by the paper detail page."""

    task: str
    profile: str = "qa"
    hashes: Optional[List[str]] = None
    tools: Optional[List[str]] = None
    session_id: Optional[str] = None


# ---------------------------------------------------------------------------
# skills
# ---------------------------------------------------------------------------

class SkillInstallRequest(BaseModel):
    path: str
    enable: bool = True
    overwrite: bool = False


class SkillPatchRequest(BaseModel):
    enabled: Optional[bool] = None
    name: Optional[str] = None
    description: Optional[str] = None
