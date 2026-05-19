from __future__ import annotations

import json

from runtime.types import RouteRequest
from skills.metacognition.tools import classify_intent_fast, self_evaluate_action


class _FakeRouter:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = []

    def route(self, tool_name: str, request: RouteRequest, *, prefer=None):  # noqa: ANN001
        self.calls.append((tool_name, request, prefer))

        class _Result:
            ok = True
            text = self.text

        return _Result()


def test_classify_intent_uses_model_router() -> None:
    router = _FakeRouter('{"category":"interrupt","confidence":0.9}')
    result = classify_intent_fast("dur", model_router=router)
    assert json.loads(result)["category"] == "interrupt"
    assert router.calls[0][0] == "classify_intent_fast"
    assert router.calls[0][1].kind == "chat"
    assert router.calls[0][2].primary.provider == "groq"


def test_classify_intent_local_interrupt_fallback() -> None:
    result = json.loads(classify_intent_fast("dur ve iptal et"))
    assert result["category"] == "interrupt"
    assert result["should_interrupt"] is True


def test_self_evaluate_uses_model_router() -> None:
    router = _FakeRouter('{"decision":"continue","risk":"low","confidence":0.8}')
    result = self_evaluate_action("form doldur", "butona tikla", model_router=router)
    assert json.loads(result)["decision"] == "continue"
    assert router.calls[0][0] == "self_evaluate_action"
    assert router.calls[0][2].primary.provider == "groq"


def test_self_evaluate_local_high_risk_fallback() -> None:
    result = json.loads(
        self_evaluate_action(
            "dosyayi temizle",
            "indirilenlerdeki dosyalari sil",
            "silme onayi bekliyor",
        )
    )
    assert result["risk"] == "high"
    assert result["decision"] == "ask_user"
