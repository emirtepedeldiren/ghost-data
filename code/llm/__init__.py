"""Provider-agnostic LLM client (D10 revised).

One interface, `LLMClient.generate(task, prompt, images=None, json_schema=None)`. The provider and the model
for each task come from code/config.toml. Every call is cached on disk and logged with token usage
(for evaluation/usage_report.md). Keys are read from the environment / .env only and are never logged.

Status 2026-10-24: implemented, NOT yet wired into the pipeline and NOT yet called against the live API;
the model choice awaits user approval.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "code" / "config.toml"


class LLMError(RuntimeError):
    """Raised when a provider call fails after retries. Callers fall back (e.g. keyword intent)."""


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cached: bool
    raw_usage: dict = field(default_factory=dict)

    def json(self):
        return json.loads(self.text)


def load_dotenv(path: Path = REPO_ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class _Provider:
    name = ""
    supports_sampling: dict[str, bool]

    def call(self, model: str, system: str, prompt: str, images: list[Path], json_schema: dict | None,
             temperature: float | None) -> tuple[str, int, int, dict]:
        raise NotImplementedError


class GeminiProvider(_Provider):
    """Gemini REST API (stdlib only). Key sent in the x-goog-api-key header, never in the URL."""
    name = "gemini"
    URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def __init__(self):
        self.key = os.environ.get("GEMINI_API_KEY")
        if not self.key:
            raise LLMError("GEMINI_API_KEY is not set")

    def call(self, model, system, prompt, images, json_schema, temperature):
        parts = [{"inlineData": {"mimeType": "image/png", "data": base64.b64encode(p.read_bytes()).decode()}} for p in images]
        parts.append({"text": prompt})
        body = {"contents": [{"role": "user", "parts": parts}], "generationConfig": {}}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if temperature is not None:
            body["generationConfig"]["temperature"] = temperature
        if json_schema:
            body["generationConfig"]["responseMimeType"] = "application/json"
            body["generationConfig"]["responseSchema"] = json_schema
        req = urllib.request.Request(self.URL.format(model=model), data=json.dumps(body).encode(),
                                     headers={"x-goog-api-key": self.key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise LLMError(f"gemini HTTP {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise LLMError(f"gemini unreachable: {e.reason}") from None
        cands = data.get("candidates") or []
        text = "".join(p.get("text", "") for c in cands[:1] for p in c.get("content", {}).get("parts", []))
        usage = data.get("usageMetadata", {})
        out_tokens = usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)
        return text, usage.get("promptTokenCount", 0), out_tokens, usage


class AnthropicProvider(_Provider):
    name = "anthropic"

    def __init__(self):
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise LLMError("ANTHROPIC_API_KEY is not set")
        import anthropic

        self.anthropic = anthropic
        self.client = anthropic.Anthropic()

    def call(self, model, system, prompt, images, json_schema, temperature):
        content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                 "data": base64.b64encode(p.read_bytes()).decode()}} for p in images]
        text_prompt = prompt + (f"\n\nRespond with JSON matching this schema:\n{json.dumps(json_schema)}" if json_schema else "")
        content.append({"type": "text", "text": text_prompt})
        kwargs = {"model": model, "max_tokens": 4096, "messages": [{"role": "user", "content": content}]}
        if system:
            kwargs["system"] = system
        if temperature is not None:
            kwargs["temperature"] = temperature
        try:
            msg = self.client.messages.create(**kwargs)
        except self.anthropic.BadRequestError as e:
            raise LLMError(f"anthropic 400: {str(e)[:300]}") from None
        except self.anthropic.APIStatusError as e:
            raise LLMError(f"anthropic HTTP {e.status_code}") from None
        except self.anthropic.APIConnectionError:
            raise LLMError("anthropic unreachable") from None
        text = "".join(b.text for b in msg.content if b.type == "text")
        return text, msg.usage.input_tokens, msg.usage.output_tokens, {"input_tokens": msg.usage.input_tokens,
                                                                        "output_tokens": msg.usage.output_tokens}


PROVIDERS = {"gemini": GeminiProvider, "anthropic": AnthropicProvider}


class LLMClient:
    def __init__(self, config_path: Path = CONFIG_PATH):
        load_dotenv()
        cfg = tomllib.loads(config_path.read_text(encoding="utf-8"))["llm"]
        self.provider_name = cfg["provider"]
        self.models: dict[str, str] = cfg["models"]
        self.prices: dict = cfg.get("prices", {})
        self.cache_dir = REPO_ROOT / cfg.get("cache_dir", "code/cache/llm")
        self.usage_log = REPO_ROOT / cfg.get("usage_log", "code/cache/llm_calls.jsonl")
        self._provider: _Provider | None = None
        self._no_sampling: set[str] = set()   # models that rejected temperature (memoized per run)

    @property
    def provider(self) -> _Provider:
        if self._provider is None:
            self._provider = PROVIDERS[self.provider_name]()
        return self._provider

    def _cache_key(self, model, task, system, prompt, images, json_schema) -> str:
        h = hashlib.sha256()
        for part in (self.provider_name, model, task, system, prompt, json.dumps(json_schema, sort_keys=True)):
            h.update(part.encode() + b"\0")
        for p in images:
            h.update(hashlib.sha256(p.read_bytes()).digest())
        return h.hexdigest()

    def _log(self, entry: dict) -> None:
        self.usage_log.parent.mkdir(parents=True, exist_ok=True)
        with self.usage_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")

    def generate(self, task: str, prompt: str, *, system: str = "", images: list[Path] | None = None,
                 json_schema: dict | None = None, temperature: float | None = 0.0) -> LLMResult:
        images = images or []
        model = self.models[task]
        key = self._cache_key(model, task, system, prompt, images, json_schema)
        cache_file = self.cache_dir / f"{key}.json"
        if cache_file.exists():
            c = json.loads(cache_file.read_text(encoding="utf-8"))
            self._log({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "task": task, "provider": self.provider_name,
                       "model": model, "cache_hit": True, "input_tokens": 0, "output_tokens": 0})
            return LLMResult(c["text"], self.provider_name, model, c["input_tokens"], c["output_tokens"], True, c.get("usage", {}))

        temp = None if model in self._no_sampling else temperature
        t0 = time.time()
        try:
            text, tin, tout, usage = self.provider.call(model, system, prompt, images, json_schema, temp)
        except LLMError as e:
            # Send temperature only to models that accept it: retry once without, remember the model.
            if temp is not None and "temperature" in str(e).lower():
                self._no_sampling.add(model)
                text, tin, tout, usage = self.provider.call(model, system, prompt, images, json_schema, None)
            else:
                raise
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({"text": text, "input_tokens": tin, "output_tokens": tout, "usage": usage},
                                         ensure_ascii=False), encoding="utf-8")
        self._log({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "task": task, "provider": self.provider_name,
                   "model": model, "cache_hit": False, "input_tokens": tin, "output_tokens": tout,
                   "latency_ms": int(1000 * (time.time() - t0)), "temperature_sent": model not in self._no_sampling})
        return LLMResult(text, self.provider_name, model, tin, tout, False, usage)
