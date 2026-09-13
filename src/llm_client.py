from __future__ import annotations

try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(override=False)
except ImportError:
    import warnings as _warnings
    _warnings.warn("python-dotenv not installed; .env ignored, export env vars manually", RuntimeWarning)

"""Octopus/OpenAI/Anthropic-compatible vision client with fully flexible params.

Octopus gateway (bestruirui/octopus) exposes three OpenAI-compatible ingresses
and forwards them via looplj/axonhub transformers:

  POST /v1/chat/completions  -> llm.APIFormatOpenAIChatCompletion
  POST /v1/responses         -> llm.APIFormatOpenAIResponse
  POST /v1/messages          -> llm.APIFormatAnthropicMessage

The gateway does NOT validate business params — it only reads `model` and
`stream` for routing/selection, then forwards raw JSON.  Any official
OpenAI / Responses / Anthropic field is accepted (see llm/model.go and
transformer/{openai,openai/responses,anthropic}/model.go).

This module therefore exposes a PASSTHROUGH layer:

* low-level  : generic_request(endpoint, body, ...) — user builds any JSON
* typed      : chat_completions / responses_create / anthropic_messages
* convenience: chat_vision / responses_vision / anthropic_vision (base64 & URL)
               with detail/high support

All entry points accept base_url/model/api_key overrides and an open **kwargs
that is merged straight into the request body — nothing is hard-coded except
sensible defaults (temperature=0, max_tokens fallback).  Use extra_body style
via kwargs to pass any vendor-private field.
"""

import base64
import http.client
import json
import os
import random
import time
import urllib.parse
import warnings
from typing import Any

DEFAULT_BASE_URL = "http://YOUR_GATEWAY_HOST:2113/v1"
DEFAULT_MODEL = "OC/muse-spark-1.3-contributor-free"
DEFAULT_ENDPOINT_PATH = "/v1/chat/completions"
# W0: POST-only timeout default 90s; GET恒15s single-try. TIMEOUT保留为POST别名(向后兼容).
TIMEOUT = 90
POST_TIMEOUT = 90
GET_TIMEOUT = 15
# Default output-length ceiling.  Reasoning models (Muse 1.2/1.3) spend
# thousands of tokens thinking before emitting any text, so a small cap
# truncates the answer: the Responses API reports status=incomplete with an
# empty output array.  20000 leaves room for thinking plus a full OCR page.
MAX_TOKENS = 20000
MAX_OUTPUT_TOKENS = 20000

# Output-length control is spelled differently per endpoint: chat/completions
# takes max_tokens (or max_completion_tokens) while the Responses API takes
# max_output_tokens.  A caller may pass any of the three; the client
# normalises to the spelling the target endpoint understands, so one body
# never carries two competing limits.
_LENGTH_KEYS = ("max_tokens", "max_completion_tokens", "max_output_tokens")
_CHAT_LENGTH_PRIORITY = ("max_tokens", "max_completion_tokens", "max_output_tokens")
_RESPONSES_LENGTH_PRIORITY = ("max_output_tokens", "max_tokens", "max_completion_tokens")
_REASONING_EFFORT_KEYS = ("reasoning_effort", "reasoningEffort")

# Thinking level sent when the caller does not pick one.  Muse 1.2/1.3 default
# to a very high effort server-side, which can spend the entire output budget
# on reasoning tokens and leave the response empty (status=incomplete,
# output=[]).  "medium" is ample for OCR.  Override with
# LLM_OCR_REASONING_EFFORT; an empty value means "send no reasoning control".
DEFAULT_REASONING_EFFORT = os.environ.get("LLM_OCR_REASONING_EFFORT", "medium").strip()
RETRY_AFTER_CAP = 60


class HttpError(RuntimeError):
    """W0冻结契约第1条: status None=timeout/OSError/JSONDecode, 否则为HTTP状态码."""

    def __init__(self, status: int | None, body: Any = None):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {str(body)[:400] if body is not None else ''}")

# Canonical endpoint paths (relative to gateway /v1)
ENDPOINT_CHAT = "/v1/chat/completions"
ENDPOINT_RESPONSES = "/v1/responses"
ENDPOINT_MESSAGES = "/v1/messages"
# Backwards-compat alias
ENDPOINTS = {
    "chat": ENDPOINT_CHAT,
    "responses": ENDPOINT_RESPONSES,
    "messages": ENDPOINT_MESSAGES,
    ENDPOINT_CHAT: ENDPOINT_CHAT,
    ENDPOINT_RESPONSES: ENDPOINT_RESPONSES,
    ENDPOINT_MESSAGES: ENDPOINT_MESSAGES,
}


def _api_key() -> str:
    for name in ("LLM_OCR_KEY", "LLM_OCR_API_KEY", "OCTOPUS_API_KEY", "OCTOPUS_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _base_url(explicit: str | None = None) -> str:
    raw = (explicit or os.environ.get("LLM_OCR_BASE_URL") or os.environ.get("LLM_OCR_BASEURL") or DEFAULT_BASE_URL).strip()
    if not raw:
        raw = DEFAULT_BASE_URL
    if "://" not in raw:
        raw = "http://" + raw.lstrip("/")
    return raw.rstrip("/")


def _model(explicit: str | None = None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    for name in ("LLM_OCR_MODEL", "OCTOPUS_MODEL"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return DEFAULT_MODEL


def _normalize_endpoint(api_path: str) -> str:
    """Normalize shorthand like 'chat' -> '/v1/chat/completions'."""
    p = api_path.strip()
    if not p.startswith("/"):
        # allow 'chat', 'responses', 'messages'
        key = p.lower()
        if key in ENDPOINTS:
            return ENDPOINTS[key]
        # treat bare 'chat/completions' etc
        if not p.startswith("v1/"):
            p = "v1/" + p.lstrip("/")
        p = "/" + p
    # exact match through alias table
    if p in ENDPOINTS:
        return ENDPOINTS[p]
    return p


def _resolve_endpoint(base_url: str, api_path: str) -> tuple[str, int, str, str]:
    """Return (scheme, port, host, path) for http.client, avoiding /v1/v1 double-write."""
    api_path = _normalize_endpoint(api_path)
    # Normalize bare host:port without scheme (e.g. "YOUR_GATEWAY_HOST:2113/v1") via _base_url semantics
    if "://" not in base_url:
        base_url = "http://" + base_url.lstrip("/")
    parsed = urllib.parse.urlparse(base_url)
    scheme = (parsed.scheme or "http").lower()
    host = parsed.hostname or "YOUR_GATEWAY_HOST"
    base_path = (parsed.path or "").rstrip("/")

    # path handling: avoid double-write
    if base_path == api_path:
        endpoint_path = api_path
    elif base_path.endswith(api_path):
        endpoint_path = base_path
    elif base_path.endswith("/v1") and api_path.startswith("/v1/"):
        # e.g. base /v1 + /v1/chat -> /v1/chat
        endpoint_path = base_path + api_path[3:]
    elif base_path == "":
        endpoint_path = api_path
    else:
        endpoint_path = base_path + api_path

    if parsed.port is not None:
        port = parsed.port
    else:
        port = 443 if scheme == "https" else 80
        if host == "YOUR_GATEWAY_HOST" and port == 80:
            port = 2113
    return scheme, port, host, endpoint_path


def _endpoint_parts(base_url: str) -> tuple[str, int, str, str]:
    """Backwards-compat: default to chat endpoint."""
    return _resolve_endpoint(base_url, ENDPOINT_CHAT)


# Backwards-compat lazy exports (W0: 模块常量延迟解析, 函数内求值; LLM_OCR_PORT废弃读到即warn忽略)
class _LazyConst:
    def __init__(self, fn): self._fn = fn
    def __str__(self): return str(self._fn())
    def __repr__(self): return repr(self._fn())
    def __format__(self, spec): return format(self._fn(), spec)
    def __int__(self): return int(self._fn())
    def __eq__(self, o):
        try:
            v = self._fn()
        except Exception:
            return False
        if isinstance(o, _LazyConst):
            try:
                o = o._fn()
            except Exception:
                return False
        return v == o
    def __ne__(self, o): return not self.__eq__(o)
    def __bool__(self):
        try:
            return bool(self._fn())
        except Exception:
            return False
    def __hash__(self): return hash(("_LazyConst", str(self)))
    def __add__(self, o): return self._fn() + o
    def __radd__(self, o): return o + self._fn()


def _legacy_port() -> int:
    raw = os.environ.get("LLM_OCR_PORT", "").strip()
    if raw:
        warnings.warn("LLM_OCR_PORT is deprecated; base_url is the only endpoint. Ignoring LLM_OCR_PORT.", RuntimeWarning)
    try:
        return _resolve_endpoint(_base_url(), ENDPOINT_CHAT)[1]
    except ValueError:
        return 2113


HOST = _LazyConst(lambda: urllib.parse.urlparse(_base_url()).hostname or "YOUR_GATEWAY_HOST")
PORT = _LazyConst(lambda: _legacy_port())
KEY = _LazyConst(lambda: _api_key())
MODEL = _LazyConst(lambda: _model())
ENDPOINT = _LazyConst(lambda: _resolve_endpoint(_base_url(), ENDPOINT_CHAT)[3])


def _merge_body_kwargs(body: dict[str, Any], kwargs: dict[str, Any]) -> None:
    """Merge kwargs into body; None values are skipped.  Handles extra_json string."""
    for k, v in kwargs.items():
        if v is None:
            continue
        # allow explicit extra dict
        if k == "extra" and isinstance(v, dict):
            body.update(v)
            continue
        if k == "extra_json" and isinstance(v, str) and v.strip():
            try:
                extra = json.loads(v)
                if isinstance(extra, dict):
                    body.update(extra)
            except json.JSONDecodeError:
                raise ValueError(f"extra_json is not valid JSON: {v[:200]}")
            continue
        body[k] = v


def _coalesce_length(
    body: dict[str, Any],
    priority: tuple[str, ...],
    key: str,
    default: int,
) -> None:
    """Reduce every output-length spelling to the one this endpoint takes.

    ``priority`` lists the accepted spellings in preference order, so a caller
    that passed ``max_output_tokens`` to the chat endpoint still has the value
    honoured -- merely renamed -- instead of ending up beside the default
    ``max_tokens`` and sending two conflicting limits.
    """
    value: Any = None
    for name in priority:
        if body.get(name) is not None:
            value = body[name]
            break
    for name in _LENGTH_KEYS:
        body.pop(name, None)
    body[key] = value if value is not None else default


def _coalesce_reasoning_effort(body: dict[str, Any], *, responses_style: bool) -> None:
    """Normalise thinking-effort control and never forward a blank value.

    chat/completions uses a flat ``reasoning_effort``; the Responses API nests
    it as ``reasoning: {"effort": ...}``.  An empty string is not a valid level,
    so it is dropped rather than sent.
    """
    effort: Any = None
    explicit_blank = False
    for name in _REASONING_EFFORT_KEYS:
        raw = body.pop(name, None)
        if isinstance(raw, str):
            raw = raw.strip()
            if raw:
                effort = raw
            else:
                explicit_blank = True
        elif raw is not None:
            effort = raw

    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict):
        nested = reasoning.get("effort")
        if isinstance(nested, str) and not nested.strip():
            reasoning.pop("effort", None)
            explicit_blank = True
        elif nested and effort is None:
            effort = nested
    elif reasoning is not None:
        body.pop("reasoning", None)

    if effort is None:
        if explicit_blank:
            return
        effort = DEFAULT_REASONING_EFFORT
    if not effort:
        return
    if responses_style:
        nested = body.get("reasoning")
        if not isinstance(nested, dict):
            nested = {}
            body["reasoning"] = nested
        nested["effort"] = effort
    else:
        body.pop("reasoning", None)
        body["reasoning_effort"] = effort


def _http_hint(status: int | None, payload: Any, base_url: str, api_path: str) -> str:
    """W0: 4xx fail-fast映射提示(仅打印,不改变HttpError原样抛出)."""
    text = json.dumps(payload, ensure_ascii=True)[:500] if isinstance(payload, dict) else str(payload)[:500]
    low = text.lower()
    if status == 401:
        return "hint: 401 Key无效,检查LLM_OCR_KEY"
    if status == 404 and "files" in api_path:
        return "hint: /v1/files已知未实现(404),无需使用"
    if status == 400 and "model not found" in low:
        parsed = urllib.parse.urlparse(base_url if "://" in base_url else "http://" + base_url.lstrip("/"))
        host = (parsed.hostname or "").lower()
        octo = os.environ.get("OCTOPUS_MODEL", "").strip() or os.environ.get("OCTOPUS_API_KEY", "").strip() or os.environ.get("OCTOPUS_KEY", "").strip()
        if host != "api.openai.com" or octo:
            return "hint: 400 model not found, Octopus场景补OC/前缀 + 建议 python -m src.cli models"
        return ""
    if status == 400 and "input[0]" in text:
        return "hint: responses input必须包message [{role:user,content:[input_text,input_image]}]"
    return ""


def _sleep_retry(attempt: int, retry_after: str | None) -> None:
    try:
        wait = float(retry_after) if retry_after else 1.0 * (attempt + 1)
    except ValueError:
        wait = 1.0 * (attempt + 1)
    wait = min(wait, RETRY_AFTER_CAP) + random.uniform(0, 0.5)
    time.sleep(wait)


def _post_json(
    api_path: str,
    body: dict[str, Any],
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: int | None = None,
    attempts: int = 3,
) -> dict[str, Any]:
    """W0契约第2条: 仅HttpError(429/5xx)/timeout/OSError N次+jitter+min(Retry-After,60s); 400/401/403/404 fail-fast原样抛HttpError.
    attempts缺省3(非auto); auto legs传attempts=1实现single-try(§1.1/§11.3第6条). 耗尽后抛原HttpError(仍是RuntimeError子类).
    timeout None归一POST_TIMEOUT(POST-only; GET恒15s走_get_json)."""
    if timeout is None:
        timeout = POST_TIMEOUT
    resolved_key = (api_key.strip() if isinstance(api_key, str) and api_key.strip() else _api_key())
    if not resolved_key:
        raise RuntimeError("Missing API key: set LLM_OCR_KEY (or LLM_OCR_API_KEY / OCTOPUS_API_KEY) or pass api_key=")
    resolved_base = _base_url(base_url)
    scheme, port, host, endpoint_path = _resolve_endpoint(resolved_base, api_path)

    raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {resolved_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    last_error: Exception | None = None
    attempts = max(1, int(attempts))
    for attempt in range(attempts):
        retry_after: str | None = None
        try:
            if scheme == "https":
                conn: http.client.HTTPConnection = http.client.HTTPSConnection(host, port, timeout=timeout)
            else:
                conn = http.client.HTTPConnection(host, port, timeout=timeout)
            try:
                conn.request("POST", endpoint_path, body=raw_body, headers=headers)
                response = conn.getresponse()
                raw = response.read()
                status = response.status
                try:
                    retry_after = response.getheader("Retry-After")
                except Exception:
                    retry_after = None
            finally:
                conn.close()
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                # W0修正案(契约#1/#6): JSONDecode归None类(与timeout/OSError同类,可回退); http_status保留在body
                raise HttpError(None, {"http_status": status, "raw": raw[:4000].decode("utf-8", errors="replace")}) from exc
            # probe-friendly trace (ASCII-safe, no key)
            try:
                usage_preview = payload.get("usage") if isinstance(payload, dict) else None
                print(f"HTTP {status} endpoint={endpoint_path} base_url={resolved_base} usage={json.dumps(usage_preview, ensure_ascii=True) if isinstance(usage_preview, dict) else usage_preview}")
            except Exception:
                print(f"HTTP {status} endpoint={endpoint_path} base_url={resolved_base}")
            if status == 429 or 500 <= status <= 599:
                raise HttpError(status, payload)
            if status < 200 or status >= 300:
                hint = _http_hint(status, payload, resolved_base, endpoint_path)
                if hint:
                    print(hint)
                raise HttpError(status, payload)
            if not isinstance(payload, dict):
                raise HttpError(status, payload)
            return payload
        except HttpError as exc:
            if exc.status == 429 or (exc.status is not None and 500 <= exc.status <= 599):
                last_error = exc
                if attempt < attempts - 1:
                    print(f"Request failed, retry {attempt + 1}/{attempts}: {exc}")
                    _sleep_retry(attempt, retry_after)
                continue
            raise
        except (TimeoutError, OSError) as exc:
            last_error = HttpError(None, str(exc)[:400])
            if attempt < attempts - 1:
                print(f"Request failed, retry {attempt + 1}/{attempts}: {exc}")
                _sleep_retry(attempt, None)
    assert last_error is not None
    # W0: 耗尽抛原HttpError(429/5xx/None), 仍是RuntimeError子类, except RuntimeError兼容
    raise last_error  # type: ignore[misc]


def _get_json(
    path: str,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: int = GET_TIMEOUT,
) -> dict[str, Any]:
    """W0契约第3条: GET single-try永不重试,任何失败抛HttpError(调用方降级)."""
    resolved_key = (api_key.strip() if isinstance(api_key, str) and api_key.strip() else _api_key())
    if not resolved_key:
        raise RuntimeError("Missing API key: set LLM_OCR_KEY (or LLM_OCR_API_KEY / OCTOPUS_API_KEY) or pass api_key=")
    resolved_base = _base_url(base_url)
    scheme, port, host, endpoint_path = _resolve_endpoint(resolved_base, path if path.startswith("/") else "/" + path)
    try:
        if scheme == "https":
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(host, port, timeout=timeout)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            conn.request("GET", endpoint_path, headers={"Authorization": f"Bearer {resolved_key}", "Accept": "application/json"})
            response = conn.getresponse()
            raw = response.read()
            status = response.status
        finally:
            conn.close()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise HttpError(status, {"raw": raw[:2000].decode("utf-8", errors="replace")}) from exc
        if status < 200 or status >= 300:
            raise HttpError(status, payload if isinstance(payload, dict) else {"raw": str(payload)[:2000]})
        if not isinstance(payload, dict):
            raise HttpError(status, {"raw": str(payload)[:2000]})
        return payload
    except HttpError:
        raise
    except (TimeoutError, OSError) as exc:
        raise HttpError(None, str(exc)[:400]) from exc


def generic_request(
    body: dict[str, Any],
    *,
    endpoint: str = ENDPOINT_CHAT,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    attempts: int = 3,
    timeout: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Most flexible entry: POST any JSON body to any endpoint.
    
    - endpoint: '/v1/chat/completions' | '/v1/responses' | '/v1/messages' or shorthand 'chat'/'responses'/'messages'
    - body: dict that will be sent as JSON (model auto-injected if missing)
    - Returns (payload, usage) where payload is the full decoded JSON, usage is payload.get('usage', {})
    """
    api_path = _normalize_endpoint(endpoint)
    # inject model if missing and not an Anthropic call where model is optional? Anthropic model is also required by transformer validator
    if "model" not in body or not body["model"]:
        body = dict(body)  # copy
        body["model"] = _model(model)
    else:
        # still normalize through _model if caller passed explicit model kwarg overriding body
        if model is not None and model.strip():
            body = dict(body)
            body["model"] = _model(model)
    payload = _post_json(api_path, body, base_url=base_url, api_key=api_key, attempts=attempts, timeout=timeout)
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    if not isinstance(usage, dict):
        usage = {}
    return payload, usage


# ---------------------------------------------------------------------------
# Typed passthrough wrappers — all accept **kwargs that merge into body
# ---------------------------------------------------------------------------

def _extract_chat_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError(f"Response missing choices: {payload}")
    content = choices[0].get("message", {}).get("content", "")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return str(content).strip()


def _extract_responses_text(payload: dict[str, Any]) -> str:
    # Responses API: output is list of items; message items have content with output_text
    outputs = payload.get("output") or []
    texts: list[str] = []
    for item in outputs:
        if not isinstance(item, dict):
            continue
        # message item
        content = item.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") in ("output_text", "text") and isinstance(part.get("text"), str):
                    texts.append(part["text"])
        # also handle direct text field for some providers
        if isinstance(item.get("text"), str):
            texts.append(item["text"])
    if texts:
        return "\n".join(texts).strip()
    # fallback: choices style for gateways that convert back
    if "choices" in payload:
        return _extract_chat_content(payload)
    # fallback: output_text at top
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"].strip()
    return ""


def _extract_anthropic_text(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if isinstance(content, list):
        texts = [blk.get("text", "") for blk in content if isinstance(blk, dict) and blk.get("type") == "text"]
        return "\n".join(texts).strip()
    if isinstance(content, str):
        return content.strip()
    # fallback to chat style
    if "choices" in payload:
        return _extract_chat_content(payload)
    return ""


def chat_completions(
    messages: list[dict[str, Any]],
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    attempts: int = 3,
    timeout: int | None = None,
    **kwargs: Any,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Passthrough to POST /v1/chat/completions.  Any kwarg merges into body.
    
    Common kwargs (all optional, examples):
      temperature, top_p, seed, frequency_penalty, presence_penalty,
      max_tokens, max_completion_tokens, reasoning_effort, reasoning_budget,
      verbosity, response_format, stop, stream, store, service_tier, etc.
    Opt-in long output: chat_completions(..., **{"max_tokens": 100000}).
    
    Returns (text, usage, raw_payload).
    """
    body: dict[str, Any] = {"model": _model(model), "messages": messages}
    if "temperature" not in kwargs and "temperature" not in body:
        body["temperature"] = 0
    _merge_body_kwargs(body, kwargs)
    _coalesce_length(body, _CHAT_LENGTH_PRIORITY, "max_tokens", MAX_TOKENS)
    _coalesce_reasoning_effort(body, responses_style=False)
    payload, usage = generic_request(body, endpoint=ENDPOINT_CHAT, base_url=base_url, api_key=api_key, attempts=attempts, timeout=timeout)
    text = _extract_chat_content(payload)
    return text, usage, payload


def chat_text(
    prompt: str,
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    **kwargs: Any,
) -> tuple[str, dict[str, Any]]:
    """Text-only chat completion (backwards-compat returns 2-tuple)."""
    text, usage, _ = chat_completions([{"role": "user", "content": prompt}], base_url=base_url, model=model, api_key=api_key, **kwargs)
    return text, usage


def _request(
    messages: list[dict[str, Any]],
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    **kwargs: Any,
) -> tuple[str, dict[str, Any]]:
    """Backwards-compat internal helper (now passthrough)."""
    text, usage, _ = chat_completions(messages, base_url=base_url, model=model, api_key=api_key, **kwargs)
    return text, usage


# --- chat vision helpers (backwards-compat) ---

def chat_vision(
    png_bytes: bytes,
    prompt: str,
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    detail: str = "high",
    attempts: int = 3,
    timeout: int | None = None,
    **kwargs: Any,
) -> tuple[str, dict[str, Any]]:
    """Vision via base64 data URI on /v1/chat/completions.  Pass detail/high/low/auto and any kwargs."""
    if not isinstance(png_bytes, (bytes, bytearray)):
        raise TypeError("png_bytes must be bytes")
    if not png_bytes:
        raise ValueError("png_bytes is empty")
    encoded = base64.b64encode(bytes(png_bytes)).decode("ascii")
    content: list[dict[str, Any]] = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}", "detail": detail}},
    ]
    text, usage, _ = chat_completions([{"role": "user", "content": content}], base_url=base_url, model=model, api_key=api_key, attempts=attempts, timeout=timeout, **kwargs)
    return text, usage


def chat_vision_url(
    image_url: str,
    prompt: str,
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    detail: str = "high",
    attempts: int = 3,
    timeout: int | None = None,
    **kwargs: Any,
) -> tuple[str, dict[str, Any]]:
    """Vision via remote http(s) URL on /v1/chat/completions — gateway fetches the URL."""
    if not isinstance(image_url, str) or not image_url.strip():
        raise ValueError("image_url must be a non-empty string")
    url = image_url.strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"image_url must be http(s), got: {url[:120]}")
    content: list[dict[str, Any]] = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": url, "detail": detail}},
    ]
    text, usage, _ = chat_completions([{"role": "user", "content": content}], base_url=base_url, model=model, api_key=api_key, attempts=attempts, timeout=timeout, **kwargs)
    return text, usage


def chat_vision_auto(
    png_bytes_or_url: bytes | str,
    prompt: str,
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    detail: str = "high",
    attempts: int = 3,
    timeout: int | None = None,
    **kwargs: Any,
) -> tuple[str, dict[str, Any]]:
    """Auto-dispatch: bytes -> base64, http(s) string -> direct URL."""
    if isinstance(png_bytes_or_url, (bytes, bytearray)):
        return chat_vision(bytes(png_bytes_or_url), prompt, base_url=base_url, model=model, api_key=api_key, detail=detail, attempts=attempts, timeout=timeout, **kwargs)
    if isinstance(png_bytes_or_url, str) and png_bytes_or_url.strip().lower().startswith(("http://", "https://")):
        return chat_vision_url(png_bytes_or_url.strip(), prompt, base_url=base_url, model=model, api_key=api_key, detail=detail, attempts=attempts, timeout=timeout, **kwargs)
    raise TypeError("png_bytes_or_url must be bytes or http(s) URL string")


# ---------------------------------------------------------------------------
# Responses API (/v1/responses) — OpenAI Responses format
# ---------------------------------------------------------------------------

def responses_create(
    input: str | list[dict[str, Any]],
    *,
    instructions: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    attempts: int = 3,
    timeout: int | None = None,
    **kwargs: Any,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Passthrough to POST /v1/responses.
    
    input: str -> {"input": "text"};  list -> {"input": [{role, content:[input_text/input_image]}]}
    Common kwargs: max_output_tokens, temperature, top_p, reasoning{effort,summary}, text{format,verbosity},
                   tools, truncation, include, previous_response_id, stream, etc.
    Opt-in long output: responses_create(..., **{"max_output_tokens": 100000}).
    Returns (text, usage, raw_payload).
    """
    body: dict[str, Any] = {"model": _model(model)}
    if isinstance(input, str):
        body["input"] = input
    elif isinstance(input, list):
        body["input"] = input
    else:
        raise TypeError("input must be str or list[dict]")
    if instructions is not None:
        body["instructions"] = instructions
    _merge_body_kwargs(body, kwargs)
    _coalesce_length(body, _RESPONSES_LENGTH_PRIORITY, "max_output_tokens", MAX_OUTPUT_TOKENS)
    _coalesce_reasoning_effort(body, responses_style=True)
    payload, usage = generic_request(body, endpoint=ENDPOINT_RESPONSES, base_url=base_url, api_key=api_key, attempts=attempts, timeout=timeout)
    text = _extract_responses_text(payload)
    # if extraction failed, try to dump payload for debugging but return empty string
    return text, usage, payload


def responses_vision(
    png_bytes: bytes,
    prompt: str,
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    detail: str = "high",
    instructions: str | None = None,
    attempts: int = 3,
    timeout: int | None = None,
    **kwargs: Any,
) -> tuple[str, dict[str, Any]]:
    """Vision via Responses API with base64 data URI."""
    if not isinstance(png_bytes, (bytes, bytearray)):
        raise TypeError("png_bytes must be bytes")
    if not png_bytes:
        raise ValueError("png_bytes is empty")
    encoded = base64.b64encode(bytes(png_bytes)).decode("ascii")
    data_uri = f"data:image/png;base64,{encoded}"
    # Responses requires input as [{role:user, content:[input_text, input_image]}]
    input_items: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": data_uri, "detail": detail},
            ],
        }
    ]
    text, usage, _ = responses_create(input_items, instructions=instructions, base_url=base_url, model=model, api_key=api_key, attempts=attempts, timeout=timeout, **kwargs)
    return text, usage


def responses_vision_url(
    image_url: str,
    prompt: str,
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    detail: str = "high",
    instructions: str | None = None,
    attempts: int = 3,
    timeout: int | None = None,
    **kwargs: Any,
) -> tuple[str, dict[str, Any]]:
    """Vision via Responses API with remote http(s) URL (no base64)."""
    if not isinstance(image_url, str) or not image_url.strip():
        raise ValueError("image_url must be a non-empty string")
    url = image_url.strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"image_url must be http(s), got: {url[:120]}")
    input_items: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": url, "detail": detail},
            ],
        }
    ]
    text, usage, _ = responses_create(input_items, instructions=instructions, base_url=base_url, model=model, api_key=api_key, attempts=attempts, timeout=timeout, **kwargs)
    return text, usage


def responses_vision_auto(
    png_bytes_or_url: bytes | str,
    prompt: str,
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    detail: str = "high",
    instructions: str | None = None,
    attempts: int = 3,
    timeout: int | None = None,
    **kwargs: Any,
) -> tuple[str, dict[str, Any]]:
    if isinstance(png_bytes_or_url, (bytes, bytearray)):
        return responses_vision(bytes(png_bytes_or_url), prompt, base_url=base_url, model=model, api_key=api_key, detail=detail, instructions=instructions, attempts=attempts, timeout=timeout, **kwargs)
    if isinstance(png_bytes_or_url, str) and png_bytes_or_url.strip().lower().startswith(("http://", "https://")):
        return responses_vision_url(png_bytes_or_url.strip(), prompt, base_url=base_url, model=model, api_key=api_key, detail=detail, instructions=instructions, attempts=attempts, timeout=timeout, **kwargs)
    raise TypeError("png_bytes_or_url must be bytes or http(s) URL string")


# ---------------------------------------------------------------------------
# Anthropic Messages API (/v1/messages)
# ---------------------------------------------------------------------------

def _guess_media_type(data: bytes) -> str:
    if data[:8].startswith(b"\x89PNG"):
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def anthropic_messages(
    messages: list[dict[str, Any]],
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    max_tokens: int = 4096,
    timeout: int | None = None,
    **kwargs: Any,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Passthrough to POST /v1/messages (Anthropic format).
    
    messages: [{"role":"user","content":[{"type":"text","text":...},{"type":"image","source":{...}}]}]
    Common kwargs: temperature, top_p, top_k, system, thinking, tools, stop_sequences, stream, etc.
    Returns (text, usage, raw_payload).
    """
    warnings.warn("messages experimental, 不保证", UserWarning)
    body: dict[str, Any] = {"model": _model(model), "messages": messages, "max_tokens": max_tokens}
    # system can be passed as 'system' kwarg (str or list) or via kwargs
    _merge_body_kwargs(body, kwargs)
    payload, usage = generic_request(body, endpoint=ENDPOINT_MESSAGES, base_url=base_url, api_key=api_key)
    text = _extract_anthropic_text(payload)
    return text, usage, payload


def anthropic_vision(
    png_bytes: bytes,
    prompt: str,
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    max_tokens: int = 4096,
    timeout: int | None = None,
    system: str | None = None,
    **kwargs: Any,
) -> tuple[str, dict[str, Any]]:
    """Vision via Anthropic messages with base64 image."""
    warnings.warn("messages experimental, 不保证", UserWarning)
    if not isinstance(png_bytes, (bytes, bytearray)):
        raise TypeError("png_bytes must be bytes")
    if not png_bytes:
        raise ValueError("png_bytes is empty")
    media_type = _guess_media_type(bytes(png_bytes))
    encoded = base64.b64encode(bytes(png_bytes)).decode("ascii")
    content: list[dict[str, Any]] = [
        {"type": "text", "text": prompt},
        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": encoded}},
    ]
    messages = [{"role": "user", "content": content}]
    # allow system to be set either explicitly or via kwargs['system']
    if system is not None and "system" not in kwargs:
        kwargs["system"] = system
    text, usage, _ = anthropic_messages(messages, base_url=base_url, model=model, api_key=api_key, max_tokens=max_tokens, timeout=timeout, **kwargs)
    return text, usage


def anthropic_vision_url(
    image_url: str,
    prompt: str,
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    max_tokens: int = 4096,
    timeout: int | None = None,
    system: str | None = None,
    **kwargs: Any,
) -> tuple[str, dict[str, Any]]:
    """Vision via Anthropic messages with remote URL (source.type=url)."""
    warnings.warn("messages experimental, 不保证", UserWarning)
    if not isinstance(image_url, str) or not image_url.strip():
        raise ValueError("image_url must be a non-empty string")
    url = image_url.strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"image_url must be http(s), got: {url[:120]}")
    # guess media type from URL extension
    ext = url.lower().split("?")[0].rsplit(".", 1)[-1] if "." in url else "png"
    media_map = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
    media_type = media_map.get(ext, "image/png")
    content: list[dict[str, Any]] = [
        {"type": "text", "text": prompt},
        {"type": "image", "source": {"type": "url", "media_type": media_type, "url": url}},
    ]
    messages = [{"role": "user", "content": content}]
    if system is not None and "system" not in kwargs:
        kwargs["system"] = system
    text, usage, _ = anthropic_messages(messages, base_url=base_url, model=model, api_key=api_key, max_tokens=max_tokens, timeout=timeout, **kwargs)
    return text, usage


def anthropic_vision_auto(
    png_bytes_or_url: bytes | str,
    prompt: str,
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    max_tokens: int = 4096,
    timeout: int | None = None,
    system: str | None = None,
    **kwargs: Any,
) -> tuple[str, dict[str, Any]]:
    warnings.warn("messages experimental, 不保证", UserWarning)
    if isinstance(png_bytes_or_url, (bytes, bytearray)):
        return anthropic_vision(bytes(png_bytes_or_url), prompt, base_url=base_url, model=model, api_key=api_key, max_tokens=max_tokens, timeout=timeout, system=system, **kwargs)
    if isinstance(png_bytes_or_url, str) and png_bytes_or_url.strip().lower().startswith(("http://", "https://")):
        return anthropic_vision_url(png_bytes_or_url.strip(), prompt, base_url=base_url, model=model, api_key=api_key, max_tokens=max_tokens, timeout=timeout, system=system, **kwargs)
    raise TypeError("png_bytes_or_url must be bytes or http(s) URL string")


# ---------------------------------------------------------------------------
# Lane B: auto 1+1 + list_models (W0 _post_json/_get_json only called, not modified)
# ---------------------------------------------------------------------------

def _auto_is_url(value: bytes | str) -> bool:
    return isinstance(value, str) and value.strip().lower().startswith(("http://", "https://"))


def _is_transient_b64_400(exc: Exception) -> bool:
    """W2: 仅 invalid base64 这一种400可回退leg2 (上游偶发把合法b64判错); 其余400照旧fail-fast."""
    if getattr(exc, "status", None) != 400:
        return False
    try:
        text = json.dumps(getattr(exc, "body", None), ensure_ascii=True).lower()
    except (TypeError, ValueError):
        text = str(getattr(exc, "body", exc)).lower()
    return "invalid base64" in text


def auto_vision(
    png_or_url: bytes | str,
    prompt: str,
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    detail: str = "high",
    instructions: str | None = None,
    timeout: int | None = None,
    **kw: Any,
) -> tuple[str, dict[str, Any]]:
    """Auto 1+1: leg1 responses single-try, fallback leg2 chat single-try on retriable only.

    Leg1: bytes -> responses_vision / http(s) str -> responses_vision_url (attempts=1).
    Fallback to leg2 (chat_vision / chat_vision_url, attempts=1) only when leg1
    raises HttpError with status in (429, 500..599) or status is None
    (timeout/OSError/JSONDecode). HttpError(400/401/403/404) is raised directly
    with no fallback and no retry. Usage is returned as-is; endpoint marking
    is recorded by the caller at landing time.
    """
    kw.pop("attempts", None)
    kw_instructions = kw.pop("instructions", None)
    effective_instructions = instructions if instructions is not None else kw_instructions
    is_url = _auto_is_url(png_or_url)
    is_bytes = isinstance(png_or_url, (bytes, bytearray))
    if not is_bytes and not is_url:
        raise TypeError("png_or_url must be bytes or http(s) URL string")
    def _mark(usage: dict[str, Any], leg: int, ep_norm: str) -> dict[str, Any]:
        try:
            if isinstance(usage, dict) and "_leg" not in usage:
                usage = dict(usage)
                usage["_leg"] = leg
                usage.setdefault("_endpoint_normalized", ep_norm)
        except Exception:
            pass
        return usage
    try:
        if is_bytes:
            text, usage = responses_vision(bytes(png_or_url), prompt, base_url=base_url, model=model, api_key=api_key, detail=detail, timeout=timeout, instructions=effective_instructions, attempts=1, **kw)
        else:
            text, usage = responses_vision_url(png_or_url.strip(), prompt, base_url=base_url, model=model, api_key=api_key, detail=detail, timeout=timeout, instructions=effective_instructions, attempts=1, **kw)
        return text, _mark(usage, 1, "responses")
    except HttpError as exc:
        retriable = (exc.status is None) or (exc.status == 429) or (exc.status is not None and 500 <= exc.status <= 599)
        if not retriable and not _is_transient_b64_400(exc):
            raise
        if isinstance(exc.status, int) and not retriable:
            warnings.warn("responses 400 invalid base64 (transient upstream misjudge); single-try fallback to chat", UserWarning)
        if effective_instructions:
            chat_prompt = str(effective_instructions) + "\n" + str(prompt)
        else:
            chat_prompt = prompt
        if is_bytes:
            text, usage = chat_vision(bytes(png_or_url), chat_prompt, base_url=base_url, model=model, api_key=api_key, detail=detail, attempts=1, timeout=timeout, **kw)
        else:
            text, usage = chat_vision_url(png_or_url.strip(), chat_prompt, base_url=base_url, model=model, api_key=api_key, detail=detail, attempts=1, timeout=timeout, **kw)
        return text, _mark(usage, 2, "chat")


def list_models(
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: int = 15,
) -> list[dict[str, Any]]:
    """List models via GET /models; any failure degrades to [] with a warning.

    Compatible with {"data": [{"id", ...}]}; keeps id/owned_by/object/created.
    Missing-key guidance raised by _get_json is re-raised directly (no bare send).
    """
    try:
        payload = _get_json("/models", base_url=base_url, api_key=api_key, timeout=timeout)
    except RuntimeError as exc:
        if "Missing API key" in str(exc):
            raise
        warnings.warn("list_models failed, fill model manually: " + str(exc)[:200], UserWarning)
        return []
    except Exception as exc:
        warnings.warn("list_models failed, fill model manually: " + str(exc)[:200], UserWarning)
        return []
    try:
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            warnings.warn("list_models failed, fill model manually: bad payload", UserWarning)
            return []
        out: list[dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id")
            if not isinstance(model_id, str) or not model_id:
                continue
            entry: dict[str, Any] = {"id": model_id}
            for key in ("owned_by", "object", "created"):
                if key in item:
                    entry[key] = item[key]
            out.append(entry)
        return out
    except Exception as exc:
        warnings.warn("list_models failed, fill model manually: " + str(exc)[:200], UserWarning)
        return []
