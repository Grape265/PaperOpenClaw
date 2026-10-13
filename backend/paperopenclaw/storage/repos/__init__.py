"""Repository layer: one module per aggregate, all sharing one Database."""

from __future__ import annotations

from .papers import PaperRepository
from .rag import RagRepository
from .kg import KnowledgeGraphRepository
from .fishbone import FishboneRepository
from .sessions import SessionRepository
from .skills import SkillRepository
from .jobs import JobRepository

__all__ = [
    "PaperRepository",
    "RagRepository",
    "KnowledgeGraphRepository",
    "FishboneRepository",
    "SessionRepository",
    "SkillRepository",
    "JobRepository",
]
