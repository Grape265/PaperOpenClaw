"""OpenAI-compatible provider: chat, vision/file input, tools, streaming, embeddings.

One implementation covers OpenAI, DeepSeek, DashScope (compatible mode),
Moonshot, Zhipu, SiliconFlow, Ollama and anything else that speaks
``/chat/completions`` + ``/embeddings`` (FR-14.1).
"""

from __future__ import annotations

import base64
import json
import random
import time
from typing import Any, AsyncIterator, Dict, Iterable, Iterator, List, Optional, Sequence

import httpx

from ..core.logging import get_logger
from ..core.logging import redact
from .base import Attachment, LLMError, LLMResult, Message, ToolSpec, Usage, elapsed_ms

logger = get_logger(__name__)

RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class OpenAICompatProvider:
    """A thin, well-behaved client for the OpenAI wire protocol."""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        model: str = "",
        *,
        timeout: float = 600.0,
        max_retries: int = 2,
        extra_headers: Optional[Dict[str, str]] = None,
        provider_id: str = "",
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model
        self.timeout = float(timeout or 600.0)
        self.max_retries = max(0, int(max_retries or 0))
        self.extra_headers = dict(extra_headers or {})
        self.provider_id = provider_id

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------
    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self.extra_headers)
        return headers

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    @staticmethod
    def _build_payload(
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float,
        max_tokens: Optional[int],
        tools: Optional[Sequence[ToolSpec]],
        tool_choice: Optional[str],
        response_format: Optional[Dict[str, Any]],
        stream: bool,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [message.to_wire() for message in messages],
            "temperature": float(temperature),
            "stream": bool(stream),
        }
        if max_tokens:
            # Both keys are sent: some gateways only honour max_tokens, newer
            # OpenAI models require max_completion_tokens. Unknown keys are
            # ignored by every implementation we target.
            payload["max_tokens"] = int(max_tokens)
        if tools:
            payload["tools"] = [spec.to_wire() for spec in tools]
            payload["tool_choice"] = tool_choice or "auto"
        if response_format:
            payload["response_format"] = response_format
        return payload

    def _should_retry(self, status: int) -> bool:
        return status in RETRY_STATUS

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(20.0, (2 ** attempt) * 0.8) + random.uniform(0, 0.4)

    def _parse_response(self, payload: Dict[str, Any], started: float) -> LLMResult:
        choices = payload.get("choices") or []
        if not choices:
            raise LLMError("提供方返回了空的 choices", body=json.dumps(payload, ensure_ascii=False)[:2000])
        choice = choices[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        if isinstance(text, list):
            # Some gateways return content as a list of parts.
            text = "".join(
                part.get("text", "") for part in text if isinstance(part, dict)
            )
        tool_calls = message.get("tool_calls") or []
        return LLMResult(
            text=str(text),
            tool_calls=list(tool_calls),
            usage=Usage.from_wire(payload.get("usage")),
            finish_reason=str(choice.get("finish_reason") or ""),
            model=str(payload.get("model") or self.model),
            raw=payload if len(json.dumps(payload, default=str)) < 200_000 else {},
            elapsed_ms=elapsed_ms(started),
        )

    # ------------------------------------------------------------------
    # chat (synchronous: used by the ingest pipeline and agent tools)
    # ------------------------------------------------------------------
    def chat(
        self,
        messages: Sequence[Message],
        *,
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        tools: Optional[Sequence[ToolSpec]] = None,
        tool_choice: Optional[str] = None,
        response_format: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> LLMResult:
        target_model = model or self.model
        if not target_model:
            raise LLMError("未指定模型名称")
        payload = self._build_payload(
            messages, model=target_model, temperature=temperature, max_tokens=max_tokens,
            tools=tools, tool_choice=tool_choice, response_format=response_format, stream=False,
        )
        url = self._url("/chat/completions")
        started = time.monotonic()
        last_error: Optional[Exception] = None

        for attempt in range(self.max_retries + 1):
            try:
                with httpx.Client(timeout=timeout or self.timeout) as client:
                    response = client.post(url, headers=self._headers(), json=payload)
                if response.status_code >= 400:
                    body = response.text[:4000]
                    if self._should_retry(response.status_code) and attempt < self.max_retries:
                        logger.warning("模型调用返回 %s，第 %s 次重试", response.status_code, attempt + 1)
                        time.sleep(self._backoff(attempt))
                        continue
                    raise LLMError(
                        f"模型调用失败（HTTP {response.status_code}）：{redact(body)[:500]}",
                        status=response.status_code, body=body, retryable=False,
                    )
                data = response.json()
                return self._parse_response(data, started)
            except LLMError:
                raise
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt < self.max_retries:
                    logger.warning("模型调用网络异常（%s），第 %s 次重试", type(exc).__name__, attempt + 1)
                    time.sleep(self._backoff(attempt))
                    continue
                raise LLMError(f"模型调用网络异常：{exc}", retryable=True) from exc
            except httpx.HTTPError as exc:  # pragma: no cover - defensive
                raise LLMError(f"模型调用失败：{exc}") from exc

        raise LLMError(f"模型调用失败：{last_error}")

    # ------------------------------------------------------------------
    # chat (asynchronous streaming: used by SSE endpoints)
    # ------------------------------------------------------------------
    async def astream(
        self,
        messages: Sequence[Message],
        *,
        model: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: Optional[int] = None,
        tools: Optional[Sequence[ToolSpec]] = None,
        response_format: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Yield ``{"type": "delta", "text": ...}`` then one ``{"type": "done", ...}``.

        Tool calls are accumulated across deltas and reported in the terminal
        event, so callers that do not care about tool use can ignore everything
        except ``delta``/``done``.
        """
        target_model = model or self.model
        if not target_model:
            raise LLMError("未指定模型名称")
        payload = self._build_payload(
            messages, model=target_model, temperature=temperature, max_tokens=max_tokens,
            tools=tools, tool_choice="auto" if tools else None, response_format=response_format,
            stream=True,
        )
        url = self._url("/chat/completions")
        started = time.monotonic()
        text_parts: List[str] = []
        tool_buffer: Dict[int, Dict[str, Any]] = {}
        usage = Usage()
        finish_reason = ""

        try:
            async with httpx.AsyncClient(timeout=timeout or self.timeout) as client:
                async with client.stream("POST", url, headers=self._headers(), json=payload) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", errors="replace")[:4000]
                        raise LLMError(
                            f"模型流式调用失败（HTTP {response.status_code}）：{redact(body)[:500]}",
                            status=response.status_code, body=body,
                        )
                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        chunk = line[5:].strip()
                        if chunk == "[DONE]":
                            break
                        try:
                            event = json.loads(chunk)
                        except json.JSONDecodeError:
                            continue
                        if event.get("usage"):
                            usage = Usage.from_wire(event["usage"])
                        choices = event.get("choices") or []
                        if not choices:
                            continue
                        choice = choices[0]
                        if choice.get("finish_reason"):
                            finish_reason = str(choice["finish_reason"])
                        delta = choice.get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            text_parts.append(str(piece))
                            yield {"type": "delta", "text": str(piece)}
                        for call in delta.get("tool_calls") or []:
                            index = int(call.get("index") or 0)
                            slot = tool_buffer.setdefault(
                                index, {"id": "", "type": "function",
                                        "function": {"name": "", "arguments": ""}}
                            )
                            if call.get("id"):
                                slot["id"] = call["id"]
                            function = call.get("function") or {}
                            if function.get("name"):
                                slot["function"]["name"] += function["name"]
                            if function.get("arguments"):
                                slot["function"]["arguments"] += function["arguments"]
        except LLMError:
            raise
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise LLMError(f"模型流式调用网络异常：{exc}", retryable=True) from exc

        result = LLMResult(
            text="".join(text_parts),
            tool_calls=[tool_buffer[key] for key in sorted(tool_buffer)],
            usage=usage,
            finish_reason=finish_reason,
            model=target_model,
            elapsed_ms=elapsed_ms(started),
        )
        yield {"type": "done", "result": result}

    # ------------------------------------------------------------------
    # embeddings
    # ------------------------------------------------------------------
    def embed(self, texts: Sequence[str], *, model: Optional[str] = None,
              timeout: Optional[float] = None) -> List[List[float]]:
        """Call ``/embeddings``; returns one vector per input text."""
        if not texts:
            return []
        target_model = model or self.model
        if not target_model:
            raise LLMError("未指定嵌入模型名称")
        url = self._url("/embeddings")
        vectors: List[List[float]] = []

        # Batch to stay well below per-request limits.
        batch_size = 16
        for start in range(0, len(texts), batch_size):
            batch = [t if t.strip() else " " for t in texts[start : start + batch_size]]
            payload = {"model": target_model, "input": batch}
            last_error: Optional[Exception] = None
            for attempt in range(self.max_retries + 1):
                try:
                    with httpx.Client(timeout=timeout or self.timeout) as client:
                        response = client.post(url, headers=self._headers(), json=payload)
                    if response.status_code >= 400:
                        body = response.text[:2000]
                        if self._should_retry(response.status_code) and attempt < self.max_retries:
                            time.sleep(self._backoff(attempt))
                            continue
                        raise LLMError(
                            f"嵌入调用失败（HTTP {response.status_code}）：{redact(body)[:300]}",
                            status=response.status_code, body=body,
                        )
                    data = response.json().get("data") or []
                    # Providers may return items out of order; index is authoritative.
                    ordered = sorted(data, key=lambda item: int(item.get("index") or 0))
                    vectors.extend([list(item.get("embedding") or []) for item in ordered])
                    break
                except LLMError:
                    raise
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_error = exc
                    if attempt < self.max_retries:
                        time.sleep(self._backoff(attempt))
                        continue
                    raise LLMError(f"嵌入调用网络异常：{exc}", retryable=True) from exc
            else:  # pragma: no cover - defensive
                raise LLMError(f"嵌入调用失败：{last_error}")

        return vectors

    # ------------------------------------------------------------------
    # diagnostics
    # ------------------------------------------------------------------
    def probe(self, *, model: Optional[str] = None, kind: str = "chat") -> Dict[str, Any]:
        """Cheap connectivity check backing ``POST /settings/test``."""
        started = time.monotonic()
        target_model = model or self.model
        if kind == "embedding":
            vectors = self.embed(["ping"], model=target_model, timeout=min(self.timeout, 60.0))
            return {
                "latency_ms": elapsed_ms(started),
                "dimension": len(vectors[0]) if vectors else 0,
                "sample": f"dim={len(vectors[0]) if vectors else 0}",
            }
        result = self.chat(
            [Message.user("Reply with the single word: pong")],
            model=target_model, temperature=0.0, max_tokens=16, timeout=min(self.timeout, 60.0),
        )
        return {
            "latency_ms": elapsed_ms(started),
            "sample": (result.text or "").strip()[:80],
            "model": result.model,
        }

    def probe_vision(self, *, model: Optional[str] = None) -> Dict[str, Any]:
        """Check whether the model accepts image input (a tiny PNG probe).

        Raises :class:`LLMError` when the endpoint rejects the image, so the
        caller can report "该模型不能解析 PDF 图片" instead of discovering the
        failure mid-ingest.
        """
        started = time.monotonic()
        target_model = model or self.model
        tiny_png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
            "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        )
        attachment = Attachment(kind="image", data=tiny_png, mime="image/png", detail="low")
        result = self.chat(
            [Message.user("这张图片的宽是多少像素？只回答一个数字。", attachments=[attachment])],
            model=target_model, temperature=0.0, max_tokens=16, timeout=min(self.timeout, 60.0),
        )
        return {
            "latency_ms": elapsed_ms(started),
            "vision": True,
            "sample": (result.text or "").strip()[:80],
            "model": result.model,
        }

    def list_models(self) -> List[str]:
        try:
            with httpx.Client(timeout=min(self.timeout, 30.0)) as client:
                response = client.get(self._url("/models"), headers=self._headers())
            if response.status_code >= 400:
                return []
            data = response.json().get("data") or []
            return sorted(str(item.get("id")) for item in data if isinstance(item, dict))
        except Exception:  # pragma: no cover - optional capability
            return []
