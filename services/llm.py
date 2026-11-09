"""One interface over the AI providers the app can talk to.

Providers: Gemini, OpenAI, Anthropic and OpenAI-compatible endpoints
(OpenRouter, Groq, Ollama, or any server set in OPENAI_BASE_URL). Pick one
with AI_PROVIDER (default `auto` = the first provider whose key is set) and
override its model with AI_MODEL.

Every SDK is optional. When the active provider has no key or no SDK,
`available` is False and callers use their deterministic fallback.
"""
from __future__ import annotations

import importlib
import json
import logging
import time
from urllib.parse import urlparse
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

from config.settings import get_config

log = logging.getLogger("jobhunter.ai")


class Provider(NamedTuple):
    sdk: str
    key: str  # AppConfig field with the API key; "" = no key needed
    model: str
    base_url: str = ""


PROVIDERS: Dict[str, Provider] = {
    "gemini": Provider("gemini", "gemini_api_key", "gemini-3.8-flash"),
    "openai": Provider("openai", "openai_api_key", "gpt-6-luna"),
    "anthropic": Provider("anthropic", "anthropic_api_key", "claude-opus-5-5"),
    "openrouter": Provider("openai", "openrouter_api_key", "openrouter/auto", "https://openrouter.ai/api/v1"),
    "groq": Provider("openai", "groq_api_key", "openai/gpt-oss-120b", "https://api.groq.com/openai/v1"),
    "ollama": Provider("openai", "", "llama3.1", "http://localhost:11434/v1"),
}
AUTO_ORDER = ("gemini", "anthropic", "openai", "openrouter", "groq")

MODULES = {"gemini": "google.genai", "openai": "openai", "anthropic": "anthropic"}
PIP = {"gemini": "google-genai", "openai": "openai", "anthropic": "anthropic"}

# Claude models that take server-side refusal fallbacks
CLAUDE_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
MAX_TOKENS = 16000

Tool = Tuple[str, str, Dict[str, Any]]  # name, description, JSON schema of the arguments


class LLMError(RuntimeError):
    pass


class Call(NamedTuple):
    id: str
    name: str
    args: Dict[str, Any]


class Reply(NamedTuple):
    text: str
    calls: List[Call]


def sdk_installed(sdk: str) -> bool:
    try:
        importlib.import_module(MODULES[sdk])
    except ImportError:
        return False
    return True


def pick(cfg) -> str:
    """Name of the provider to use ("" when auto finds no key)."""
    name = (cfg.ai_provider or "auto").strip().lower()
    if name != "auto":
        return name
    return next((n for n in AUTO_ORDER if getattr(cfg, PROVIDERS[n].key, "")), "")


def get(cfg=None) -> "LLM":
    return LLM(cfg or get_config())


def overview(cfg=None) -> List[Dict[str, str]]:
    """One row per provider for `quackers ai-check`."""
    cfg = cfg or get_config()
    active = pick(cfg)
    rows = []
    for name, p in PROVIDERS.items():
        key = ("set" if getattr(cfg, p.key, "") else "missing") if p.key else "not needed"
        rows.append({
            "provider": name,
            "active": "yes" if name == active else "",
            "key": key,
            "sdk": "installed" if sdk_installed(p.sdk) else f"pip install {PIP[p.sdk]}",
            "default_model": p.model,
        })
    return rows


class LLM:
    """The configured provider + model."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.last_diagnostic: Dict[str, Any] = {}
        self.provider = pick(cfg)
        self.sdk = self.model = self.key = self.base_url = ""
        self.reason: Optional[str] = None
        self._client = None
        p = PROVIDERS.get(self.provider)
        if p is None:
            self.reason = (f"unknown AI_PROVIDER {self.provider!r} (use one of: auto, {', '.join(PROVIDERS)})"
                           if self.provider else
                           "no AI key in .env (set GEMINI_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY, ...)")
            return
        self.sdk = p.sdk
        self.model = cfg.ai_model or (cfg.gemini_model if self.provider == "gemini" else "") or p.model
        self.key = getattr(cfg, p.key, "") if p.key else ""
        # an empty OLLAMA_BASE_URL must never fall through to api.openai.com
        self.base_url = {"openai": cfg.openai_base_url, "ollama": cfg.ollama_base_url}.get(self.provider) or p.base_url
        # local OpenAI-compatible servers (LM Studio, vLLM, ...) usually take no key
        keyless = self.provider == "openai" and urlparse(self.base_url).hostname in {"localhost", "127.0.0.1", "::1"}
        if p.key and not self.key and not keyless:
            self.reason = f"{p.key.upper()} is not set in .env"
        elif not sdk_installed(self.sdk):
            self.reason = f"{PIP[self.sdk]} is not installed (pip install {PIP[self.sdk]})"

    @property
    def available(self) -> bool:
        return self.reason is None

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}" if self.model else "none"

    @property
    def responses_api(self) -> bool:
        # OpenAI itself gets the Responses API; compatible servers only speak Chat Completions
        return self.provider == "openai" and (not self.base_url or urlparse(self.base_url).hostname == "api.openai.com")

    def client(self):
        if self._client is None:
            if self.sdk == "gemini":
                from google import genai

                self._client = genai.Client(api_key=self.key, http_options={"timeout": int(self.cfg.ai_timeout_seconds * 1000), "retry_options": {"attempts": 1}})
            elif self.sdk == "openai":
                import openai

                self._client = openai.OpenAI(api_key=self.key or "unused", base_url=self.base_url or None,
                                             timeout=self.cfg.ai_timeout_seconds, max_retries=0)
            else:
                import anthropic

                self._client = anthropic.Anthropic(api_key=self.key, timeout=self.cfg.ai_timeout_seconds, max_retries=0)
        return self._client

    def complete(self, system: str, prompt: str, json_mode: bool = False) -> str:
        """One-shot answer as text (JSON text with json_mode). Raises LLMError."""
        if not self.available:
            raise LLMError(self.reason)
        def call():
            if self.sdk == "gemini":
                text = self._gemini(system, prompt, json_mode)
            elif self.sdk == "anthropic":
                text = claude_text(self.claude(system=system, messages=[{"role": "user", "content": prompt}]))
            elif self.responses_api:
                text = self._responses(system, prompt, json_mode)
            else:
                text = self._chat_completions(system, prompt, json_mode)
            if not isinstance(text, str) or not text.strip():
                raise LLMError("empty response")
            if json_mode and parse_json(text) is None:
                raise LLMError("malformed JSON response")
            return text
        return self.request("complete_json" if json_mode else "complete", call)

    def request(self, operation: str, call):
        """Bounded retries; diagnostics deliberately omit prompts, URLs and exception bodies."""
        started = time.monotonic()
        for attempt in range(1, self.cfg.ai_max_attempts + 1):
            try:
                result = call()
                self.last_diagnostic = dict(provider=self.provider, model=self.model, operation=operation,
                    success=True, response_validation="passed", retry_count=attempt - 1,
                    duration_seconds=round(time.monotonic() - started, 3), failure_reason=None)
                log.info("AI request %s", self.last_diagnostic)
                return result
            except Exception as exc:
                status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
                reason = failure_reason(exc)
                retryable = status in (408, 409, 429, 500, 502, 503, 504) or reason in {"timeout", "network_error", "provider_outage"}
                self.last_diagnostic = dict(provider=self.provider, model=self.model, operation=operation,
                    success=False, response_validation="failed" if isinstance(exc, LLMError) else "not_run",
                    retry_count=attempt - 1, duration_seconds=round(time.monotonic() - started, 3), failure_reason=reason)
                log.warning("AI request %s", self.last_diagnostic)
                if not retryable or attempt == self.cfg.ai_max_attempts:
                    raise LLMError(f"{self.label}: {reason}") from None
                delay = min(30, self.cfg.ai_backoff_seconds * 2 ** (attempt - 1))
                headers = getattr(getattr(exc, "response", None), "headers", {}) or {}
                try:
                    delay = min(30, max(delay, float(headers.get("retry-after", 0))))
                except (TypeError, ValueError):
                    pass
                time.sleep(delay)

    def chat(self, system: str, tools: Sequence[Tool]):
        """A multi-turn session where the model may call `tools`."""
        if not self.available:
            raise LLMError(self.reason)
        if self.sdk == "gemini":
            return GeminiChat(self, system, tools)
        if self.sdk == "anthropic":
            return ClaudeChat(self, system, tools)
        return ResponsesChat(self, system, tools) if self.responses_api else CompatChat(self, system, tools)

    def claude(self, **kw):
        kw.setdefault("max_tokens", MAX_TOKENS)
        c = self.client()
        if self.model in CLAUDE_FALLBACK_MODELS:
            r = c.beta.messages.create(model=self.model, betas=["server-side-fallback-2026-07-01"],
                                       fallbacks="default", **kw)
        else:
            r = c.messages.create(model=self.model, **kw)
        if r.stop_reason == "refusal":
            raise LLMError(f"{self.label} declined the request")
        return r

    def _gemini(self, system, prompt, json_mode):
        from google.genai import types

        cfg = types.GenerateContentConfig(system_instruction=system,
                                          response_mime_type="application/json" if json_mode else None)
        return self.client().models.generate_content(model=self.model, contents=prompt, config=cfg).text

    def _responses(self, system, prompt, json_mode):
        kw: Dict[str, Any] = {"model": self.model, "instructions": system, "input": prompt, "store": False}
        if json_mode:
            kw["text"] = {"format": {"type": "json_object"}}
        return self.client().responses.create(**kw).output_text

    def _chat_completions(self, system, prompt, json_mode):
        import openai

        kw: Dict[str, Any] = {"model": self.model, "messages": [
            {"role": "system", "content": system}, {"role": "user", "content": prompt}]}
        if json_mode:
            kw["response_format"] = {"type": "json_object"}
        try:
            r = self.client().chat.completions.create(**kw)
        except openai.BadRequestError as exc:
            # Retry only a known unsupported response-format option, not auth/content/model errors.
            if not json_mode or not any(x in str(exc).lower() for x in ("response_format", "json mode", "json_object")):
                raise
            # some local models reject JSON mode; the prompt still asks for JSON
            del kw["response_format"]
            r = self.client().chat.completions.create(**kw)
        return r.choices[0].message.content


def failure_reason(exc: Exception) -> str:
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status in (401, 403):
        return "authentication_or_permission_error"
    if status == 429:
        return "rate_limit_or_quota_exceeded"
    if status == 404:
        return "model_or_endpoint_not_found"
    if status in (400, 422):
        return "invalid_request_or_model_parameters"
    if isinstance(status, int) and status >= 500:
        return "provider_outage"
    name = type(exc).__name__.lower()
    if "timeout" in name or isinstance(exc, TimeoutError):
        return "timeout"
    if "connection" in name or isinstance(exc, ConnectionError):
        return "network_error"
    if isinstance(exc, LLMError):
        # Only locally constructed validation messages are exposed.
        for label in ("empty response", "malformed JSON response", "declined", "invalid response"):
            if label in str(exc):
                return label
        return "response_validation_failed"
    return f"request_failed ({type(exc).__name__})" + (f" HTTP {status}" if isinstance(status, int) else "")


# ---- tool-calling chats: send(text) / send_results([(call, result_dict)]) -> Reply ----


class GeminiChat:
    def __init__(self, ai: LLM, system: str, tools: Sequence[Tool]):
        self._ai = ai
        from google.genai import types

        decls = [types.FunctionDeclaration(name=n, description=d, parameters_json_schema=s) for n, d, s in tools]
        cfg = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=decls)],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        self._chat = ai.client().chats.create(model=ai.model, config=cfg)

    def send(self, text: str) -> Reply:
        return self._reply(self._ai.request("chat", lambda: self._chat.send_message(text)))

    def send_results(self, results) -> Reply:
        from google.genai import types

        parts = [types.Part(function_response=types.FunctionResponse(id=c.id or None, name=c.name, response=out))
                 for c, out in results]
        return self._reply(self._ai.request("chat", lambda: self._chat.send_message(parts)))

    def _reply(self, r) -> Reply:
        calls = [Call(c.id or "", c.name, dict(c.args or {})) for c in r.function_calls or []]
        return Reply("" if calls else (r.text or ""), calls)


class ResponsesChat:
    def __init__(self, ai: LLM, system: str, tools: Sequence[Tool]):
        self._ai = ai
        self._system = system
        self._tools = [{"type": "function", "name": n, "description": d, "parameters": s, "strict": False}
                       for n, d, s in tools]
        self._input: List[Any] = []

    def send(self, text: str) -> Reply:
        self._input.append({"role": "user", "content": text})
        return self._next()

    def send_results(self, results) -> Reply:
        self._input += [{"type": "function_call_output", "call_id": c.id, "output": json.dumps(out)}
                        for c, out in results]
        return self._next()

    def _next(self) -> Reply:
        # store=False keeps the CV off OpenAI's servers; reasoning is carried client-side instead
        r = self._ai.request("chat", lambda: self._ai.client().responses.create(
            model=self._ai.model, instructions=self._system, input=self._input, tools=self._tools,
            store=False, include=["reasoning.encrypted_content"]))
        self._input += r.output
        calls = [Call(o.call_id, o.name, _args(o.arguments)) for o in r.output if o.type == "function_call"]
        return Reply("" if calls else r.output_text, calls)


class CompatChat:
    def __init__(self, ai: LLM, system: str, tools: Sequence[Tool]):
        self._ai = ai
        self._tools = [{"type": "function", "function": {"name": n, "description": d, "parameters": s}}
                       for n, d, s in tools]
        self._msgs: List[Dict[str, Any]] = [{"role": "system", "content": system}]

    def send(self, text: str) -> Reply:
        self._msgs.append({"role": "user", "content": text})
        return self._next()

    def send_results(self, results) -> Reply:
        self._msgs += [{"role": "tool", "tool_call_id": c.id, "content": json.dumps(out)} for c, out in results]
        return self._next()

    def _next(self) -> Reply:
        m = self._ai.request("chat", lambda: self._ai.client().chat.completions.create(
            model=self._ai.model, messages=self._msgs, tools=self._tools)).choices[0].message
        tcs = [t for t in m.tool_calls or [] if t.type == "function"]
        msg: Dict[str, Any] = {"role": "assistant", "content": m.content or ""}
        reasoning = getattr(m, "reasoning_content", None)
        if reasoning:
            # DeepSeek-style thinking models need it echoed back during tool calls
            msg["reasoning_content"] = reasoning
        if tcs:
            msg["tool_calls"] = [{"id": t.id, "type": "function",
                                  "function": {"name": t.function.name, "arguments": t.function.arguments}}
                                 for t in tcs]
        self._msgs.append(msg)
        calls = [Call(t.id, t.function.name, _args(t.function.arguments)) for t in tcs]
        return Reply("" if calls else (m.content or ""), calls)


class ClaudeChat:
    def __init__(self, ai: LLM, system: str, tools: Sequence[Tool]):
        self._ai = ai
        self._system = system
        self._tools = [{"name": n, "description": d, "input_schema": s} for n, d, s in tools]
        self._msgs: List[Dict[str, Any]] = []

    def send(self, text: str) -> Reply:
        self._msgs.append({"role": "user", "content": text})
        return self._next()

    def send_results(self, results) -> Reply:
        self._msgs.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": c.id, "content": json.dumps(out),
             "is_error": not out.get("ok", True)}
            for c, out in results]})
        return self._next()

    def _next(self) -> Reply:
        r = self._ai.request("chat", lambda: self._ai.claude(system=self._system, tools=self._tools, messages=self._msgs))
        # keep every block (tool_use, thinking, fallback) so the next turn is valid
        self._msgs.append({"role": "assistant", "content": r.content})
        calls = [Call(b.id, b.name, dict(b.input or {})) for b in r.content if b.type == "tool_use"]
        return Reply("" if calls else claude_text(r), calls)


# ---- helpers ----


def claude_text(r) -> str:
    return "".join(b.text for b in r.content if b.type == "text")


def parse_json(text: str):
    """First JSON object (or list of objects) in a model reply; tolerates fences and prose."""
    t = text or ""
    dec = json.JSONDecoder()
    for i, ch in enumerate(t):
        if ch not in "{[":
            continue
        try:
            obj, _ = dec.raw_decode(t, i)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) or (isinstance(obj, list) and obj and all(isinstance(x, dict) for x in obj)):
            return obj
    return None


def json_list(text: str, key: str) -> List[Dict[str, Any]]:
    """`{key: [...]}` or a bare `[...]` from a model reply, as a list of dicts."""
    obj = parse_json(text)
    if isinstance(obj, dict):
        obj = obj.get(key)
    return [x for x in obj if isinstance(x, dict)] if isinstance(obj, list) else []


def _args(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        obj = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _safe(exc: Exception) -> str:
    return str(exc)[:300].replace("\n", " ")
