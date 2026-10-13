"""Retrieval-grounded question answering (FR-11).

Flow: retrieve (C→B→A) → if the apex found nothing, answer with an explicit
"库中无相关内容" instead of calling the model → otherwise stream a cited answer.

The short-circuit matters. Asking a model to answer from an empty evidence block
is exactly the situation where it invents plausible content, so the retrieval
layer refuses to hand over an empty context and the answer is produced
deterministically (FR-11.4).
"""

from __future__ import annotations

import time
from typing import Any, AsyncIterator, Dict, List, Optional, Sequence

from ..core.hashing import stable_id
from ..core.logging import get_logger
from ..llm.base import Message
from ..llm.prompts import qa as qa_prompts
from ..llm.registry import KIND_CHAT
from .profiles import get_profile

logger = get_logger(__name__)


def _ensure_session(runtime: Any, session_id: Optional[str], question: str,
                    hashes: Optional[Sequence[str]]) -> str:
    services = runtime.services
    if session_id:
        session = services.sessions.get(session_id)
        if session is not None:
            return session_id
    identifier = session_id or f"sess_{stable_id('qa', question, time.time(), length=12)}"
    services.sessions.create(
        identifier,
        kind="qa",
        title=question[:60],
        topic=question[:200],
        paper_hashes=list(hashes or []),
    )
    return identifier


def _retrieve(runtime: Any, question: str, hashes: Optional[Sequence[str]],
              top_k: Optional[Dict[str, int]]) -> Dict[str, Any]:
    services = runtime.services
    result = services.retriever.search(question, hashes=hashes, top_k=top_k)
    evidences = services.retriever.hydrate_evidence(result.get("evidences") or [])
    result["evidences"] = evidences
    return result


def _scope_hashes(runtime: Any, hashes: Optional[Sequence[str]]) -> Optional[List[str]]:
    if hashes:
        return list(hashes)
    return None


def _okf_context(runtime: Any, retrieval: Dict[str, Any], max_chars: int = 2400) -> str:
    """Return an OKF原文 block when the pyramid did not reach layer A.

    The cascade is C→B→A; when A has nothing useful, the distilled layers alone
    may lack the detail the question needs, so the answer is grounded with the
    most relevant paper's OKF full text ("有必要时查看 OKF").
    """
    layers = retrieval.get("layers_used") or []
    if "A" in layers:
        return ""
    candidates = retrieval.get("candidates") or []
    if not candidates:
        return ""
    top = candidates[0]
    hash_value = str(top.get("hash") or "")
    title = str(top.get("title") or "")
    text = runtime.services.okf_fulltext(hash_value, max_chars)
    if not text:
        return ""
    return (
        f"\n\n=====\n以下是检索到的最相关论文《{title}》（哈希 {hash_value[:8]}）的 OKF 原文，"
        f"供你核对细节与原文措辞；引用其中的具体表述时请注明出处为「OKF 原文」。\n{text}"
    )


def answer_question(
    runtime: Any,
    *,
    question: str,
    hashes: Optional[Sequence[str]] = None,
    top_k: Optional[Dict[str, int]] = None,
    session_id: Optional[str] = None,
    agent_id: str = "qa",
    profile_id: str = "qa",
) -> Dict[str, Any]:
    """Non-streaming QA. Returns the answer plus its citations."""
    services = runtime.services
    question = (question or "").strip()
    if not question:
        return {"answer": "请输入问题。", "citations": [], "session_id": session_id or "",
                "layers_used": [], "empty": True}

    scope = _scope_hashes(runtime, hashes)
    session = _ensure_session(runtime, session_id, question, scope)
    services.sessions.add_message(session, role="user", content=question, agent_id=agent_id)

    retrieval = _retrieve(runtime, question, scope, top_k)
    citations = [qa_prompts.citation_entry(item) for item in retrieval.get("evidences") or []]

    if retrieval.get("empty") or not citations:
        answer = qa_prompts.no_answer_response(question)
        services.sessions.add_message(session, role="assistant", content=answer,
                                      agent_id=agent_id, citations=[])
        services.sessions.update(session, status="done")
        return {"answer": answer, "citations": [], "session_id": session,
                "layers_used": [], "empty": True, "usage": {}}

    profile = get_profile(profile_id)
    evidence_block = qa_prompts.format_evidence(retrieval["evidences"])
    evidence_block += _okf_context(runtime, retrieval)
    system = Message.system(qa_prompts.qa_system_prompt(evidence_block))
    user = Message.user(qa_prompts.qa_user_prompt(question))

    client = services.registry.try_client(KIND_CHAT) or services.registry.try_client("parse")
    if client is None:
        answer = "尚未配置问答模型，无法生成回答。检索已返回证据，请在设置页配置模型后重试。"
        services.sessions.add_message(session, role="assistant", content=answer,
                                      agent_id=agent_id, citations=citations)
        return {"answer": answer, "citations": citations, "session_id": session,
                "layers_used": retrieval.get("layers_used", []), "empty": False,
                "error": "PROVIDER_NOT_CONFIGURED"}

    route = services.registry.route_config(KIND_CHAT)
    try:
        result = client.chat(
            [system, user],
            temperature=services.settings.current.agent.temperature,
            max_tokens=services.settings.current.agent.max_tokens,
        )
        answer = (result.text or "").strip()
        usage = result.usage.to_dict()
    except Exception as exc:  # noqa: BLE001 - surface, never crash the request
        logger.warning("问答模型调用失败：%s", exc)
        answer = f"模型调用失败，无法生成回答：{exc}"
        usage = {}

    services.sessions.add_message(session, role="assistant", content=answer,
                                  agent_id=agent_id, citations=citations, usage=usage)
    services.sessions.update(session, status="done")
    return {
        "answer": answer,
        "citations": citations,
        "session_id": session,
        "layers_used": retrieval.get("layers_used", []),
        "empty": False,
        "usage": usage,
    }


async def stream_answer(
    runtime: Any,
    *,
    question: str,
    hashes: Optional[Sequence[str]] = None,
    top_k: Optional[Dict[str, int]] = None,
    session_id: Optional[str] = None,
    agent_id: str = "qa",
    profile_id: str = "qa",
) -> AsyncIterator[Dict[str, Any]]:
    """Streaming QA: ``citations`` -> ``delta``* -> ``done`` (or ``error``)."""
    services = runtime.services
    question = (question or "").strip()
    if not question:
        yield {"type": "delta", "text": "请输入问题。"}
        yield {"type": "done", "session_id": session_id or "", "usage": {}}
        return

    scope = _scope_hashes(runtime, hashes)
    session = _ensure_session(runtime, session_id, question, scope)
    services.sessions.add_message(session, role="user", content=question, agent_id=agent_id)

    yield {"type": "status", "message": "正在检索金字塔语义库"}

    retrieval = _retrieve(runtime, question, scope, top_k)
    citations = [qa_prompts.citation_entry(item) for item in retrieval.get("evidences") or []]
    yield {
        "type": "citations",
        "citations": citations,
        "layers_used": retrieval.get("layers_used", []),
        "candidates": retrieval.get("candidates", []),
        "empty": bool(retrieval.get("empty")),
    }

    if retrieval.get("empty") or not citations:
        answer = qa_prompts.no_answer_response(question)
        yield {"type": "delta", "text": answer}
        services.sessions.add_message(session, role="assistant", content=answer,
                                      agent_id=agent_id, citations=[])
        services.sessions.update(session, status="done")
        yield {"type": "done", "session_id": session, "usage": {}, "empty": True}
        return

    client = services.registry.try_client(KIND_CHAT) or services.registry.try_client("parse")
    if client is None:
        message = "尚未配置问答模型，无法生成回答。检索已返回证据，请在设置页配置模型后重试。"
        yield {"type": "delta", "text": message}
        services.sessions.add_message(session, role="assistant", content=message,
                                      agent_id=agent_id, citations=citations)
        yield {"type": "done", "session_id": session, "usage": {},
               "error": "PROVIDER_NOT_CONFIGURED"}
        return

    evidence_block = qa_prompts.format_evidence(retrieval["evidences"])
    evidence_block += _okf_context(runtime, retrieval)
    messages = [
        Message.system(qa_prompts.qa_system_prompt(evidence_block)),
        Message.user(qa_prompts.qa_user_prompt(question)),
    ]

    collected: List[str] = []
    usage: Dict[str, Any] = {}
    try:
        async for event in client.astream(
            messages,
            temperature=services.settings.current.agent.temperature,
            max_tokens=services.settings.current.agent.max_tokens,
        ):
            if event.get("type") == "delta":
                collected.append(event["text"])
                yield {"type": "delta", "text": event["text"]}
            elif event.get("type") == "done":
                usage = event["result"].usage.to_dict()
    except Exception as exc:  # noqa: BLE001
        logger.warning("问答流式调用失败：%s", exc)
        yield {"type": "error", "code": getattr(exc, "code", "PROVIDER_ERROR"),
               "message": f"模型调用失败：{exc}"}
        if not collected:
            collected.append(f"模型调用失败，无法生成回答：{exc}")

    answer = "".join(collected).strip()
    services.sessions.add_message(session, role="assistant", content=answer,
                                  agent_id=agent_id, citations=citations, usage=usage)
    services.sessions.update(session, status="done")
    yield {"type": "done", "session_id": session, "usage": usage,
           "layers_used": retrieval.get("layers_used", [])}
