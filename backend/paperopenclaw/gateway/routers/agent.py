"""Agent endpoints: QA (including SSE), brainstorm, sessions and catalogues."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ...core.errors import BadRequest, SessionNotFound
from ...core.events import bus, session_topic
from ...core.hashing import stable_id
from ...core.logging import get_logger
from ...services import Services
from ...storage.db import utc_now
from ..deps import get_runner, get_services
from ..responses import success
from ..runner import JobRunner
from ..schemas import (AgentRunRequest, BrainstormContinueRequest,
                       BrainstormRequest, QaRequest, SessionMessageRequest)
from ..sse import from_events, from_subscription, sse_response

logger = get_logger(__name__)
router = APIRouter(tags=["agent"])


# ---------------------------------------------------------------------------
# catalogues
# ---------------------------------------------------------------------------

@router.get("/agent/profiles")
def profiles(request: Request, services: Services = Depends(get_services)) -> Dict[str, Any]:
    return success({"items": services.agents.profiles()}, request)


@router.get("/agent/tools")
def tools(request: Request, services: Services = Depends(get_services)) -> Dict[str, Any]:
    return success({"items": services.agents.tool_catalogue()}, request)


# ---------------------------------------------------------------------------
# QA
# ---------------------------------------------------------------------------

@router.post("/agent/qa")
def qa(request: Request, body: QaRequest,
       services: Services = Depends(get_services)) -> Dict[str, Any]:
    if not (body.question or "").strip():
        raise BadRequest("question 不能为空")
    result = services.agents.answer(
        question=body.question,
        hashes=body.hashes or None,
        top_k=body.top_k,
        session_id=body.session_id,
        agent_id=body.agent_id or "qa",
    )
    return success(result, request)


@router.post("/agent/qa/stream")
async def qa_stream(request: Request, body: QaRequest,
                    services: Services = Depends(get_services)) -> StreamingResponse:
    if not (body.question or "").strip():
        raise BadRequest("question 不能为空")

    generator = services.agents.stream_answer(
        question=body.question,
        hashes=body.hashes or None,
        top_k=body.top_k,
        session_id=body.session_id,
        agent_id=body.agent_id or "qa",
    )
    return sse_response(from_events(generator, request))


@router.post("/agent/run")
def run_agent_endpoint(request: Request, body: AgentRunRequest,
                       services: Services = Depends(get_services)) -> Dict[str, Any]:
    """Run one agent through its tool loop, without streaming."""
    if not (body.task or "").strip():
        raise BadRequest("task 不能为空")

    from ...agent.profiles import get_profile

    profile = get_profile(body.profile or "qa")
    session_id = body.session_id or f"sess_{stable_id('run', body.task, utc_now(), length=12)}"
    if services.sessions.get(session_id) is None:
        services.sessions.create(session_id, kind="agent", title=body.task[:60],
                                 topic=body.task[:200], paper_hashes=list(body.hashes or []))

    context = services.agents.make_tool_context(
        session_id=session_id,
        agent_id=profile.id,
        allowed_hashes=body.hashes or None,
        allowed_tools=body.tools or profile.tools,
    )
    turn = services.agents.run_loop(profile, task=body.task, tool_context=context)

    services.sessions.add_message(session_id, role="user", content=body.task,
                                  agent_id=profile.id)
    services.sessions.add_message(session_id, role="assistant", content=turn.text,
                                  agent_id=profile.id, usage=turn.usage.to_dict())
    services.sessions.update(session_id, status="done")

    return success({
        "session_id": session_id,
        "agent_id": profile.id,
        "answer": turn.text,
        "steps": turn.steps,
        "tool_calls": turn.tool_calls,
        "usage": turn.usage.to_dict(),
        "elapsed_ms": turn.elapsed_ms,
        "error": turn.error,
    }, request)


# ---------------------------------------------------------------------------
# brainstorm
# ---------------------------------------------------------------------------

@router.post("/agent/brainstorm")
def brainstorm(request: Request, body: BrainstormRequest,
               services: Services = Depends(get_services),
               runner: JobRunner = Depends(get_runner)) -> Dict[str, Any]:
    if not (body.topic or "").strip():
        raise BadRequest("topic 不能为空")

    # The id is minted here so the client can subscribe before the work starts;
    # otherwise a fast first round could publish before the subscription exists.
    session_id = f"sess_{stable_id('brainstorm', body.topic, utc_now(), length=12)}"
    agents = [item.model_dump(exclude_none=True) for item in (body.agents or [])]
    moderator = body.moderator.model_dump() if body.moderator else None

    # The row is created *before* returning, so an immediate
    # `GET /agent/sessions/{id}/events` finds the session rather than a 404.
    services.sessions.create(
        session_id,
        kind="brainstorm",
        title=body.topic[:60],
        topic=body.topic,
        paper_hashes=list(body.hashes or []),
        meta={"rounds": body.rounds, "queued": True},
    )

    runner.submit(
        session_id,
        services.agents.brainstorm,
        topic=body.topic,
        hashes=body.hashes or [],
        agents=agents,
        rounds=body.rounds,
        moderator=moderator,
        session_id=session_id,
        interactive=True,
    )
    return success({"session_id": session_id, "status": "running"}, request)


@router.post("/agent/sessions/{session_id}/continue")
def continue_brainstorm_endpoint(request: Request, session_id: str,
                                 body: BrainstormContinueRequest,
                                 services: Services = Depends(get_services),
                                 runner: JobRunner = Depends(get_runner)) -> Dict[str, Any]:
    """Run the next round of an interactive brainstorm session.

    ``message`` is optional user steering; omit or send an empty string to mean
    "不发言" (proceed on the blackboard alone).
    """
    session = services.sessions.get(session_id)
    if session is None:
        raise SessionNotFound(f"会话不存在：{session_id}")
    if session.get("status") != "awaiting_user":
        raise BadRequest("当前会话不在等待用户发言的状态")

    services.sessions.update(session_id, status="running")
    runner.submit(session_id, services.agents.continue_brainstorm,
                  session_id, message=(body.message or "").strip() or None)
    return success({"session_id": session_id, "status": "running"}, request)


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------

@router.get("/agent/sessions")
def list_sessions(request: Request, kind: str = "", page: int = 1, page_size: int = 20,
                  services: Services = Depends(get_services)) -> Dict[str, Any]:
    items, total = services.sessions.list(kind=kind, page=page, page_size=page_size)
    return success({"items": items, "total": total, "page": max(1, page),
                    "page_size": max(1, min(page_size, 200))}, request)


@router.get("/agent/sessions/{session_id}")
def get_session(request: Request, session_id: str,
                services: Services = Depends(get_services)) -> Dict[str, Any]:
    session = services.sessions.get(session_id)
    if session is None:
        raise SessionNotFound(f"会话不存在：{session_id}")
    agents = services.sessions.list_agents(session_id)
    messages = services.sessions.list_messages(session_id)
    blackboard = services.sessions.list_blackboard(session_id)

    # Group by agent so the UI can render isolated threads side by side (FR-10.2).
    by_agent: Dict[str, List[Dict[str, Any]]] = {}
    for message in messages:
        by_agent.setdefault(message.get("agent_id") or "system", []).append(message)

    synthesis = next((item for item in messages if item["role"] == "synthesis"), None)
    parsed_synthesis: Optional[Dict[str, Any]] = None
    if synthesis:
        try:
            parsed_synthesis = json.loads(synthesis["content"])
        except Exception:  # noqa: BLE001 - fall back to raw text
            parsed_synthesis = {"synthesis": synthesis["content"]}

    return success({
        "session": session,
        "agents": agents,
        "messages": messages,
        "by_agent": by_agent,
        "blackboard": blackboard,
        "synthesis": parsed_synthesis,
    }, request)


@router.get("/agent/sessions/{session_id}/events")
async def session_events(request: Request, session_id: str,
                         services: Services = Depends(get_services)) -> StreamingResponse:
    session = services.sessions.get(session_id)
    if session is None:
        raise SessionNotFound(f"会话不存在：{session_id}")

    topic = session_topic(session_id)
    subscription = bus.subscribe(topic, backlog=500)

    async def generator():
        try:
            if session.get("status") in ("done", "failed"):
                # Already finished: synthesise the terminal frame from the store
                # rather than leaving the client waiting for an event that will
                # never be republished.
                yield ("event: done\ndata: "
                       + json.dumps({"session_id": session_id,
                                     "status": session.get("status")},
                                    ensure_ascii=False) + "\n\n")
                return
            async for chunk in from_subscription(subscription, request):
                yield chunk
        finally:
            bus.unsubscribe(subscription)

    return sse_response(generator())


@router.post("/agent/sessions/{session_id}/message")
def post_session_message(request: Request, session_id: str, body: SessionMessageRequest,
                         services: Services = Depends(get_services)) -> Dict[str, Any]:
    session = services.sessions.get(session_id)
    if session is None:
        raise SessionNotFound(f"会话不存在：{session_id}")
    if not (body.content or "").strip():
        raise BadRequest("content 不能为空")

    message_id = services.sessions.add_message(session_id, role="user", content=body.content)
    services.sessions.update(session_id, status="idle")
    return success({"message_id": message_id, "session_id": session_id}, request)


@router.delete("/agent/sessions/{session_id}")
def delete_session(request: Request, session_id: str,
                   services: Services = Depends(get_services)) -> Dict[str, Any]:
    if services.sessions.get(session_id) is None:
        raise SessionNotFound(f"会话不存在：{session_id}")
    deleted = services.sessions.delete(session_id)
    bus.clear(session_topic(session_id))
    return success({"session_id": session_id, "deleted": deleted}, request)


@router.get("/agent/sessions/{session_id}/blackboard")
def get_blackboard(request: Request, session_id: str,
                   services: Services = Depends(get_services)) -> Dict[str, Any]:
    if services.sessions.get(session_id) is None:
        raise SessionNotFound(f"会话不存在：{session_id}")
    return success({"items": services.sessions.list_blackboard(session_id)}, request)
