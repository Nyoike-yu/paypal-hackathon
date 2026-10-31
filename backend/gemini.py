"""Gemini, behind the small interface the rest of the app already uses.

main.py and brain.py call `ai.messages.create(model, max_tokens, system, tools, messages, ...)` and read back
`stop_reason` and `content` blocks of type "text" / "tool_use". This module keeps exactly that shape and translates
to and from Google's google-genai SDK, so the agent loop, the tool schemas, the guards and the test fakes do not change.

Notes that come from how Gemini works:
  - Tool calls carry a "thought signature" that must be sent back on the next turn. Each block we return keeps the
    original Gemini part in `raw`, and to_contents() sends that part back untouched.
  - A tool result must match its call by name (and by id when Gemini gave one), so results are labelled from the calls.
  - Recent Gemini models ignore `temperature`, so it is accepted and dropped.
  - Thinking tokens count toward max_output_tokens, so we add headroom (GEMINI_THINKING_HEADROOM, default 2048).
  - The client is created on first use, so importing this with no API key is harmless (demo mode and tests).
"""
import os, json, uuid, base64, threading
from types import SimpleNamespace
from google import genai
from google.genai import types

DEFAULT_MODEL = "gemini-3.6-flash"

class APIError(Exception):
    """Any failure talking to Gemini (network, quota, bad key, blocked or malformed request)."""

def api_key():
    return os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""

def has_key():
    return bool(api_key())

def model_name():
    return os.getenv("GEMINI_MODEL", DEFAULT_MODEL)

# ---------- Messages-style input -> Gemini ----------
def _get(o, k, default=None):
    return o.get(k, default) if isinstance(o, dict) else getattr(o, k, default)

def _wire_id(call_id):
    """Ids we made up ("gem-...") were never Gemini's, so they are not sent back."""
    return None if not call_id or str(call_id).startswith("gem-") else str(call_id)

def _result_dict(content):
    try: v = json.loads(content) if isinstance(content, str) else content
    except ValueError: v = content
    return v if isinstance(v, dict) else {"result": v}

def to_contents(messages):
    names = {}  # tool call id -> function name, to label the results that come back
    out = []
    for m in messages:
        role = "model" if m["role"] == "assistant" else "user"
        c = m["content"]
        parts = []
        for b in ([{"type": "text", "text": c}] if isinstance(c, str) else list(c)):
            t = _get(b, "type")
            if t == "tool_use": names[_get(b, "id")] = _get(b, "name")
            raw = _get(b, "raw")
            if raw is not None: parts.append(raw); continue  # a part Gemini produced: send it back exactly as it was (keeps thought signatures)
            if t == "text":
                if _get(b, "text"): parts.append(types.Part(text=_get(b, "text")))
            elif t == "image":
                src = _get(b, "source")
                parts.append(types.Part.from_bytes(data=base64.b64decode(_get(src, "data")), mime_type=_get(src, "media_type", "image/jpeg")))
            elif t == "tool_use":
                parts.append(types.Part(function_call=types.FunctionCall(id=_wire_id(_get(b, "id")), name=_get(b, "name"), args=dict(_get(b, "input") or {}))))
            elif t == "tool_result":
                tid = _get(b, "tool_use_id")
                parts.append(types.Part(function_response=types.FunctionResponse(id=_wire_id(tid), name=names.get(tid, "tool"), response=_result_dict(_get(b, "content")))))
        if parts: out.append(types.Content(role=role, parts=parts))
    return out

def to_tools(tools):
    decls = []
    for t in tools or []:
        schema = dict(t.get("input_schema") or {})
        if not schema.get("required"): schema.pop("required", None)
        kw = {"name": t["name"], "description": t.get("description", "")}
        if schema.get("properties"): kw["parameters_json_schema"] = schema  # a tool that takes nothing is declared with no parameters
        decls.append(types.FunctionDeclaration(**kw))
    return [types.Tool(function_declarations=decls)] if decls else None

# ---------- Gemini output -> Messages-style ----------
def from_response(res):
    cands = getattr(res, "candidates", None) or []
    content = getattr(cands[0], "content", None) if cands else None
    blocks = []
    for p in (getattr(content, "parts", None) or []):
        fc = getattr(p, "function_call", None)
        if fc:
            blocks.append(SimpleNamespace(type="tool_use", id=fc.id or "gem-" + uuid.uuid4().hex[:12], name=fc.name, input=dict(fc.args or {}), raw=p))
        elif getattr(p, "text", None) and not getattr(p, "thought", False):
            blocks.append(SimpleNamespace(type="text", text=p.text, raw=p))
    return SimpleNamespace(stop_reason="tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn", content=blocks)

# ---------- the client ----------
class Client:
    def __init__(self, max_retries=2, timeout=30.0):
        self._max_retries, self._timeout = max_retries, timeout
        self._c, self._lock = None, threading.Lock()
        self.messages = SimpleNamespace(create=self._create)

    def _client(self):
        with self._lock:
            if self._c is None:
                self._c = genai.Client(api_key=api_key() or None, http_options=types.HttpOptions(
                    timeout=int(self._timeout * 1000), retry_options=types.HttpRetryOptions(attempts=self._max_retries + 1)))
            return self._c

    def _create(self, *, messages, model=None, max_tokens=1024, system=None, tools=None, tool_choice=None, **_ignored):  # _ignored: temperature etc.
        cfg = {"system_instruction": system, "max_output_tokens": int(max_tokens) + int(os.getenv("GEMINI_THINKING_HEADROOM", "2048")),
               "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True)}  # our code runs the tools, not the SDK
        level = os.getenv("GEMINI_THINKING_LEVEL", "").strip()
        if level: cfg["thinking_config"] = types.ThinkingConfig(thinking_level=level.upper())  # optional: low / medium / high
        decls = to_tools(tools)
        if decls:
            cfg["tools"] = decls
            if isinstance(tool_choice, dict) and tool_choice.get("type") == "tool":  # a forced tool call, used for structured answers
                cfg["tool_config"] = types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="ANY", allowed_function_names=[tool_choice["name"]]))
        try:
            res = self._client().models.generate_content(model=model or model_name(), contents=to_contents(messages), config=types.GenerateContentConfig(**cfg))
        except Exception as e:
            raise APIError(f"{type(e).__name__}: {e}") from e
        return from_response(res)
