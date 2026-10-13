"""API prefix and router mounting order.

Mounted in one place so the contract's path layout is visible at a glance and so
route-ordering hazards (a ``/{id}`` path shadowing a literal sibling) are handled
consistently: routers whose paths share a prefix are declared in specificity
order inside their own module.
"""

from __future__ import annotations

from fastapi import FastAPI

from .routers import agent, files, ingest, kg, papers, rag, skills, system

API_PREFIX = "/api/v1"

# Order matters only across routers that could overlap; these do not.
ROUTERS = (
    system.router,
    papers.router,
    ingest.router,
    rag.router,
    kg.router,
    agent.router,
    skills.router,
    files.router,
)


def include_routers(app: FastAPI, prefix: str = API_PREFIX) -> None:
    for router in ROUTERS:
        app.include_router(router, prefix=prefix)
