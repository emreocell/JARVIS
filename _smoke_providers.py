"""Manual provider smoke tests for configured API keys.

Usage:
    python _smoke_providers.py --groq
    python _smoke_providers.py --openrouter
    python _smoke_providers.py --vision
    python _smoke_providers.py --all

The script never prints configured secrets.
"""

from __future__ import annotations

import argparse
import base64
import sys

from app_config import load_app_config, mask_secret
from runtime.clients.google_vision_client import GoogleVisionClient
from runtime.clients.groq_client import GroqClient
from runtime.clients.openrouter_client import OpenRouterClient


def _make_test_png() -> bytes:
    # 1x1 transparent PNG. Enough to verify auth/API/quota without extra deps.
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    )


def smoke_groq() -> bool:
    cfg = load_app_config()
    key = str(cfg.get("groq_api_key", "") or "")
    print(f"Groq key: {mask_secret(key)}")
    client = GroqClient(key)
    models = client.list_models(timeout=15)
    print(f"Groq models visible: {len(models)}")
    text = client.chat(
        "llama-3.1-8b-instant",
        [{"role": "user", "content": "Respond with exactly: groq-ok"}],
        max_tokens=16,
        temperature=0,
        timeout=20,
    )
    print(f"Groq chat: {text[:80]}")
    return "groq-ok" in text.lower()


def smoke_openrouter() -> bool:
    cfg = load_app_config()
    key = str(cfg.get("openrouter_api_key", "") or "")
    print(f"OpenRouter key: {mask_secret(key)}")
    client = OpenRouterClient(key)
    models = client.list_models(timeout=20)
    print(f"OpenRouter models visible: {len(models)}")
    text = client.chat(
        "openai/gpt-oss-20b:free",
        [{"role": "user", "content": "Respond with exactly: openrouter-ok"}],
        max_tokens=24,
        temperature=0,
        timeout=45,
    )
    print(f"OpenRouter chat: {text[:80]}")
    return "openrouter-ok" in text.lower()


def smoke_vision() -> bool:
    cfg = load_app_config()
    key = str(cfg.get("google_vision_api_key", "") or "")
    print(f"Google Vision key: {mask_secret(key)}")
    client = GoogleVisionClient(key)
    data = client.annotate_image(
        _make_test_png(),
        features=["TEXT_DETECTION", "LABEL_DETECTION"],
        timeout=25,
    )
    summary = client.summarize(data)
    print(f"Vision summary: {summary[:240]}")
    return bool(data.get("responses"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--groq", action="store_true")
    parser.add_argument("--openrouter", action="store_true")
    parser.add_argument("--vision", action="store_true")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()
    run_groq = args.groq or args.all or not (args.groq or args.openrouter or args.vision)
    run_openrouter = args.openrouter or args.all
    run_vision = args.vision or args.all

    ok = True
    if run_groq:
        try:
            ok = smoke_groq() and ok
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"Groq smoke failed: {type(exc).__name__}: {exc}")
    if run_openrouter:
        try:
            ok = smoke_openrouter() and ok
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"OpenRouter smoke failed: {type(exc).__name__}: {exc}")
    if run_vision:
        try:
            ok = smoke_vision() and ok
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"Vision smoke failed: {type(exc).__name__}: {exc}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
