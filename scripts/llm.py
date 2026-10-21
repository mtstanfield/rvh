"""The one OpenAI-compatible chat client for rvh's host-side stages
(analyze_repo, score_files). The worker talks to the model through goose."""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request


def normalize_endpoint(endpoint: str) -> str:
    """Endpoint base without a trailing slash or ``/v1`` (goose's OPENAI_HOST form)."""
    base = endpoint.strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base


def chat(
    prompt: str,
    *,
    endpoint: str,
    model: str,
    api_key: str | None,
    timeout: int,
    temperature: float | None = None,
    label: str = "llm",
) -> str:
    """Send one user message; return the reply text, or "" on any failure.

    Callers treat "" as "no usable answer" and fall back (retry, default
    score, …), so transport and parse errors are logged, not raised.
    """
    body: dict = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    if temperature is not None:
        body["temperature"] = temperature
    req = urllib.request.Request(
        normalize_endpoint(endpoint) + "/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key or 'dummy'}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        print(f"[{label}] HTTP {exc.code} from {endpoint}: {detail}", file=sys.stderr, flush=True)
        return ""
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        print(f"[{label}] request to {endpoint} failed: {exc}", file=sys.stderr, flush=True)
        return ""
    try:
        return (result["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        return ""
