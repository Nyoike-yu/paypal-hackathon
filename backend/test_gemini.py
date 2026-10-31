"""gemini.py translates between the app's Messages-style calls and Google's SDK. Pure: no network, no key, no database. Run with: pytest --noconftest test_gemini.py"""
import base64, json
from types import SimpleNamespace
import pytest
from google.genai import types
import gemini

def reply(*parts):
    return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=list(parts)))])

def call_part(name="buy", args=None, id=None, sig=b"sig"):
    return types.Part(function_call=types.FunctionCall(id=id, name=name, args=args or {"product_id": 5}), thought_signature=sig)

# ---------- what we send ----------
def test_roles_and_plain_text():
    c = gemini.to_contents([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}, {"role": "user", "content": "buy it"}])
    assert [x.role for x in c] == ["user", "model", "user"] and c[0].parts[0].text == "hi"

def test_photo_becomes_inline_bytes():
    img = base64.b64encode(b"\xff\xd8jpeg").decode()
    c = gemini.to_contents([{"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": img}}, {"type": "text", "text": "find this"}]}])
    assert c[0].parts[0].inline_data.data == b"\xff\xd8jpeg" and c[0].parts[0].inline_data.mime_type == "image/jpeg" and c[0].parts[1].text == "find this"

def test_a_tool_round_trip_returns_gemini_parts_untouched_and_labels_results():
    first = call_part(id="abc123")
    res = gemini.from_response(reply(first))
    assert res.stop_reason == "tool_use" and res.content[0].name == "buy" and res.content[0].input == {"product_id": 5} and res.content[0].id == "abc123"
    result = {"type": "tool_result", "tool_use_id": "abc123", "content": json.dumps({"status": "completed"}), "is_error": False}
    c = gemini.to_contents([{"role": "user", "content": "buy it"}, {"role": "assistant", "content": res.content}, {"role": "user", "content": [result]}])
    assert c[1].role == "model" and c[1].parts[0] is first  # same part, so the thought signature goes back too
    fr = c[2].parts[0].function_response
    assert c[2].role == "user" and fr.name == "buy" and fr.id == "abc123" and fr.response == {"status": "completed"}

def test_ids_we_invented_are_not_sent_and_list_results_are_wrapped():
    res = gemini.from_response(reply(call_part("search_products", {"query": "art"}, id=None)))
    tid = res.content[0].id
    assert tid.startswith("gem-")
    result = {"type": "tool_result", "tool_use_id": tid, "content": json.dumps([{"id": 1}]), "is_error": False}
    c = gemini.to_contents([{"role": "assistant", "content": res.content}, {"role": "user", "content": [result]}])
    fr = c[1].parts[0].function_response
    assert fr.id is None and fr.name == "search_products" and fr.response == {"result": [{"id": 1}]}

def test_several_results_go_back_in_one_turn_in_order():
    res = gemini.from_response(reply(call_part("lookup_topup", {}, "a"), call_part("list_my_topups", {}, "b")))
    results = [{"type": "tool_result", "tool_use_id": i, "content": json.dumps({"error": "x"}), "is_error": True} for i in ("a", "b")]
    c = gemini.to_contents([{"role": "assistant", "content": res.content}, {"role": "user", "content": results}])
    assert [p.function_response.name for p in c[1].parts] == ["lookup_topup", "list_my_topups"]

def test_tool_declarations():
    tools = [{"name": "list_my_topups", "description": "d", "input_schema": {"type": "object", "properties": {}, "required": []}},
             {"name": "lookup_topup", "description": "d", "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": []}},
             {"name": "buy", "description": "d", "input_schema": {"type": "object", "properties": {"product_id": {"type": "integer"}}, "required": ["product_id"]}}]
    d = gemini.to_tools(tools)[0].function_declarations
    assert [x.name for x in d] == ["list_my_topups", "lookup_topup", "buy"]
    assert d[0].parameters_json_schema is None and d[1].parameters_json_schema is not None
    assert "required" not in d[1].parameters_json_schema and d[2].parameters_json_schema["required"] == ["product_id"]
    assert gemini.to_tools([]) is None

# ---------- what comes back ----------
def test_text_reply_ends_the_turn_and_thoughts_are_dropped():
    res = gemini.from_response(reply(types.Part(text="thinking...", thought=True), types.Part(text="Done!")))
    assert res.stop_reason == "end_turn" and [b.text for b in res.content] == ["Done!"]

@pytest.mark.parametrize("bad", [SimpleNamespace(candidates=[]), SimpleNamespace(candidates=None), SimpleNamespace(candidates=[SimpleNamespace(content=None)])])
def test_blocked_or_empty_replies_are_an_empty_end_turn(bad):
    res = gemini.from_response(bad)
    assert res.stop_reason == "end_turn" and res.content == []

# ---------- the client ----------
class FakeGenAI:
    def __init__(self, result=None, exc=None): self.seen, self.result, self.exc = {}, result, exc; self.models = SimpleNamespace(generate_content=self._gc)
    def _gc(self, **kw):
        self.seen = kw
        if self.exc: raise self.exc
        return self.result

def client_with(monkeypatch, fake):
    monkeypatch.setattr(gemini.Client, "_client", lambda self: fake)
    return gemini.Client()

def test_forced_tool_call_and_settings(monkeypatch):
    monkeypatch.delenv("GEMINI_MODEL", raising=False); monkeypatch.delenv("GEMINI_THINKING_LEVEL", raising=False); monkeypatch.delenv("GEMINI_THINKING_HEADROOM", raising=False)
    fake = FakeGenAI(reply(call_part("rank_basket", {"picks": []})))
    out = client_with(monkeypatch, fake).messages.create(model=None, max_tokens=600, temperature=0.2, system="sys", messages=[{"role": "user", "content": "x"}],
                                                          tools=[{"name": "rank_basket", "description": "d", "input_schema": {"type": "object", "properties": {"picks": {"type": "array"}}}}],
                                                          tool_choice={"type": "tool", "name": "rank_basket"})
    cfg = fake.seen["config"]
    assert out.stop_reason == "tool_use" and out.content[0].input == {"picks": []}
    assert fake.seen["model"] == gemini.DEFAULT_MODEL and cfg.system_instruction == "sys" and cfg.max_output_tokens == 600 + 2048
    assert cfg.temperature is None and cfg.thinking_config is None  # sampling settings are left to the model
    assert cfg.tool_config.function_calling_config.mode == "ANY" and cfg.tool_config.function_calling_config.allowed_function_names == ["rank_basket"]
    assert cfg.automatic_function_calling.disable is True

def test_model_and_thinking_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "my-model"); monkeypatch.setenv("GEMINI_THINKING_LEVEL", "low"); monkeypatch.setenv("GEMINI_THINKING_HEADROOM", "100")
    fake = FakeGenAI(reply(types.Part(text="ok")))
    client_with(monkeypatch, fake).messages.create(max_tokens=50, messages=[{"role": "user", "content": "x"}])
    assert fake.seen["model"] == "my-model" and fake.seen["config"].max_output_tokens == 150 and str(fake.seen["config"].thinking_config.thinking_level).upper().endswith("LOW")
    assert fake.seen["config"].tools is None and fake.seen["config"].tool_config is None

@pytest.mark.parametrize("exc", [RuntimeError("quota"), ConnectionError("down"), ValueError("bad")])
def test_every_failure_becomes_apierror(monkeypatch, exc):
    with pytest.raises(gemini.APIError):
        client_with(monkeypatch, FakeGenAI(exc=exc)).messages.create(messages=[{"role": "user", "content": "x"}])

def test_a_bad_photo_is_an_apierror_not_a_crash(monkeypatch):
    bad = {"role": "user", "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "***not base64***"}}]}
    with pytest.raises(gemini.APIError):
        client_with(monkeypatch, FakeGenAI(reply(types.Part(text="x")))).messages.create(messages=[bad])

def test_no_key_is_harmless_until_a_call_is_made(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False); monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    assert not gemini.has_key()
    gemini.Client()  # must not raise: the app imports this at startup, with or without a key
    monkeypatch.setenv("GOOGLE_API_KEY", "k"); assert gemini.has_key()
