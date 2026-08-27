from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from models.evaluation import Evaluation, WEIGHTS
from services.scoring import _evaluation_semantic_error, _repair_model_payload, call_json


def evaluation_payload() -> dict:
    payload = {
        "hard_gate_result": "满足",
        "hard_gate_notes": [],
        "matched_keywords": [],
        "missing_keywords": [],
        "strengths": [],
        "risks": [],
        "resume_suggestions": [],
        "recommendation": "建议投递",
        "recommendation_reason": "匹配",
    }
    for field, maximum in WEIGHTS.items():
        payload[field] = {
            "score": maximum // 2,
            "max_score": maximum,
            "score_reason": "存在证据",
            "evidence": ["简历事实 → JD要求"],
            "gaps": [],
        }
    return payload


class FakeCompletions:
    def __init__(self, contents: list[str]):
        self.contents = contents
        self.calls = 0
        self.kwargs = []

    def create(self, **kwargs):
        self.kwargs.append(kwargs)
        content = self.contents[self.calls]
        self.calls += 1
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )


class ScoringJsonRepairTests(unittest.TestCase):
    def test_repairs_string_and_null_list_fields(self):
        payload = evaluation_payload()
        payload["matched_keywords"] = "行业研究"
        payload["missing_keywords"] = None
        payload["hard_requirements"]["evidence"] = "咨询经历 → 行业研究"

        repaired = _repair_model_payload(payload, Evaluation)
        result = Evaluation.model_validate(repaired)

        self.assertEqual(result.matched_keywords, ["行业研究"])
        self.assertEqual(result.missing_keywords, [])
        self.assertEqual(result.hard_requirements.evidence, ["咨询经历 → 行业研究"])

    def test_retries_once_when_shape_cannot_be_repaired(self):
        invalid = evaluation_payload()
        invalid["hard_requirements"]["score"] = "无法评分"
        valid = evaluation_payload()
        completions = FakeCompletions(
            [json.dumps(invalid, ensure_ascii=False), json.dumps(valid, ensure_ascii=False)]
        )
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

        result = call_json(client, "test-model", "system", "user", Evaluation)

        self.assertIsInstance(result, Evaluation)
        self.assertEqual(completions.calls, 2)

    def test_retries_placeholder_evaluation_and_uses_zero_temperature(self):
        placeholder = evaluation_payload()
        for field in WEIGHTS:
            placeholder[field] = {
                "score": 0,
                "max_score": WEIGHTS[field],
                "score_reason": "该维度尚未定义评估标准，无法确定得分。",
                "evidence": [],
                "gaps": ["缺少评估标准"],
            }
        valid = evaluation_payload()
        completions = FakeCompletions(
            [json.dumps(placeholder, ensure_ascii=False), json.dumps(valid, ensure_ascii=False)]
        )
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

        result = call_json(
            client,
            "test-model",
            "system",
            "user",
            Evaluation,
            semantic_validator=_evaluation_semantic_error,
        )

        self.assertIsInstance(result, Evaluation)
        self.assertEqual(completions.calls, 2)
        self.assertEqual(completions.kwargs[0]["temperature"], 0)


if __name__ == "__main__":
    unittest.main()
