"""Multi-agent brainstorm orchestration (FR-10).

Round structure, and why each round is shaped the way it is::

    round 0   moderator decomposes the topic into one sub-question per role
    round 1   every agent analyses independently — no agent sees another's
              output, which is what produces genuinely different starting points
    blackboard the moderator compresses round 1 into a short summary
    round 2..k each agent revises its position, seeing only the blackboard
              (not the other agents' full contexts, ADR-8)
    final     moderator synthesises consensus, disagreements and open questions

The blackboard is the *only* cross-agent channel. Sharing full contexts would
both blow the token budget and collapse the discussion into one voice, because
later agents would anchor on the first agent's phrasing.

Isolation is enforced on four axes at once (FR-10.2): separate message history,
separate workspace directory, tool allowlist, and paper scope.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Sequence

from ..core.events import bus, session_topic
from ..core.hashing import stable_id
from ..core.jsonutil import JsonExtractionError, extract_json, safe_json_loads
from ..core.logging import get_logger
from ..llm.base import Message
from ..llm.prompts import brainstorm as brainstorm_prompts
from ..llm.registry import KIND_CHAT
from .profiles import AgentProfile, resolve_roster

logger = get_logger(__name__)


def _publish(target: str, name: str, **payload: Any) -> None:
    """Publish a session event.

    The first parameter is named ``target`` rather than ``session_id`` so that a
    payload may itself carry a ``session_id`` key without colliding.
    """
    bus.publish(session_topic(target), name, payload)


def run_brainstorm(
    runtime: Any,
    *,
    topic: str,
    hashes: Optional[Sequence[str]] = None,
    agents: Optional[Sequence[Dict[str, Any]]] = None,
    rounds: Optional[int] = None,
    moderator: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None,
    interactive: bool = False,
) -> Dict[str, Any]:
    """Start a brainstorm session.

    Non-interactive (default) runs every round through to synthesis. Interactive
    mode runs round 1, then parks the session in ``awaiting_user`` so the caller
    can steer the next round via :func:`continue_brainstorm`.
    """
    services = runtime.services
    settings = services.settings.current
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("议题不能为空")

    identifiers = [str(item.get("id") or item.get("profile_id") or "")
                   for item in (agents or [])]
    roster = resolve_roster([item for item in identifiers if item] or None,
                            limit=settings.agent.brainstorm_max_agents)

    overrides: Dict[str, Dict[str, Any]] = {}
    for item in agents or []:
        key = str(item.get("id") or item.get("profile_id") or "")
        if key:
            overrides[key] = dict(item)

    total_rounds = max(1, int(rounds or settings.agent.brainstorm_rounds))
    scope = list(hashes or [])
    moderator_config = dict(moderator or {})
    moderator_profile = _moderator_profile(moderator_config)

    identifier = session_id or f"sess_{stable_id('brainstorm', topic, time.time(), length=12)}"
    services.sessions.create(
        identifier,
        kind="brainstorm",
        title=topic[:60],
        topic=topic,
        paper_hashes=scope,
        meta={"rounds": total_rounds, "agents": [profile.id for profile in roster],
              "interactive": bool(interactive), "rounds_done": 0,
              "steering": [], "moderator": moderator_config},
    )

    agent_rows: List[Dict[str, Any]] = []
    for profile in roster:
        override = overrides.get(profile.id, {})
        allowed_tools = list(override.get("tools") or profile.tools)
        agent_scope = list(override.get("hashes") or scope)
        workspace = services.paths.agent_dir(identifier, profile.id)
        workspace.mkdir(parents=True, exist_ok=True)
        row = services.sessions.add_agent(
            identifier,
            profile.id,
            name=str(override.get("name") or profile.name),
            persona=str(override.get("persona") or profile.persona),
            profile_id=profile.id,
            provider=str(override.get("provider") or ""),
            model=str(override.get("model") or ""),
            tools=allowed_tools,
            paper_hashes=agent_scope,
            workspace=services.paths.to_relative(workspace),
        )
        agent_rows.append(row)

    _publish(identifier, "round", round=0, phase="decompose",
             agents=[row["id"] for row in agent_rows])

    try:
        plan = _decompose(runtime, moderator_profile, moderator_config, topic, agent_rows, scope)
        sub_questions: Dict[str, str] = {
            str(item.get("role") or ""): str(item.get("question") or topic)
            for item in plan.get("sub_questions") or []
        }
        shared_context = str(plan.get("shared_context") or "")
        services.sessions.add_message(identifier, role="blackboard", agent_id="moderator",
                                      content=json.dumps(plan, ensure_ascii=False),
                                      round_index=0)
        _publish(identifier, "plan", sub_questions=sub_questions,
                 shared_context=shared_context)
        _update_meta(services, identifier, sub_questions=sub_questions,
                     shared_context=shared_context)

        speeches: Dict[str, str] = {}
        transcript: List[Dict[str, Any]] = []

        _run_round(runtime, identifier, 1, agent_rows, sub_questions, shared_context,
                   "", speeches, "", transcript)
        _update_meta(services, identifier, rounds_done=1)

        if interactive and total_rounds > 1:
            services.sessions.update(identifier, status="awaiting_user")
            _publish(identifier, "awaiting_user", round=1, total_rounds=total_rounds)
            return {"session_id": identifier, "rounds": total_rounds, "rounds_done": 1,
                    "status": "awaiting_user", "agents": [row["id"] for row in agent_rows]}

        for round_index in range(2, total_rounds + 1):
            blackboard_text = _latest_blackboard(services, identifier)
            _run_round(runtime, identifier, round_index, agent_rows, sub_questions,
                       shared_context, blackboard_text, speeches, "", transcript)
            _update_meta(services, identifier, rounds_done=round_index)
            services.sessions.update(identifier, rounds=round_index)

        synthesis = _run_synthesis(runtime, identifier, moderator_profile, moderator_config,
                                   topic, transcript, total_rounds)
        return {"session_id": identifier, "rounds": total_rounds,
                "agents": [row["id"] for row in agent_rows], "synthesis": synthesis}

    except Exception as exc:  # noqa: BLE001 - report and close the stream
        logger.exception("头脑风暴执行失败：%s", exc)
        services.sessions.update(identifier, status="failed",
                                 meta=_read_meta(services, identifier, extra={"error": str(exc)}))
        _publish(identifier, "error", code=getattr(exc, "code", "INTERNAL_ERROR"),
                 message=str(exc))
        _publish(identifier, "done", session_id=identifier, failed=True)
        raise


def continue_brainstorm(runtime: Any, session_id: str,
                        message: Optional[str] = None) -> Dict[str, Any]:
    """Run the next round of a parked interactive brainstorm session.

    ``message`` is optional user steering: when present it is recorded and
    injected into every agent's prompt for the next round so the discussion
    shifts direction accordingly. When absent ("不发言") the round proceeds on
    the blackboard alone.
    """
    services = runtime.services
    session = services.sessions.get(session_id)
    if session is None:
        from ..core.errors import SessionNotFound

        raise SessionNotFound(f"会话不存在：{session_id}")

    meta = safe_json_loads(session.get("meta") or "", {})
    topic = str(session.get("topic") or "")
    total_rounds = max(1, int(meta.get("rounds") or 1))
    rounds_done = max(1, int(meta.get("rounds_done") or 1))
    sub_questions = meta.get("sub_questions") or {}
    shared_context = str(meta.get("shared_context") or "")
    steering = list(meta.get("steering") or [])
    moderator_config = dict(meta.get("moderator") or {})
    moderator_profile = _moderator_profile(moderator_config)

    if message and message.strip():
        steering.append(message.strip())
        services.sessions.add_message(session_id, role="user", content=message.strip(),
                                      agent_id="moderator")
        _update_meta(services, session_id, steering=steering)
        services.sessions.update(session_id, status="running")

    agent_rows = services.sessions.list_agents(session_id)
    speeches = _last_speeches(services, session_id, agent_rows)
    transcript = _load_transcript(services, session_id)

    next_round = rounds_done + 1
    steering_block = "\n".join(steering)
    blackboard_text = _latest_blackboard(services, session_id)

    try:
        _run_round(runtime, session_id, next_round, agent_rows, sub_questions,
                   shared_context, blackboard_text, speeches, steering_block, transcript)
        _update_meta(services, session_id, rounds_done=next_round)
        services.sessions.update(session_id, rounds=next_round)

        if next_round >= total_rounds:
            synthesis = _run_synthesis(runtime, session_id, moderator_profile,
                                       moderator_config, topic, transcript, total_rounds)
            return {"session_id": session_id, "status": "done",
                    "rounds_done": next_round, "synthesis": synthesis}

        services.sessions.update(session_id, status="awaiting_user")
        _publish(session_id, "awaiting_user", round=next_round, total_rounds=total_rounds)
        return {"session_id": session_id, "status": "awaiting_user", "rounds_done": next_round}

    except Exception as exc:  # noqa: BLE001
        logger.exception("头脑风暴第 %s 轮执行失败：%s", next_round, exc)
        services.sessions.update(session_id, status="failed",
                                 meta=_read_meta(services, session_id, extra={"error": str(exc)}))
        _publish(session_id, "error", code=getattr(exc, "code", "INTERNAL_ERROR"),
                 message=str(exc))
        _publish(session_id, "done", session_id=session_id, failed=True)
        raise


def _moderator_profile(config: Dict[str, Any]) -> AgentProfile:
    return AgentProfile(
        id="moderator",
        name=str(config.get("name") or "主持人"),
        persona=(
            "你是一场学术头脑风暴的主持人。你负责拆解议题、汇总观点、"
            "识别真实共识与分歧，并在分歧处给出基于证据的判断。"
            "你不代替他人发言，也不引入讨论中未出现的论点。"
        ),
        tools=["rag_search", "paper_meta", "kg_query", "okf_read", "asset_list", "py_eval"],
    )


def _read_meta(services: Any, identifier: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    session = services.sessions.get(identifier) or {}
    meta = safe_json_loads(session.get("meta") or "", {})
    if extra:
        meta.update(extra)
    return meta


def _update_meta(services: Any, identifier: str, **values: Any) -> None:
    merged = _read_meta(services, identifier, extra=values)
    services.sessions.update(identifier, meta=merged)


def _run_round(
    runtime: Any,
    identifier: str,
    round_index: int,
    agent_rows: Sequence[Dict[str, Any]],
    sub_questions: Dict[str, str],
    shared_context: str,
    blackboard_text: str,
    speeches: Dict[str, str],
    steering: str,
    transcript: List[Dict[str, Any]],
) -> None:
    services = runtime.services
    _publish(identifier, "round", round=round_index,
             phase="independent" if round_index == 1 else "revise",
             agents=[row["id"] for row in agent_rows])

    for row in agent_rows:
        profile = AgentProfile(
            id=row["id"],
            name=row["name"],
            persona=row["persona"],
            tools=list(row.get("tools") or []),
        )
        question = sub_questions.get(row["id"]) or sub_questions.get(row["profile_id"]) or ""
        services.sessions.set_agent_status(identifier, row["id"], "running")

        text, usage, citations = _run_agent_round(
            runtime, identifier, row, profile, question, shared_context,
            blackboard_text, speeches.get(row["id"]), round_index, steering,
        )
        services.sessions.set_agent_status(identifier, row["id"], "done")

        speeches[row["id"]] = text
        transcript.append({
            "agent_id": row["id"],
            "name": row["name"],
            "round": round_index,
            "text": text,
        })
        services.sessions.add_message(
            identifier, role="assistant", content=text, agent_id=row["id"],
            round_index=round_index, citations=citations, usage=usage,
        )
        _publish(identifier, "message", agent_id=row["id"], agent_name=row["name"],
                 round=round_index, text=text, citations=citations)

    summary = _summarise_round(runtime, _moderator_profile({}), {}, round_index, agent_rows, speeches)
    if summary:
        services.sessions.add_blackboard(
            identifier, round_index=round_index, content=summary,
            author_agent="moderator", entry_type="summary",
        )
        services.sessions.add_message(identifier, role="blackboard", agent_id="moderator",
                                      content=summary, round_index=round_index)
        _publish(identifier, "blackboard", round=round_index, summary=summary)


def _run_synthesis(runtime: Any, identifier: str, moderator: AgentProfile,
                   config: Dict[str, Any], topic: str,
                   transcript: List[Dict[str, Any]], total_rounds: int) -> Dict[str, Any]:
    services = runtime.services
    _publish(identifier, "round", round=total_rounds + 1, phase="synthesis",
             agents=["moderator"])
    synthesis = _synthesise(runtime, moderator, config, topic, transcript,
                            _latest_blackboard(services, identifier))
    services.sessions.add_message(
        identifier, role="synthesis", agent_id="moderator",
        content=json.dumps(synthesis, ensure_ascii=False), round_index=total_rounds + 1,
    )
    _publish(identifier, "synthesis", **synthesis)
    services.sessions.update(identifier, status="done")
    _publish(identifier, "done", session_id=identifier)
    return synthesis


def _last_speeches(services: Any, session_id: str,
                   agent_rows: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    messages = services.sessions.list_messages(session_id)
    speeches: Dict[str, str] = {}
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        agent_id = message.get("agent_id") or ""
        if agent_id in (row["id"] for row in agent_rows) and agent_id not in speeches:
            speeches[agent_id] = message.get("content") or ""
    return speeches


def _load_transcript(services: Any, session_id: str) -> List[Dict[str, Any]]:
    messages = services.sessions.list_messages(session_id)
    transcript: List[Dict[str, Any]] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        transcript.append({
            "agent_id": message.get("agent_id") or "",
            "name": "",
            "round": int(message.get("round") or 0),
            "text": message.get("content") or "",
        })
    return transcript


# ---------------------------------------------------------------------------
# stages
# ---------------------------------------------------------------------------

def _chat_client(runtime: Any, provider_id: str = "", model: str = "") -> Any:
    services = runtime.services
    if provider_id and model:
        return services.registry.client_for(provider_id, model)
    client = services.registry.try_client(KIND_CHAT) or services.registry.try_client("parse")
    if client is None:
        from ..core.errors import ProviderNotConfigured

        raise ProviderNotConfigured("尚未配置问答/推理模型，无法进行头脑风暴。", kind="chat")
    return client


def _json_call(runtime: Any, system: str, user: str, *, provider_id: str = "",
               model: str = "", temperature: float = 0.2, max_tokens: int = 4096,
               retries: int = 1) -> Dict[str, Any]:
    """Call the model and recover a JSON object, retrying once on failure."""
    client = _chat_client(runtime, provider_id, model)
    messages = [Message.system(system), Message.user(user)]
    last_error: Optional[str] = None
    for attempt in range(retries + 1):
        result = client.chat(messages, temperature=temperature if attempt == 0 else 0.0,
                             max_tokens=max_tokens)
        text = result.text or ""
        try:
            return extract_json(text, expect="object")
        except JsonExtractionError as exc:
            last_error = str(exc)
            messages = messages[:1] + [
                Message.assistant(text[:2000]),
                Message.user(f"上一次返回无法解析为 JSON：{last_error}。请只输出合法 JSON 对象。"),
            ]
    raise JsonExtractionError(f"模型未返回合法 JSON：{last_error}")


def _paper_briefs(runtime: Any, hashes: Sequence[str]) -> List[Dict[str, Any]]:
    return runtime.services.retriever.paper_briefs(list(hashes))


def _decompose(runtime: Any, moderator: AgentProfile, config: Dict[str, Any],
               topic: str, agent_rows: Sequence[Dict[str, Any]],
               hashes: Sequence[str]) -> Dict[str, Any]:
    roles = [{"name": row["name"], "persona": row["persona"]} for row in agent_rows]
    briefs = _paper_briefs(runtime, hashes)
    try:
        return _json_call(
            runtime,
            brainstorm_prompts.decompose_system_prompt(),
            brainstorm_prompts.decompose_user_prompt(topic, roles, briefs),
            provider_id=str(config.get("provider") or ""),
            model=str(config.get("model") or ""),
            temperature=0.3,
        )
    except Exception as exc:  # noqa: BLE001 - a failed plan must not kill the session
        logger.warning("议题拆解失败，使用兜底子问题：%s", exc)
        return {
            "sub_questions": [{"role": row["id"], "question": topic,
                               "focus": row["persona"][:80]} for row in agent_rows],
            "shared_context": topic,
            "fallback": True,
        }


def _run_agent_round(
    runtime: Any,
    session_id: str,
    row: Dict[str, Any],
    profile: AgentProfile,
    question: str,
    shared_context: str,
    blackboard: str,
    previous: Optional[str],
    round_index: int,
    steering: str = "",
) -> tuple:
    """Run one agent for one round, inside its own isolation boundary.

    Returns ``(text, usage, citations)`` where ``text`` is natural prose — a
    distillation of the retrieved evidence with inline ``[n]`` citations and a
    trailing reference list — never a copy of the source or a JSON blob.
    """
    services = runtime.services

    tool_context = runtime.make_tool_context(
        session_id=session_id,
        agent_id=row["id"],
        allowed_hashes=row.get("paper_hashes") or None,
        allowed_tools=row.get("tools") or None,
        workspace=str(services.paths.resolve(row["workspace"])) if row.get("workspace") else None,
        round_index=round_index,
    )

    retrieval = services.retriever.search(question, hashes=row.get("paper_hashes") or None)
    evidences = services.retriever.hydrate_evidence(retrieval.get("evidences") or [])
    evidence_block = _format_evidence_with_refs(evidences)
    citations = [
        {"unit_id": item.get("unit_id"), "layer": item.get("layer"),
         "hash": item.get("hash"), "title": item.get("paper_title") or item.get("title"),
         "authors": item.get("authors") or [], "section": item.get("section"),
         "snippet": (item.get("text") or "")[:280], "score": item.get("score"),
         "asset_refs": item.get("asset_refs") or []}
        for item in evidences
    ]

    task = brainstorm_prompts.analysis_user_prompt(
        question, evidence_block, round_index=round_index,
        previous=previous, blackboard=blackboard, steering=steering,
    )
    system = brainstorm_prompts.analysis_system_prompt(
        profile.name, profile.persona, shared_context,
    )

    client = _chat_client(runtime, row.get("provider") or "", row.get("model") or "")
    settings = services.settings.current.agent
    messages = [Message.system(system), Message.user(task)]

    text = ""
    usage: Dict[str, Any] = {}
    try:
        result = client.chat(messages, temperature=settings.temperature + 0.1,
                             max_tokens=settings.max_tokens)
        text = (result.text or "").strip()
        usage = result.usage.to_dict()
    except Exception as exc:  # noqa: BLE001 - one agent failing must not stop the round
        logger.warning("角色 %s 第 %s 轮失败：%s", row["id"], round_index, exc)
        text = f"（本轮未能完成分析：{exc}）"

    return text, usage, citations


def _format_evidence_with_refs(evidences: Sequence[Dict[str, Any]]) -> str:
    """Number the retrieved evidence for inline [n] citations and a reference list.

    The reference format the model must reproduce is `[n] 作者，论文名，哈希`,
    so author and hash are surfaced here rather than only a title.
    """
    if not evidences:
        return "（没有检索到相关证据）"
    lines: List[str] = []
    for index, item in enumerate(evidences, start=1):
        authors = "、".join(str(a) for a in (item.get("authors") or []) if a)
        title = item.get("paper_title") or item.get("title") or "未命名论文"
        hash_value = str(item.get("hash") or "")[:12]
        section = item.get("section_label") or item.get("section") or ""
        header = f"[{index}] {title}"
        if authors:
            header += f"｜作者：{authors}"
        if section:
            header += f"｜{section}"
        if hash_value:
            header += f"｜哈希：{hash_value}"
        lines.append(f"{header}\n{str(item.get('text') or '')[:800]}")
    return "\n\n".join(lines)


def _summarise_round(runtime: Any, moderator: AgentProfile, config: Dict[str, Any],
                     round_index: int, agent_rows: Sequence[Dict[str, Any]],
                     speeches: Dict[str, str]) -> str:
    payload = []
    for row in agent_rows:
        text = speeches.get(row["id"])
        if text:
            payload.append({"name": row["name"], "text": text})
    if not payload:
        return ""
    try:
        client = _chat_client(runtime, str(config.get("provider") or ""),
                              str(config.get("model") or ""))
        result = client.chat(
            [Message.system(brainstorm_prompts.blackboard_system_prompt()),
             Message.user(brainstorm_prompts.blackboard_user_prompt(round_index, payload))],
            temperature=0.2,
        )
        return (result.text or "").strip()
    except Exception as exc:  # noqa: BLE001 - fall back to a mechanical summary
        logger.warning("黑板摘要生成失败，使用机械摘录：%s", exc)
        lines = []
        for row in agent_rows:
            text = speeches.get(row["id"]) or ""
            if text:
                lines.append(f"- {row['name']}：{text[:120]}")
        return "（自动摘录）\n" + "\n".join(lines)


def _synthesise(runtime: Any, moderator: AgentProfile, config: Dict[str, Any],
                topic: str, transcript: Sequence[Dict[str, Any]],
                blackboard: str) -> Dict[str, Any]:
    try:
        return _json_call(
            runtime,
            brainstorm_prompts.synthesis_system_prompt(),
            brainstorm_prompts.synthesis_user_prompt(topic, transcript, blackboard),
            provider_id=str(config.get("provider") or ""),
            model=str(config.get("model") or ""),
            temperature=0.2,
            max_tokens=4096,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("汇总失败：%s", exc)
        return {
            "synthesis": f"汇总未能生成：{exc}",
            "consensus": [],
            "disagreements": [],
            "evidence_summary": [],
            "open_questions": [],
            "recommendations": [],
            "failed": True,
        }


def _latest_blackboard(services: Any, session_id: str) -> str:
    entries = services.sessions.list_blackboard(session_id)
    if not entries:
        return ""
    return str(entries[-1].get("content") or "")
