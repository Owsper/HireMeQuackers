"""services.llm: provider choice, JSON parsing and every provider adapter.

Fake clients stand in for the SDKs (no network). Each recorded call is also
bound against the installed SDK's real method signature, so a wrong keyword
(like the old `generate_content(system_instruction=...)`) fails here.
"""
from __future__ import annotations

import inspect
import json
from types import SimpleNamespace as NS

import pytest

from database.models import Job
from services import ai_client, llm

SCHEMA = {"type": "object", "properties": {}}


def _ai(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return llm.get()


def _sdk(monkeypatch):
    monkeypatch.setattr(llm, "sdk_installed", lambda sdk: True)


def _bind(method, kw):
    inspect.signature(method).bind(None, **kw)


class _Rec:
    """Records kwargs, returns the next canned result (or raises it)."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, **kw):
        self.calls.append({k: (list(v) if isinstance(v, list) else v) for k, v in kw.items()})
        r = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(r, Exception):
            raise r
        return r


# ---- provider selection ---------------------------------------------------------


def test_no_key_means_off(monkeypatch):
    ai = _ai(monkeypatch)
    assert not ai.available and "GEMINI_API_KEY" in ai.reason and ai.label == "none"


def test_auto_order(monkeypatch):
    _sdk(monkeypatch)
    assert _ai(monkeypatch, OPENAI_API_KEY="o", ANTHROPIC_API_KEY="a").provider == "anthropic"
    assert _ai(monkeypatch, GEMINI_API_KEY="g").provider == "gemini"


def test_explicit_provider_and_model(monkeypatch):
    _sdk(monkeypatch)
    ai = _ai(monkeypatch, AI_PROVIDER=" OpenAI ", OPENAI_API_KEY="o", AI_MODEL="gpt-x")
    assert ai.available and ai.label == "openai:gpt-x" and ai.responses_api


def test_defaults_and_legacy_gemini_model(monkeypatch):
    _sdk(monkeypatch)
    assert _ai(monkeypatch, ANTHROPIC_API_KEY="a").model == "claude-opus-5-5"
    assert _ai(monkeypatch, GEMINI_API_KEY="g", GEMINI_MODEL="gemini-old").model == "gemini-old"
    assert _ai(monkeypatch, AI_MODEL="gemini-new").model == "gemini-new"


def test_missing_key_unknown_provider_missing_sdk(monkeypatch):
    assert "ANTHROPIC_API_KEY is not set" in _ai(monkeypatch, AI_PROVIDER="anthropic").reason
    assert "unknown AI_PROVIDER 'skynet'" in _ai(monkeypatch, AI_PROVIDER="skynet").reason
    monkeypatch.setattr(llm, "sdk_installed", lambda sdk: False)
    assert "pip install anthropic" in _ai(monkeypatch, AI_PROVIDER="anthropic", ANTHROPIC_API_KEY="a").reason


def test_ollama_and_base_url_use_chat_completions(monkeypatch):
    _sdk(monkeypatch)
    ai = _ai(monkeypatch, AI_PROVIDER="ollama")
    assert ai.available and ai.base_url == "http://localhost:11434/v1" and not ai.responses_api
    ai = _ai(monkeypatch, AI_PROVIDER="openai", OPENAI_API_KEY="o", OPENAI_BASE_URL="http://localhost:1234/v1")
    assert not ai.responses_api


def test_overview_marks_active_provider(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    rows = {r["provider"]: r for r in llm.overview()}
    assert rows["groq"]["active"] == "yes" and rows["groq"]["key"] == "set"
    assert rows["ollama"]["key"] == "not needed" and rows["gemini"]["active"] == ""


# ---- JSON parsing ---------------------------------------------------------------


@pytest.mark.parametrize("text, want", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 1}\n```', {"a": 1}),
    ('Sure [1]! Here: {"a": {"b": "}"}} hope it helps', {"a": {"b": "}"}}),
    ('[{"q": 1}, {"q": 2}]', [{"q": 1}, {"q": 2}]),
    ("no json at all", None),
    ("", None),
])
def test_parse_json(text, want):
    assert llm.parse_json(text) == want


def test_json_list_wrapped_or_bare():
    assert llm.json_list('{"questions": [{"q": 1}, "junk"]}', "questions") == [{"q": 1}]
    assert llm.json_list('[{"q": 1}]', "questions") == [{"q": 1}]
    assert llm.json_list('{"other": [{"q": 1}]}', "questions") == []


# ---- one-shot completions ---------------------------------------------------------


def test_gemini_puts_system_prompt_in_config(monkeypatch):
    genai = pytest.importorskip("google.genai")
    _sdk(monkeypatch)
    ai = _ai(monkeypatch, GEMINI_API_KEY="g")
    gen = _Rec(NS(text='{"ok": true}'))
    ai._client = NS(models=NS(generate_content=gen))
    assert ai.complete("SYS", "hi", json_mode=True) == '{"ok": true}'
    kw = gen.calls[0]
    assert kw["config"].system_instruction == "SYS" and kw["config"].response_mime_type == "application/json"
    _bind(genai.models.Models.generate_content, kw)


def test_openai_responses_complete(monkeypatch):
    pytest.importorskip("openai")
    from openai.resources.responses import Responses

    _sdk(monkeypatch)
    ai = _ai(monkeypatch, OPENAI_API_KEY="o")
    create = _Rec(NS(output_text='{"a": 1}'))
    ai._client = NS(responses=NS(create=create))
    assert ai.complete("SYS", "hi", json_mode=True) == '{"a": 1}'
    kw = create.calls[0]
    assert kw["instructions"] == "SYS" and kw["store"] is False
    assert kw["text"] == {"format": {"type": "json_object"}}
    _bind(Responses.create, kw)


def test_compat_retries_without_json_mode(monkeypatch):
    openai = pytest.importorskip("openai")
    import httpx
    from openai.resources.chat.completions import Completions

    _sdk(monkeypatch)
    ai = _ai(monkeypatch, AI_PROVIDER="ollama")
    bad = openai.BadRequestError("json mode unsupported", body=None,
                                 response=httpx.Response(400, request=httpx.Request("POST", "http://x")))
    create = _Rec(bad, NS(choices=[NS(message=NS(content='{"a": 1}'))]))
    ai._client = NS(chat=NS(completions=NS(create=create)))
    assert ai.complete("SYS", "hi", json_mode=True) == '{"a": 1}'
    first, second = create.calls
    assert first["response_format"] == {"type": "json_object"} and "response_format" not in second
    assert second["messages"][0] == {"role": "system", "content": "SYS"}
    _bind(Completions.create, first)


def test_claude_fallbacks_only_on_supported_models(monkeypatch):
    pytest.importorskip("anthropic")
    from anthropic.resources.beta.messages import Messages as BetaMessages
    from anthropic.resources.messages import Messages

    _sdk(monkeypatch)
    msg = NS(stop_reason="end_turn", content=[NS(type="thinking", thinking=""), NS(type="text", text="hello")])
    beta, plain = _Rec(msg), _Rec(msg)
    client = NS(beta=NS(messages=NS(create=beta)), messages=NS(create=plain))

    ai = _ai(monkeypatch, ANTHROPIC_API_KEY="a")
    ai._client = client
    assert ai.complete("SYS", "hi") == "hello"
    kw = beta.calls[0]
    assert kw["model"] == "claude-opus-5-5" and kw["fallbacks"] == "default"
    assert kw["betas"] == ["server-side-fallback-2026-07-01"] and kw["max_tokens"] == llm.MAX_TOKENS
    _bind(BetaMessages.create, kw)

    ai = _ai(monkeypatch, AI_MODEL="claude-haiku-5-5")
    ai._client = client
    ai.complete("SYS", "hi")
    assert "fallbacks" not in plain.calls[0] and plain.calls[0]["system"] == "SYS"
    _bind(Messages.create, plain.calls[0])


def test_refusal_empty_and_sdk_errors_become_llm_errors(monkeypatch):
    _sdk(monkeypatch)
    ai = _ai(monkeypatch, ANTHROPIC_API_KEY="a", AI_MODEL="claude-haiku-5-5")
    ai._client = NS(messages=NS(create=_Rec(NS(stop_reason="refusal", content=[]))))
    with pytest.raises(llm.LLMError, match="declined"):
        ai.complete("SYS", "hi")
    ai._client = NS(messages=NS(create=_Rec(NS(stop_reason="end_turn", content=[]))))
    with pytest.raises(llm.LLMError, match="empty response"):
        ai.complete("SYS", "hi")
    ai._client = NS(messages=NS(create=_Rec(RuntimeError("quota\nexceeded"))))
    with pytest.raises(llm.LLMError, match=r"anthropic:claude-haiku-5-5: request_failed \(RuntimeError\)"):
        ai.complete("SYS", "hi")
    with pytest.raises(llm.LLMError):
        _ai(monkeypatch, AI_PROVIDER="skynet").complete("SYS", "hi")


# ---- tool-calling chats ------------------------------------------------------------


RESULT = {"ok": True, "output": "id  title\n3  Data Intern"}


def test_gemini_chat_round_trip(monkeypatch):
    genai = pytest.importorskip("google.genai")
    _sdk(monkeypatch)
    ai = _ai(monkeypatch, GEMINI_API_KEY="g")
    send = _Rec()
    replies = [NS(function_calls=[NS(id="c1", name="get_status", args={"x": 1})], text=None),
               NS(function_calls=None, text="done")]
    chat_obj = NS(send_message=lambda m: (send.calls.append(m), replies.pop(0))[1])
    create = _Rec(chat_obj)
    ai._client = NS(chats=NS(create=create))
    chat = ai.chat("SYS", [("get_status", "desc", SCHEMA)])
    r = chat.send("hi")
    assert r == llm.Reply("", [llm.Call("c1", "get_status", {"x": 1})])
    assert chat.send_results([(r.calls[0], RESULT)]) == llm.Reply("done", [])
    fr = send.calls[1][0].function_response
    assert fr.id == "c1" and fr.name == "get_status" and fr.response == RESULT
    cfg = create.calls[0]["config"]
    assert cfg.system_instruction == "SYS" and cfg.automatic_function_calling.disable
    _bind(genai.chats.Chats.create, create.calls[0])


def test_openai_responses_chat_round_trip(monkeypatch):
    pytest.importorskip("openai")
    from openai.resources.responses import Responses

    _sdk(monkeypatch)
    ai = _ai(monkeypatch, OPENAI_API_KEY="o")
    fc = NS(type="function_call", call_id="c1", name="show_job", arguments='{"job_id": 3}')
    create = _Rec(NS(output=[NS(type="reasoning"), fc], output_text=""),
                  NS(output=[NS(type="message")], output_text="Job 3 fits"))
    ai._client = NS(responses=NS(create=create))
    chat = ai.chat("SYS", [("show_job", "d", SCHEMA)])
    r = chat.send("show 3")
    assert r.calls == [llm.Call("c1", "show_job", {"job_id": 3})]
    assert chat.send_results([(r.calls[0], RESULT)]).text == "Job 3 fits"
    kw = create.calls[1]
    assert kw["store"] is False and kw["include"] == ["reasoning.encrypted_content"]
    assert fc in kw["input"]  # earlier output items are replayed
    assert kw["input"][-1] == {"type": "function_call_output", "call_id": "c1", "output": json.dumps(RESULT)}
    assert kw["tools"][0] == {"type": "function", "name": "show_job", "description": "d",
                              "parameters": SCHEMA, "strict": False}
    _bind(Responses.create, kw)


def test_compat_chat_round_trip(monkeypatch):
    pytest.importorskip("openai")
    from openai.resources.chat.completions import Completions

    _sdk(monkeypatch)
    ai = _ai(monkeypatch, AI_PROVIDER="groq", GROQ_API_KEY="k")
    assert ai.base_url == "https://api.groq.com/openai/v1" and ai.model == "openai/gpt-oss-120b"
    tc = NS(id="t1", type="function", function=NS(name="get_status", arguments="not json"))
    create = _Rec(NS(choices=[NS(message=NS(content=None, tool_calls=[tc]))]),
                  NS(choices=[NS(message=NS(content="all good", tool_calls=None))]))
    ai._client = NS(chat=NS(completions=NS(create=create)))
    chat = ai.chat("SYS", [("get_status", "d", SCHEMA)])
    r = chat.send("status?")
    assert r.calls == [llm.Call("t1", "get_status", {})]  # bad JSON args -> {} (build_argv reports it)
    assert chat.send_results([(r.calls[0], RESULT)]).text == "all good"
    msgs = create.calls[1]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "tool"]
    assert msgs[2]["tool_calls"][0]["id"] == "t1"
    assert msgs[3] == {"role": "tool", "tool_call_id": "t1", "content": json.dumps(RESULT)}
    _bind(Completions.create, create.calls[1])


def test_claude_chat_round_trip(monkeypatch):
    pytest.importorskip("anthropic")
    from anthropic.resources.messages import Messages

    _sdk(monkeypatch)
    ai = _ai(monkeypatch, ANTHROPIC_API_KEY="a", AI_MODEL="claude-haiku-5-5")
    first = NS(stop_reason="tool_use", content=[NS(type="text", text="Looking."),
                                                NS(type="tool_use", id="tu1", name="list_jobs", input={"limit": 5})])
    create = _Rec(first, NS(stop_reason="end_turn", content=[NS(type="text", text="Here you go")]))
    ai._client = NS(messages=NS(create=create))
    chat = ai.chat("SYS", [("list_jobs", "d", SCHEMA)])
    r = chat.send("jobs?")
    assert r.calls == [llm.Call("tu1", "list_jobs", {"limit": 5})]
    failed = {"ok": False, "output": "Invalid arguments"}
    assert chat.send_results([(r.calls[0], failed)]).text == "Here you go"
    kw = create.calls[1]
    assert kw["messages"][1] == {"role": "assistant", "content": first.content}
    assert kw["messages"][2]["content"] == [{"type": "tool_result", "tool_use_id": "tu1",
                                             "content": json.dumps(failed), "is_error": True}]
    assert kw["tools"] == [{"name": "list_jobs", "description": "d", "input_schema": SCHEMA}]
    _bind(Messages.create, kw)


# ---- scoring through the layer ---------------------------------------------------------


class _FakeAI:
    available = True
    label = "fake:m1"
    reason = None

    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    def complete(self, system, prompt, json_mode=False):
        self.prompts.append(prompt)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@pytest.fixture
def scoring(monkeypatch):
    store = {}
    monkeypatch.setattr(ai_client, "cache_get", store.get)
    monkeypatch.setattr(ai_client, "cache_set", store.__setitem__)
    monkeypatch.setattr(ai_client, "record_usage", lambda **kw: None)

    def _mk(reply):
        client = ai_client.AIClient()
        client._ai = _FakeAI(reply)
        return client

    return _mk, store


def test_deterministic_score_handles_experienced_intern(sample_profile, sample_job_factory):
    # used to raise AttributeError ('bool' has no .lower()) for this exact pair
    result = ai_client.deterministic_score(sample_profile, Job(**sample_job_factory()))
    assert result["experience_fit"] == "unknown" and result["ai_model"] == "deterministic-fallback"
    assert result["total_score"] is None  # the fixture has only a snippet


def test_legacy_model_percentage_is_rejected_without_caching(scoring, sample_profile, sample_job_factory):
    mk, store = scoring
    reply = json.dumps({"total_score": 0.9, "experience_fit": "GREAT", "evidence": "one string",
                        "confidence": "high", "title_relevance": "exact"})
    client = mk(reply)
    from services.ai_diagnostics import SAMPLE_JD
    result = client.score_job_fit(sample_profile, Job(**sample_job_factory(description=SAMPLE_JD)))
    assert result["ai_model"] == "deterministic-fallback"
    assert result["fallback_reason"] == "invalid response"
    assert store == {}
    prompt = client._ai.prompts[0]
    assert "alex.smith@example.com" not in prompt and "000-000-0000" not in prompt
    assert '"description"' in prompt and "source_quote" in prompt


def test_failed_model_call_falls_back_without_caching(scoring, sample_profile, sample_job_factory):
    mk, store = scoring
    from services.ai_diagnostics import SAMPLE_JD
    result = mk(llm.LLMError("quota")).score_job_fit(sample_profile, Job(**sample_job_factory(description=SAMPLE_JD)))
    assert result["ai_model"] == "deterministic-fallback" and result["fallback_reason"]
    assert store == {}
    result = mk('{"no": "score"}').score_job_fit(sample_profile, Job(**sample_job_factory(description=SAMPLE_JD)))
    assert result["ai_model"] == "deterministic-fallback" and store == {}


def test_cache_key_depends_on_model(scoring, sample_profile, sample_job_factory):
    mk, _ = scoring
    job = Job(**sample_job_factory())
    a, b = mk("{}"), mk("{}")
    b._ai.label = "fake:m2"
    assert a._cache_key(sample_profile, job) != b._cache_key(sample_profile, job)


# ---- review fixes ----------------------------------------------------------------------


def test_empty_ollama_url_stays_local(monkeypatch):
    _sdk(monkeypatch)
    assert _ai(monkeypatch, AI_PROVIDER="ollama", OLLAMA_BASE_URL="").base_url == "http://localhost:11434/v1"


def test_openai_base_url_needs_no_key_but_openai_itself_does(monkeypatch):
    _sdk(monkeypatch)
    assert _ai(monkeypatch, AI_PROVIDER="openai", OPENAI_BASE_URL="http://localhost:1234/v1").available
    official = _ai(monkeypatch, OPENAI_BASE_URL="https://api.openai.com/v1")
    assert official.responses_api and "OPENAI_API_KEY" in official.reason


def test_compat_chat_echoes_reasoning_content(monkeypatch):
    _sdk(monkeypatch)
    ai = _ai(monkeypatch, AI_PROVIDER="openai", OPENAI_API_KEY="test-key", OPENAI_BASE_URL="https://api.deepseek.com", AI_MODEL="deepseek-x")
    tc = NS(id="t1", type="function", function=NS(name="get_status", arguments="{}"))
    create = _Rec(NS(choices=[NS(message=NS(content="", reasoning_content="think", tool_calls=[tc]))]),
                  NS(choices=[NS(message=NS(content="ok", tool_calls=None))]))
    ai._client = NS(chat=NS(completions=NS(create=create)))
    chat = ai.chat("SYS", [("get_status", "d", SCHEMA)])
    chat.send_results([(chat.send("hi").calls[0], RESULT)])
    assert create.calls[1]["messages"][2]["reasoning_content"] == "think"


def test_repeated_model_failures_do_not_permanently_disable_provider(scoring, monkeypatch, sample_profile, sample_job_factory):
    _sdk(monkeypatch)
    calls = []

    def boom(**kw):
        calls.append(kw)
        raise RuntimeError("connection refused")

    client = ai_client.AIClient()
    client._ai = _ai(monkeypatch, ANTHROPIC_API_KEY="a", AI_MODEL="claude-haiku-5-5")
    client._ai._client = NS(messages=NS(create=boom))
    from services.ai_diagnostics import SAMPLE_JD
    for i in range(4):
        result = client.score_job_fit(sample_profile, Job(**sample_job_factory(title=f"Data Intern {i}", description=SAMPLE_JD)))
        assert result["ai_model"] == "deterministic-fallback"
    assert len(calls) == 4 and client.is_configured
