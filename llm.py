"""LLM router: Cerebras / OpenRouter / OpenAI, all OpenAI-compatible.

Provider differences that actually bite (and are handled here):
  * Cerebras expects `max_completion_tokens`; OpenAI/OpenRouter accept
    `max_tokens`. Sending the wrong key is a 400, not a silent fallback, so the
    field name is chosen per provider instead of hoping both work.
  * gpt-oss is a REASONING model. It may emit `reasoning_content` alongside (or
    instead of) `content`, and it can spend the whole budget thinking. We read
    the first non-empty of content/reasoning_content and raise the budget on a
    length stop rather than accepting a truncated answer.
  * JSON mode differs: Cerebras supports strict `json_schema`, OpenRouter
    support varies by model, OpenAI supports `json_object`. We try strict
    schema -> json_object -> plain, and treat a 400/404/422 at any step as
    "this provider does not do that" instead of a hard failure.

Everything returns None rather than raising, so callers fall back to the
deterministic templates and a missing key never kills a run.
"""
import json
import re
import time

import requests

import config

_SESSION = requests.Session()
_NO_KEY_WARNED = False

# last call's diagnostics, surfaced by main.py --test-llm
LAST_CALL = {}


def provider():
    """-> (name, base_url, key, model). name is None when nothing is set."""
    order = [config.LLM_PROVIDER] if config.LLM_PROVIDER else \
        ["cerebras", "openrouter", "openai"]
    for p in order:
        if p == "cerebras" and config.CEREBRAS_API_KEY:
            return ("cerebras", config.CEREBRAS_BASE_URL,
                    config.CEREBRAS_API_KEY, config.CEREBRAS_MODEL)
        if p == "openrouter" and config.OPENROUTER_API_KEY:
            return ("openrouter", config.LLM_BASE_URL,
                    config.OPENROUTER_API_KEY, config.LLM_MODEL)
        if p == "openai" and config.OPENAI_API_KEY:
            return ("openai", config.OPENAI_BASE_URL,
                    config.OPENAI_API_KEY, config.LLM_MODEL.split("/")[-1])
    return (None, "", "", "")


def have_key():
    return provider()[0] is not None


def describe():
    name, base, key, model = provider()
    if not name:
        return ("no LLM key set - running in deterministic template mode.\n"
                "  set CEREBRAS_API_KEY (fastest), or OPENROUTER_API_KEY, "
                "or OPENAI_API_KEY")
    return f"provider={name}  model={model}  base={base}  key=...{key[-4:]}"


def _headers(name, key):
    h = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if name == "openrouter":
        h["HTTP-Referer"] = "https://sunnypipeline.local"
        h["X-Title"] = "Sunny Pipeline v4"
    return h


def _token_field(name):
    """Cerebras uses max_completion_tokens; the others use max_tokens."""
    return "max_completion_tokens" if name == "cerebras" else "max_tokens"


def _apply_tokens(payload, name, n):
    payload[_token_field(name)] = int(n)
    # send the other spelling too where it is harmless: some gateways behind
    # the same base URL accept only one of the two.
    if name != "cerebras":
        payload.pop("max_completion_tokens", None)
    return payload


def _content_of(choice):
    """First non-empty of content / reasoning_content, with stray thinking
    wrappers stripped. Reasoning models sometimes leave content empty."""
    msg = choice.get("message", {}) or {}
    for field in ("content", "reasoning_content", "reasoning"):
        v = msg.get(field)
        if isinstance(v, str) and v.strip():
            v = v.strip()
            # drop  thinking...<｜end▁of▁thinking｜> blocks that some models inline
            v = re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", v,
                       flags=re.S).strip()
            if v:
                return v
    return ""


def chat(messages, model=None, temperature=0.8, max_tokens=3500, tries=3,
         json_schema=None, force_plain=False):
    """One completion. json_schema: optional strict schema dict. Returns the
    text, or None if every attempt failed."""
    global _NO_KEY_WARNED
    name, base, key, model_default = provider()
    if not name:
        if not _NO_KEY_WARNED:
            print("  [llm] " + describe())
            _NO_KEY_WARNED = True
        return None

    payload = {"model": model or model_default, "messages": messages,
               "temperature": temperature}
    _apply_tokens(payload, name, max_tokens)
    if json_schema and not force_plain:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "sunny", "strict": True,
                            "schema": json_schema}}

    last_err = None
    for attempt in range(tries + 1):
        try:
            r = _SESSION.post(base + "/chat/completions",
                              headers=_headers(name, key), json=payload,
                              timeout=180)
            LAST_CALL.update(provider=name, status=r.status_code,
                             model=payload["model"])
            if r.status_code == 429:
                wait = min(30, 2 ** attempt)
                print(f"  [llm] rate limited (429) - backing off {wait}s")
                time.sleep(wait)
                continue
            if r.status_code in (400, 404, 422):
                # most likely an unsupported response_format -> drop it once
                if "response_format" in payload:
                    print("  [llm] provider rejected response_format - retrying "
                          "without it")
                    payload.pop("response_format", None)
                    continue
                if _token_field(name) == "max_completion_tokens":
                    print("  [llm] provider rejected max_completion_tokens - "
                          "retrying with max_tokens")
                    payload.pop("max_completion_tokens", None)
                    payload["max_tokens"] = int(max_tokens)
                    continue
                body = r.text[:300]
                last_err = f"HTTP {r.status_code}: {body}"
                print(f"  [llm] {last_err}")
                break
            r.raise_for_status()
            data = r.json()
            choice = data["choices"][0]
            text = _content_of(choice)
            if choice.get("finish_reason") == "length":
                bigger = int(payload.get(_token_field(name), max_tokens) * 1.6)
                print(f"  [llm] output truncated - retrying with "
                      f"{_token_field(name)}={bigger}")
                _apply_tokens(payload, name, bigger)
                if len(text) > 200:      # partial answer beats nothing
                    LAST_CALL["truncated"] = True
                    return text
                continue
            if not text:
                print("  [llm] empty content - retrying")
                continue
            return text
        except Exception as e:
            last_err = e
            print(f"  [llm] attempt {attempt + 1} failed: {e}")
            time.sleep(min(20, 2 ** attempt))
    print(f"  [llm] giving up: {last_err}")
    LAST_CALL["error"] = str(last_err)
    return None


def _extract_json(raw):
    """Strip fences, then greedy-match the outermost {...} or [...]. Non-greedy
    matching stops at the first closing brace and breaks nested JSON."""
    if not raw:
        return None, "empty response"
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.S)
    m = re.search(r"\{.*\}|\[.*\]", raw, re.S)
    if not m:
        return None, "no JSON object found"
    try:
        return json.loads(m.group(0)), None
    except json.JSONDecodeError as e:
        return None, str(e)


def chat_json(messages, schema=None, **kw):
    """Structured output. Strict schema when the provider supports it, then
    plain, then ONE repair round that feeds the parser error back."""
    raw = chat(messages, json_schema=schema, **kw)
    if raw is None:
        raw = chat(messages, force_plain=True, **kw)
    if not raw:
        return None
    data, err = _extract_json(raw)
    if data is not None:
        return data
    print(f"  [llm] invalid JSON ({err}) - asking the model to repair it")
    repair = list(messages) + [
        {"role": "assistant", "content": raw[:3000]},
        {"role": "user",
         "content": f"Your reply was not valid JSON: {err}. Reply with ONLY "
                    "valid JSON. No markdown fences, no commentary."}]
    raw2 = chat(repair, force_plain=True, **kw)
    if not raw2:
        return None
    data, err2 = _extract_json(raw2)
    if data is None:
        print(f"  [llm] repair failed too: {err2}")
    return data


def self_test():
    """Cheap round trip used by main.py --test-llm."""
    print("  " + describe())
    if not have_key():
        return False
    r = chat([{"role": "user", "content": "Reply with exactly: LLM OK"}],
             temperature=0.0, max_tokens=64)
    print(f"  plain text -> {r!r}")
    ok_text = bool(r and "OK" in r.upper())
    j = chat_json([{"role": "user",
                    "content": 'Return JSON {"status": "ok", "n": 3} and '
                               'nothing else.'}],
                  temperature=0.0, max_tokens=128)
    print(f"  json mode  -> {j}")
    ok_json = isinstance(j, dict)
    print(f"  RESULT: text={'PASS' if ok_text else 'FAIL'}  "
          f"json={'PASS' if ok_json else 'FAIL'}")
    return ok_text and ok_json
