"""LLM provider tests against a local mock OpenAI-compatible server.

No network is required: a threaded :mod:`http.server` speaks the wire protocol
so we can verify request shaping (vision/file parts, tools, retries), streaming
accumulation and error mapping deterministically.

    python tests/test_llm.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _harness import run_all  # noqa: E402

from paperopenclaw.config import Settings, SettingsStore  # noqa: E402
from paperopenclaw.core.errors import ProviderNotConfigured  # noqa: E402
from paperopenclaw.core.paths import DataPaths  # noqa: E402
from paperopenclaw.llm import (  # noqa: E402
    KIND_CHAT,
    KIND_PARSE,
    Attachment,
    LLMError,
    Message,
    OpenAICompatProvider,
    ProviderRegistry,
    ToolSpec,
)


# ---------------------------------------------------------------------------
# mock server
# ---------------------------------------------------------------------------

class _State:
    """Records what the client sent so tests can assert on it."""

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.fail_next = 0
        self.force_status: Optional[int] = None
        self.reply_text = "pong"
        self.embed_dim = 8


STATE = _State()


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:  # silence
        return

    # -- helpers -------------------------------------------------------
    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- routes --------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        if self.path.endswith("/models"):
            self._send_json(200, {"data": [{"id": "mock-large"}, {"id": "mock-small"}]})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        payload = self._read_json()
        STATE.requests.append({"path": self.path, "payload": payload,
                              "auth": self.headers.get("Authorization", "")})

        if STATE.fail_next > 0:
            STATE.fail_next -= 1
            self._send_json(429, {"error": {"message": "rate limited"}})
            return
        if STATE.force_status:
            status = STATE.force_status
            self._send_json(status, {"error": {"message": f"forced {status}"}})
            return

        if self.path.endswith("/embeddings"):
            inputs = payload.get("input") or []
            data = [
                {"index": index, "embedding": [float(index + 1)] * STATE.embed_dim}
                for index in range(len(inputs))
            ]
            self._send_json(200, {"data": list(reversed(data)), "model": payload.get("model")})
            return

        if payload.get("stream"):
            self._send_streaming()
            return

        self._send_json(200, {
            "id": "chatcmpl-mock",
            "model": payload.get("model"),
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": STATE.reply_text},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        })

    def _send_streaming(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        for piece in ["自", "注意力", "机制"]:
            chunk = {"choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()
        # A tool-call delta split across two frames, as real gateways do.
        for fragment in [{"index": 0, "id": "call_1",
                          "function": {"name": "rag_", "arguments": '{"que'}},
                         {"index": 0, "function": {"name": "search", "arguments": 'ry":"attention"}'}}]:
            chunk = {"choices": [{"index": 0, "delta": {"tool_calls": [fragment]}, "finish_reason": None}]}
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()
        final = {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                 "usage": {"prompt_tokens": 5, "completion_tokens": 9, "total_tokens": 14}}
        self.wfile.write(f"data: {json.dumps(final)}\n\n".encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class MockServer:
    def __init__(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}/v1"

    def __enter__(self) -> "MockServer":
        self.thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _provider(server: MockServer, **kwargs: Any) -> OpenAICompatProvider:
    return OpenAICompatProvider(server.base_url, api_key="sk-test-key",
                                model="mock-large", timeout=10, max_retries=0, **kwargs)


# ---------------------------------------------------------------------------
# request shaping
# ---------------------------------------------------------------------------

def test_chat_basic_and_usage():
    with MockServer() as server:
        STATE.fail_next = 0
        STATE.force_status = None
        STATE.reply_text = "pong"
        result = _provider(server).chat([Message.user("hi")], temperature=0.0, max_tokens=32)

        assert result.text == "pong"
        assert result.usage.prompt_tokens == 11
        assert result.usage.total_tokens == 18
        assert result.usage.calls == 1
        assert result.finish_reason == "stop"

        sent = STATE.requests[-1]
        assert sent["path"] == "/v1/chat/completions"
        assert sent["auth"] == "Bearer sk-test-key"
        assert sent["payload"]["model"] == "mock-large"
        assert sent["payload"]["temperature"] == 0.0
        assert sent["payload"]["max_tokens"] == 32
        assert sent["payload"]["stream"] is False
        assert sent["payload"]["messages"] == [{"role": "user", "content": "hi"}]


def test_vision_attachments_become_content_parts():
    with MockServer() as server:
        STATE.force_status = None
        message = Message.user(
            "解析这篇论文",
            [Attachment(kind="image", data=b"PNGDATA", mime="image/png", page=1)],
        )
        _provider(server).chat([message])
        content = STATE.requests[-1]["payload"]["messages"][0]["content"]
        assert isinstance(content, list)
        assert content[0] == {"type": "text", "text": "解析这篇论文"}
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_pdf_file_attachment_uses_file_part():
    """FR-3.1: the PDF itself is sent, not a transcription."""
    with MockServer() as server:
        STATE.force_status = None
        message = Message.user(
            "解析",
            [Attachment(kind="file", data=b"%PDF-1.4 fake", mime="application/pdf",
                        filename="paper.pdf")],
        )
        _provider(server).chat([message])
        part = STATE.requests[-1]["payload"]["messages"][0]["content"][1]
        assert part["type"] == "file"
        assert part["file"]["filename"] == "paper.pdf"
        assert part["file"]["file_data"].startswith("data:application/pdf;base64,")


def test_tools_are_serialised():
    with MockServer() as server:
        STATE.force_status = None
        tool = ToolSpec(name="rag_search", description="检索",
                        parameters={"type": "object", "properties": {"query": {"type": "string"}},
                                    "required": ["query"]})
        _provider(server).chat([Message.user("q")], tools=[tool])
        payload = STATE.requests[-1]["payload"]
        assert payload["tool_choice"] == "auto"
        assert payload["tools"][0]["type"] == "function"
        assert payload["tools"][0]["function"]["name"] == "rag_search"
        assert payload["tools"][0]["function"]["parameters"]["required"] == ["query"]


def test_tool_result_message_shape():
    with MockServer() as server:
        STATE.force_status = None
        messages = [
            Message.user("q"),
            Message.assistant("", tool_calls=[{"id": "call_1", "type": "function",
                                               "function": {"name": "rag_search", "arguments": "{}"}}]),
            Message.tool_result("call_1", "rag_search", '{"items": []}'),
        ]
        _provider(server).chat(messages)
        sent = STATE.requests[-1]["payload"]["messages"]
        assert sent[1]["tool_calls"][0]["id"] == "call_1"
        assert sent[2]["role"] == "tool"
        assert sent[2]["tool_call_id"] == "call_1"
        assert sent[2]["name"] == "rag_search"


# ---------------------------------------------------------------------------
# retries and errors
# ---------------------------------------------------------------------------

def test_retries_on_rate_limit_then_succeeds():
    with MockServer() as server:
        STATE.force_status = None
        STATE.fail_next = 1
        provider = OpenAICompatProvider(server.base_url, api_key="k", model="mock-large",
                                        timeout=10, max_retries=2)
        result = provider.chat([Message.user("hi")])
        assert result.text == "pong"
        assert len(STATE.requests) >= 2
        STATE.fail_next = 0


def test_client_error_is_not_retried():
    with MockServer() as server:
        STATE.force_status = 400
        provider = OpenAICompatProvider(server.base_url, api_key="k", model="mock-large",
                                        timeout=10, max_retries=3)
        before = len(STATE.requests)
        try:
            provider.chat([Message.user("hi")])
        except LLMError as exc:
            assert exc.status == 400
            assert "400" in str(exc)
        else:
            raise AssertionError("应当抛出 LLMError")
        assert len(STATE.requests) - before == 1, "4xx 不应重试"
        STATE.force_status = None


def test_unknown_model_name_raises():
    with MockServer() as server:
        provider = OpenAICompatProvider(server.base_url, api_key="k", model="")
        try:
            provider.chat([Message.user("hi")])
        except LLMError as exc:
            assert "模型名称" in str(exc)
        else:
            raise AssertionError("应当抛出 LLMError")


# ---------------------------------------------------------------------------
# streaming
# ---------------------------------------------------------------------------

def test_streaming_accumulates_text_and_tool_calls():
    with MockServer() as server:
        STATE.force_status = None
        provider = _provider(server)

        async def run() -> Dict[str, Any]:
            text = ""
            final = None
            async for event in provider.astream([Message.user("讲讲自注意力")]):
                if event["type"] == "delta":
                    text += event["text"]
                else:
                    final = event["result"]
            return {"text": text, "final": final}

        outcome = asyncio.run(run())
        assert outcome["text"] == "自注意力机制"
        final = outcome["final"]
        assert final is not None
        assert final.text == "自注意力机制"
        # Tool-call fragments are stitched back together by index.
        assert len(final.tool_calls) == 1
        call = final.tool_calls[0]
        assert call["id"] == "call_1"
        assert call["function"]["name"] == "rag_search"
        assert json.loads(call["function"]["arguments"]) == {"query": "attention"}
        assert final.usage.completion_tokens == 9


def test_streaming_surfaces_http_errors():
    with MockServer() as server:
        STATE.force_status = 500

        async def run() -> None:
            provider = _provider(server)
            async for _event in provider.astream([Message.user("hi")]):
                pass

        try:
            asyncio.run(run())
        except LLMError as exc:
            assert exc.status == 500
        else:
            raise AssertionError("应当抛出 LLMError")
        finally:
            STATE.force_status = None


# ---------------------------------------------------------------------------
# embeddings and probing
# ---------------------------------------------------------------------------

def test_embeddings_respect_index_order():
    with MockServer() as server:
        STATE.force_status = None
        vectors = _provider(server).embed(["a", "b", "c"])
        assert len(vectors) == 3
        # The mock returns reversed order; the client must restore it by index.
        assert vectors[0][0] == 1.0
        assert vectors[1][0] == 2.0
        assert vectors[2][0] == 3.0
        assert len(vectors[0]) == STATE.embed_dim


def test_embeddings_batch_over_16():
    with MockServer() as server:
        STATE.force_status = None
        vectors = _provider(server).embed([f"t{i}" for i in range(20)])
        assert len(vectors) == 20
        # 20 inputs at batch size 16 -> two requests.
        embedding_calls = [r for r in STATE.requests if r["path"].endswith("/embeddings")]
        assert len(embedding_calls) == 2


def test_probe_and_list_models():
    with MockServer() as server:
        STATE.force_status = None
        provider = _provider(server)
        detail = provider.probe(kind="chat")
        assert detail["latency_ms"] >= 0
        assert "sample" in detail
        assert provider.list_models() == ["mock-large", "mock-small"]


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

def _registry(server: MockServer) -> ProviderRegistry:
    import tempfile

    paths = DataPaths(tempfile.mkdtemp()).ensure_base()
    settings = Settings(
        providers=[{"id": "mock", "name": "Mock", "base_url": server.base_url,
                    "api_key": "sk-x", "models": ["mock-large"], "vision": True}],
        routing={"chat": {"provider": "mock", "model": "mock-large"},
                 "parse": {"provider": "mock", "model": "mock-large"}},
    )
    return ProviderRegistry(SettingsStore(paths, settings))


def test_registry_routes_and_caches():
    with MockServer() as server:
        STATE.force_status = None
        registry = _registry(server)
        first = registry.client(KIND_CHAT)
        second = registry.client(KIND_CHAT)
        assert first is second, "相同配置应复用客户端"

        client = registry.client(KIND_PARSE)
        assert client.model == "mock-large"

        status = registry.status()
        assert status["chat"]["configured"] is True
        assert status["parse"]["vision"] is True
        assert status["digest"]["configured"] is False


def test_registry_raises_when_unconfigured():
    import tempfile

    paths = DataPaths(tempfile.mkdtemp()).ensure_base()
    registry = ProviderRegistry(SettingsStore(paths, Settings()))
    try:
        registry.client(KIND_CHAT)
    except ProviderNotConfigured as exc:
        assert exc.code == "PROVIDER_NOT_CONFIGURED"
        assert "chat" in str(exc)
    else:
        raise AssertionError("应当抛出 ProviderNotConfigured")
    assert registry.try_client(KIND_CHAT) is None


def test_registry_invalidate_rebuilds():
    with MockServer() as server:
        registry = _registry(server)
        first = registry.client(KIND_CHAT)
        registry.invalidate()
        second = registry.client(KIND_CHAT)
        assert first is not second


def test_registry_rebuilds_when_api_key_changes():
    with MockServer() as server:
        registry = _registry(server)
        first = registry.client(KIND_CHAT)
        registry.store.update({"providers": [{"id": "mock", "base_url": server.base_url,
                                              "api_key": "sk-rotated", "models": ["mock-large"]}]})
        second = registry.client(KIND_CHAT)
        assert second is not first
        assert second.api_key == "sk-rotated"


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------

def _run_all() -> int:
    return run_all(globals(), verbose=True)


if __name__ == "__main__":
    raise SystemExit(_run_all())
