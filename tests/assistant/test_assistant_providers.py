"""Provider contract tests: every provider type, end to end over a real socket.

Each profile is pointed at the local fake server with an explicit base URL
(allowed in local mode), so the real adapter, auth-header construction, codec
and outbound client all run. No live provider is called.
"""
from __future__ import annotations

import asyncio

import pytest

from .fake_llm import Scripted, anthropic_text_events, openai_text_events
from vivarium_workbench_assistant.config import ProviderInstance
from vivarium_workbench_assistant.providers.adapters import ProviderAdapter
from vivarium_workbench_assistant.providers.auth_google import GoogleTokenProvider
from vivarium_workbench_assistant.providers.base import ChatMessage, ChatRequest, ProviderError, TextDelta, TextPart
from vivarium_workbench_assistant.secrets import SecretStore

KEY = "sk-test-contract-0123456789abcdef"


def adapter(inst: ProviderInstance, store: SecretStore | None = None) -> ProviderAdapter:
    store = store or SecretStore()
    return ProviderAdapter(inst, mode="local", secrets=store, google=GoogleTokenProvider(store))


def stream_text(ad: ProviderAdapter, model: str = "m") -> str:
    async def go():
        out = ""
        async for ev in ad.stream_chat(ChatRequest(model=model, system="S",
                                                   messages=[ChatMessage("user", [TextPart("hello")])])):
            if isinstance(ev, TextDelta):
                out += ev.text
        return out
    return asyncio.run(go())


def with_key(inst: ProviderInstance, field: str = "api_key") -> SecretStore:
    store = SecretStore()
    store.set(inst, KEY, field=field, persist=False)
    return store


def test_anthropic_contract(fake_llm):
    inst = ProviderInstance(id="anthropic", type="anthropic", display_name="A", credential={"source": "session"},
                            base_url=fake_llm.base)
    store = with_key(inst)
    ad = adapter(inst, store)
    models = asyncio.run(ad.list_models())
    assert [m.id for m in models] == ["claude-fake"] and models[0].context_window == 200000
    fake_llm.queue(Scripted(events=anthropic_text_events("Hel", "lo")))
    assert stream_text(ad) == "Hello"
    post = fake_llm.posts()[-1]
    assert post["path"] == "/v1/messages"
    assert post["headers"]["x-api-key"] == KEY
    assert post["headers"]["anthropic-version"] == "2023-06-01"
    assert "authorization" not in post["headers"]
    assert post["json"]["system"] == "S" and post["json"]["stream"] is True


def test_openai_contract(fake_llm):
    inst = ProviderInstance(id="openai", type="openai", display_name="O", credential={"source": "session"},
                            base_url=fake_llm.base + "/v1")
    ad = adapter(inst, with_key(inst))
    assert {m.id for m in asyncio.run(ad.list_models())} == {"fake-model", "fake-tools"}
    fake_llm.queue(Scripted(events=openai_text_events("Hi", " there")))
    assert stream_text(ad) == "Hi there"
    post = fake_llm.posts()[-1]
    assert post["path"] == "/v1/chat/completions"
    assert post["headers"]["authorization"] == f"Bearer {KEY}"
    assert post["json"]["stream_options"] == {"include_usage": True}


def test_openrouter_attribution_is_opt_in(fake_llm):
    inst = ProviderInstance(id="or", type="openrouter", display_name="OR", credential={"source": "session"},
                            base_url=fake_llm.base + "/api/v1")
    store = with_key(inst)
    fake_llm.queue(Scripted(events=openai_text_events("a")))
    stream_text(adapter(inst, store))
    assert "x-openrouter-title" not in fake_llm.posts()[-1]["headers"]
    inst2 = inst.model_copy(update={"send_attribution_headers": True})
    fake_llm.queue(Scripted(events=openai_text_events("b")))
    stream_text(adapter(inst2, store))
    h = fake_llm.posts()[-1]["headers"]
    assert h["x-openrouter-title"] == "Vivarium Workbench" and "http-referer" not in h


def test_ai_studio_discovery_uses_native_list_with_header_key(fake_llm):
    inst = ProviderInstance(id="gemini", type="ai_studio", display_name="G", credential={"source": "session"},
                            base_url=fake_llm.base + "/v1beta/openai")
    ad = adapter(inst, with_key(inst))
    models = asyncio.run(ad.list_models())
    assert [m.id for m in models] == ["gemini-fake"]
    get = [r for r in fake_llm.requests if r["method"] == "GET"][-1]
    assert get["path"].startswith("/v1beta/models?pageSize=")
    assert get["headers"]["x-goog-api-key"] == KEY
    assert "key=" not in get["path"]             # never a key in the URL
    fake_llm.queue(Scripted(events=openai_text_events("gem")))
    assert stream_text(ad) == "gem"
    assert fake_llm.posts()[-1]["path"] == "/v1beta/openai/chat/completions"


def test_vertex_with_session_access_token(fake_llm):
    base = fake_llm.base + "/v1/projects/my-project/locations/global/endpoints/openapi"
    inst = ProviderInstance(id="vertex", type="vertex", display_name="V", project="my-project", location="global",
                            credential={"source": "session"}, base_url=base, manual_models=["google/gemini-x"])
    store = SecretStore()
    store.set(inst, "ya29.session-token-value-0123", field="access_token", persist=False)
    ad = adapter(inst, store)
    assert asyncio.run(ad.list_models()) == []            # no discovery on Vertex
    fake_llm.queue(Scripted(events=openai_text_events("v")))
    assert stream_text(ad, "google/gemini-x") == "v"
    post = fake_llm.posts()[-1]
    assert post["path"].endswith("/endpoints/openapi/chat/completions")
    assert post["headers"]["authorization"] == "Bearer ya29.session-token-value-0123"


def test_vertex_default_endpoint_is_derived_from_project_and_location():
    from vivarium_workbench_assistant.providers.http import destination_for
    inst = ProviderInstance(id="vertex", type="vertex", display_name="V", project="my-project",
                            location="us-central1", credential={"source": "adc"})
    d = destination_for(inst, mode="local")
    assert d.host == "us-central1-aiplatform.googleapis.com"
    assert d.base_url.endswith("/v1/projects/my-project/locations/us-central1/endpoints/openapi")


def test_local_ollama_style_no_key(fake_llm):
    inst = ProviderInstance(id="ollama", type="openai_compatible", display_name="Ollama", preset="custom",
                            credential={"source": "none"}, base_url=fake_llm.base + "/v1")
    ad = adapter(inst)
    result = asyncio.run(ad.validate())
    assert result.ok and result.models_count == 2
    fake_llm.queue(Scripted(events=openai_text_events("local!")))
    assert stream_text(ad) == "local!"
    assert "authorization" not in fake_llm.posts()[-1]["headers"]


def test_missing_key_is_a_config_error(fake_llm):
    inst = ProviderInstance(id="openai", type="openai", display_name="O", credential={"source": "session"},
                            base_url=fake_llm.base + "/v1")
    with pytest.raises(ProviderError) as ei:
        stream_text(adapter(inst))
    assert ei.value.kind == "config"


def test_auth_error_is_mapped_and_redacted(fake_llm):
    inst = ProviderInstance(id="openai", type="openai", display_name="O", credential={"source": "session"},
                            base_url=fake_llm.base + "/v1")
    fake_llm.queue(Scripted(status=401, json_body={"error": {"message": f"Incorrect API key provided: {KEY}"}}))
    with pytest.raises(ProviderError) as ei:
        stream_text(adapter(inst, with_key(inst)))
    assert ei.value.kind == "auth" and KEY not in ei.value.safe_message


def test_non_stream_response_is_explained(fake_llm):
    inst = ProviderInstance(id="local", type="openai_compatible", display_name="L", preset="custom",
                            credential={"source": "none"}, base_url=fake_llm.base + "/v1")
    fake_llm.queue(Scripted(raw=b"<html>not an api</html>", content_type="text/html"))
    with pytest.raises(ProviderError) as ei:
        stream_text(adapter(inst))
    assert "did not return a stream" in ei.value.safe_message


def test_google_auth_uses_adc_and_refreshes(monkeypatch):
    import datetime as dt

    import google.auth
    calls = {"refresh": 0}

    class Creds:
        token = None
        expiry = None
        valid = False

        def refresh(self, request):
            calls["refresh"] += 1
            self.token = f"ya29.fresh-token-{calls['refresh']:04d}-abcdef"
            self.expiry = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=1)

    creds = Creds()
    monkeypatch.setattr(google.auth, "default", lambda scopes=None: (creds, "proj"))
    inst = ProviderInstance(id="vertex", type="vertex", display_name="V", project="my-project", location="global",
                            credential={"source": "adc"})
    gp = GoogleTokenProvider(SecretStore())
    t1 = asyncio.run(gp.token(inst))
    t2 = asyncio.run(gp.token(inst))
    assert t1 == t2 and calls["refresh"] == 1                  # cached while fresh
    creds.expiry = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(seconds=60)
    t3 = asyncio.run(gp.token(inst))
    assert calls["refresh"] == 2 and t3 != t1                  # refreshed inside the 5-minute margin


def test_google_auth_missing_adc_is_actionable(monkeypatch):
    import google.auth
    import google.auth.exceptions

    def boom(scopes=None):
        raise google.auth.exceptions.DefaultCredentialsError("none")
    monkeypatch.setattr(google.auth, "default", boom)
    inst = ProviderInstance(id="vertex", type="vertex", display_name="V", project="my-project", location="global",
                            credential={"source": "adc"})
    with pytest.raises(ProviderError) as ei:
        asyncio.run(GoogleTokenProvider(SecretStore()).token(inst))
    assert ei.value.kind == "config" and "gcloud auth application-default login" in ei.value.safe_message


def test_key_file_must_exist(tmp_path):
    inst = ProviderInstance(id="vertex", type="vertex", display_name="V", project="my-project", location="global",
                            credential={"source": "key_file", "key_file_path": str(tmp_path / "missing.json")})
    with pytest.raises(ProviderError) as ei:
        asyncio.run(GoogleTokenProvider(SecretStore()).token(inst))
    assert ei.value.kind == "config"


@pytest.mark.parametrize("host,ok", [
    ("aiplatform.googleapis.com", True),
    ("us-central1-aiplatform.googleapis.com", True),
    ("europe-west4-aiplatform.googleapis.com", True),
    ("evil.com-aiplatform.googleapis.com", False),
    ("x.us-central1-aiplatform.googleapis.com", False),
    ("aiplatform.googleapis.com.evil.com", False),
    ("-aiplatform.googleapis.com", False),
])
def test_vertex_host_pinning(host, ok):
    from vivarium_workbench_assistant.providers.profiles import PROFILES, host_is_official
    assert host_is_official(PROFILES["vertex"], host) is ok
