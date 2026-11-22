"""AI providers: request building, stream parsing and model listing.

Three wire protocols cover every provider:
  * "anthropic" - Claude Messages API
  * "gemini"    - Google Generative Language API
  * "openai"    - OpenAI Chat Completions, also spoken by Mistral, Groq,
                  DeepSeek, xAI, OpenRouter, Ollama, LM Studio, vLLM, ...

Messages are stored provider-neutral (see dock.py) and converted here for
whichever provider the user picks, so one conversation can switch models.
"""

import json
from dataclasses import dataclass, field
from typing import List

from .i18n import tr

ANTHROPIC_VERSION = "2026-10-21"
ANTHROPIC_FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Claude models that accept server-side refusal fallbacks ("fallbacks": "default").
ANTHROPIC_FALLBACK_MODELS = {
    "claude-fable-5-2",
    "claude-fable-5-1",
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-sonnet-5-5",
}


class ProviderError(Exception):
    pass


@dataclass
class Provider:
    id: str
    name: str
    kind: str  # anthropic | openai | gemini
    base_url: str
    key_env: List[str] = field(default_factory=list)
    needs_key: bool = True
    models: List[str] = field(default_factory=list)  # seeds; "Refresh" fetches the live list
    key_url: str = ""


PROVIDERS = [
    Provider("anthropic", "Anthropic (Claude)", "anthropic", "https://api.anthropic.com/v1",
             ["ANTHROPIC_API_KEY"],
             models=["claude-opus-5-5", "claude-fable-5-2", "claude-sonnet-5-5", "claude-haiku-5-5"],
             key_url="https://console.anthropic.com/settings/keys"),
    Provider("openai", "OpenAI", "openai", "https://api.openai.com/v1", ["OPENAI_API_KEY"],
             models=["gpt-5", "gpt-5-mini", "gpt-4.1"],
             key_url="https://platform.openai.com/api-keys"),
    Provider("gemini", "Google Gemini", "gemini", "https://generativelanguage.googleapis.com/v1beta",
             ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
             models=["gemini-2.5-pro", "gemini-2.5-flash"],
             key_url="https://aistudio.google.com/apikey"),
    Provider("mistral", "Mistral", "openai", "https://api.mistral.ai/v1", ["MISTRAL_API_KEY"],
             models=["mistral-large-latest", "codestral-latest"],
             key_url="https://console.mistral.ai/api-keys"),
    Provider("groq", "Groq", "openai", "https://api.groq.com/openai/v1", ["GROQ_API_KEY"],
             models=["llama-3.3-70b-versatile"], key_url="https://console.groq.com/keys"),
    Provider("deepseek", "DeepSeek", "openai", "https://api.deepseek.com/v1", ["DEEPSEEK_API_KEY"],
             models=["deepseek-chat", "deepseek-reasoner"],
             key_url="https://platform.deepseek.com/api_keys"),
    Provider("xai", "xAI (Grok)", "openai", "https://api.x.ai/v1", ["XAI_API_KEY"],
             models=["grok-4"], key_url="https://console.x.ai"),
    Provider("openrouter", "OpenRouter (hundreds of models)", "openai", "https://openrouter.ai/api/v1",
             ["OPENROUTER_API_KEY"], models=[], key_url="https://openrouter.ai/keys"),
    # 127.0.0.1 rather than localhost: on Windows "localhost" may resolve to IPv6 (::1)
    # first, while Ollama and LM Studio listen on IPv4 only by default.
    Provider("ollama", "Ollama (local)", "openai", "http://127.0.0.1:11434/v1", needs_key=False,
             models=["llama3.2", "qwen2.5-coder"]),
    Provider("lmstudio", "LM Studio (local)", "openai", "http://127.0.0.1:1234/v1", needs_key=False),
    Provider("custom", "Custom (OpenAI-compatible)", "openai", "http://localhost:8000/v1",
             ["CUSTOM_AI_API_KEY"], needs_key=False),
]

PROVIDERS_BY_ID = {p.id: p for p in PROVIDERS}


def display_name(provider):
    """Provider name for the interface, with translated descriptions."""
    if provider.id in ("ollama", "lmstudio"):
        return tr("local_suffix").format(provider.name.split(" (")[0])
    if provider.id == "custom":
        return tr("custom_provider")
    if provider.id == "openrouter":
        return tr("many_models_suffix").format("OpenRouter")
    return provider.name

# Non-chat model families hidden from OpenAI's /models listing.
_OPENAI_NON_CHAT = ("embedding", "tts", "whisper", "dall-e", "moderation", "transcribe",
                    "audio", "realtime", "image", "davinci", "babbage", "sora")


# --------------------------------------------------------------------------- helpers

def user_text(msg):
    ctx = msg.get("context")
    if ctx:
        return "<qgis_context>\n{}\n</qgis_context>\n\n{}".format(ctx, msg["text"])
    return msg["text"]


def _history(messages):
    """Only real conversation turns: drop notes and excluded (refused/failed) replies."""
    return [m for m in messages
            if m["role"] in ("user", "assistant") and not m.get("exclude")]


def _anthropic_replay_blocks(blocks):
    """Assistant content to echo back, unchanged, on the next request.

    After a mid-output server-side fallback the API wants thinking/tool blocks
    that came before the last `fallback` block left out (and the fallback
    block kept in place).
    """
    last_fb = max((i for i, b in enumerate(blocks) if b.get("type") == "fallback"), default=-1)
    out = []
    for i, b in enumerate(blocks):
        if i < last_fb and b.get("type") not in ("text", "fallback"):
            continue
        out.append(b)
    return out


# --------------------------------------------------------------------------- requests

def build_chat_request(provider, base_url, key, model, system, messages, max_tokens):
    """Return (url, headers, body_bytes, parser) for a streaming chat request."""
    base = (base_url or provider.base_url).rstrip("/")
    history = _history(messages)
    if provider.kind == "anthropic":
        return _anthropic_request(base, key, model, system, history, max_tokens)
    if provider.kind == "gemini":
        return _gemini_request(base, key, model, system, history)
    return _openai_request(provider, base, key, model, system, history)


def _anthropic_request(base, key, model, system, history, max_tokens):
    msgs = []
    for m in history:
        if m["role"] == "user":
            content = []
            if m.get("image_b64"):
                content.append({"type": "image", "source": {
                    "type": "base64", "media_type": m.get("image_mime", "image/jpeg"),
                    "data": m["image_b64"]}})
            content.append({"type": "text", "text": user_text(m)})
            msgs.append({"role": "user", "content": content})
        else:
            blocks = m.get("provider_blocks") if m.get("provider_kind") == "anthropic" else None
            if blocks:
                content = _anthropic_replay_blocks(blocks)
            elif m.get("text"):
                content = [{"type": "text", "text": m["text"]}]
            else:
                continue
            msgs.append({"role": "assistant", "content": content})

    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": msgs,
        "stream": True,
        "cache_control": {"type": "ephemeral"},  # automatic prompt caching of the history
    }
    headers = {
        "content-type": "application/json",
        "x-api-key": key,
        "anthropic-version": ANTHROPIC_VERSION,
    }
    if model in ANTHROPIC_FALLBACK_MODELS:
        # If a safety classifier declines, the API retries on a suitable model.
        body["fallbacks"] = "default"
        headers["anthropic-beta"] = ANTHROPIC_FALLBACK_BETA
    return base + "/messages", headers, _dumps(body), AnthropicParser()


def _openai_request(provider, base, key, model, system, history):
    msgs = [{"role": "system", "content": system}]
    for m in history:
        if m["role"] == "user":
            if m.get("image_b64"):
                content = [
                    {"type": "text", "text": user_text(m)},
                    {"type": "image_url", "image_url": {
                        "url": "data:{};base64,{}".format(m.get("image_mime", "image/jpeg"),
                                                          m["image_b64"])}},
                ]
            else:
                content = user_text(m)
            msgs.append({"role": "user", "content": content})
        elif m.get("text"):
            msgs.append({"role": "assistant", "content": m["text"]})
    body = {"model": model, "messages": msgs, "stream": True}
    headers = {"content-type": "application/json"}
    if key:
        headers["authorization"] = "Bearer " + key
    if provider.id == "openrouter":
        headers["x-title"] = "QGIS AI Assistant"
    return base + "/chat/completions", headers, _dumps(body), OpenAIParser()


def _gemini_request(base, key, model, system, history):
    contents = []
    for m in history:
        if m["role"] == "user":
            parts = [{"text": user_text(m)}]
            if m.get("image_b64"):
                parts.append({"inline_data": {"mime_type": m.get("image_mime", "image/jpeg"),
                                              "data": m["image_b64"]}})
            role = "user"
        elif m.get("text"):
            parts, role = [{"text": m["text"]}], "model"
        else:
            continue
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": role, "parts": parts})
    body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents}
    model_id = model[len("models/"):] if model.startswith("models/") else model
    url = "{}/models/{}:streamGenerateContent?alt=sse".format(base, model_id)
    headers = {"content-type": "application/json", "x-goog-api-key": key}
    return url, headers, _dumps(body), GeminiParser()


def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


# --------------------------------------------------------------------------- stream parsers
# Each parser gets one decoded SSE `data:` JSON object at a time and returns
# the visible text it adds (or ""). result() is called once at the end.

class AnthropicParser:
    def __init__(self):
        self.blocks = {}
        self.stop_reason = None
        self.stop_details = None
        self.served_model = None

    def feed(self, obj):
        t = obj.get("type")
        if t == "message_start":
            self.served_model = (obj.get("message") or {}).get("model")
        elif t == "content_block_start":
            block = dict(obj.get("content_block") or {})
            self.blocks[obj.get("index", len(self.blocks))] = block
            if block.get("type") == "fallback":
                to = (block.get("to") or {}).get("model", "another model")
                return "\n\n*(continued by {})*\n\n".format(to)
        elif t == "content_block_delta":
            block = self.blocks.setdefault(obj.get("index", 0), {"type": "text", "text": ""})
            d = obj.get("delta") or {}
            dt = d.get("type")
            if dt == "text_delta":
                block["text"] = block.get("text", "") + d.get("text", "")
                return d.get("text", "")
            if dt == "thinking_delta":
                block["thinking"] = block.get("thinking", "") + d.get("thinking", "")
            elif dt == "signature_delta":
                block["signature"] = block.get("signature", "") + d.get("signature", "")
            elif dt == "citations_delta":
                block.setdefault("citations", []).append(d.get("citation"))
            elif dt == "input_json_delta":
                block["_partial_json"] = block.get("_partial_json", "") + d.get("partial_json", "")
        elif t == "message_delta":
            d = obj.get("delta") or {}
            if d.get("stop_reason"):
                self.stop_reason = d["stop_reason"]
            if d.get("stop_details"):
                self.stop_details = d["stop_details"]
        elif t == "error":
            err = obj.get("error") or {}
            raise ProviderError(err.get("message") or json.dumps(err))
        return ""

    def result(self, cancelled=False):
        blocks = [self.blocks[i] for i in sorted(self.blocks)]
        for b in blocks:
            if b.get("type") == "tool_use" and "_partial_json" in b:
                try:
                    b["input"] = json.loads(b.pop("_partial_json") or "{}")
                except ValueError:
                    b["input"] = {}
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        notice = None
        if self.stop_reason == "refusal":
            cat = (self.stop_details or {}).get("category")
            notice = tr("refused") + (" ({})".format(cat) if cat else "")
        elif self.stop_reason == "max_tokens":
            notice = tr("truncated")
        return {
            "text": text,
            # Replaying an interrupted stream's half-built blocks would be rejected.
            "blocks": None if cancelled else blocks,
            "stop_reason": self.stop_reason,
            "refused": self.stop_reason == "refusal",
            "served_model": self.served_model,
            "notice": notice,
        }


class OpenAIParser:
    def __init__(self):
        self.text = ""
        self.stop_reason = None
        self.served_model = None

    def feed(self, obj):
        if obj.get("error"):
            err = obj["error"]
            raise ProviderError(err.get("message") if isinstance(err, dict) else str(err))
        self.served_model = obj.get("model") or self.served_model
        choices = obj.get("choices") or []
        if not choices:
            return ""
        ch = choices[0]
        if ch.get("finish_reason"):
            self.stop_reason = ch["finish_reason"]
        content = (ch.get("delta") or {}).get("content")
        if isinstance(content, str) and content:
            self.text += content
            return content
        return ""

    def result(self, cancelled=False):
        notice = None
        if self.stop_reason == "content_filter":
            notice = tr("content_filter")
        elif self.stop_reason == "length":
            notice = tr("truncated")
        return {"text": self.text, "blocks": None, "stop_reason": self.stop_reason,
                "refused": self.stop_reason == "content_filter",
                "served_model": self.served_model, "notice": notice}


class GeminiParser:
    def __init__(self):
        self.text = ""
        self.stop_reason = None
        self.served_model = None

    def feed(self, obj):
        if obj.get("error"):
            err = obj["error"]
            raise ProviderError(err.get("message") if isinstance(err, dict) else str(err))
        block = (obj.get("promptFeedback") or {}).get("blockReason")
        if block:
            raise ProviderError(tr("provider_blocked").format(block))
        self.served_model = obj.get("modelVersion") or self.served_model
        out = ""
        for cand in (obj.get("candidates") or [])[:1]:
            for part in (cand.get("content") or {}).get("parts") or []:
                if part.get("thought"):
                    continue
                out += part.get("text") or ""
            if cand.get("finishReason"):
                self.stop_reason = cand["finishReason"]
        self.text += out
        return out

    def result(self, cancelled=False):
        notice = None
        refused = self.stop_reason in ("SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION")
        if refused:
            notice = tr("provider_stopped").format(self.stop_reason)
        elif self.stop_reason == "MAX_TOKENS":
            notice = tr("truncated")
        return {"text": self.text, "blocks": None, "stop_reason": self.stop_reason,
                "refused": refused, "served_model": self.served_model, "notice": notice}


# --------------------------------------------------------------------------- model listing

def build_models_request(provider, base_url, key):
    """Return (url, headers) for the provider's model-listing endpoint."""
    base = (base_url or provider.base_url).rstrip("/")
    if provider.kind == "anthropic":
        return base + "/models?limit=1000", {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}
    if provider.kind == "gemini":
        return base + "/models?pageSize=1000", {"x-goog-api-key": key}
    headers = {"authorization": "Bearer " + key} if key else {}
    return base + "/models", headers


def parse_models(provider, data):
    """Return a sorted list of {"id": str, "max_tokens": int|None}."""
    out = []
    if provider.kind == "gemini":
        for m in data.get("models") or []:
            if "generateContent" not in (m.get("supportedGenerationMethods") or []):
                continue
            name = m.get("name", "")
            out.append({"id": name[len("models/"):] if name.startswith("models/") else name,
                        "max_tokens": m.get("outputTokenLimit")})
    else:
        for m in data.get("data") or []:
            mid = m.get("id")
            if not mid:
                continue
            if provider.id == "openai" and any(s in mid for s in _OPENAI_NON_CHAT):
                continue
            out.append({"id": mid, "max_tokens": m.get("max_tokens")})
    if provider.kind == "anthropic":
        return out  # the API already lists newest first
    return sorted(out, key=lambda m: m["id"])
