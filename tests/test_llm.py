import types
import unittest
from unittest.mock import patch

import llm


class _Completions:
    def __init__(self, content="GOOD: sound", reasoning=""):
        self.content = content
        self.reasoning = reasoning
        self.request = None

    def create(self, **kwargs):
        self.request = kwargs
        message = types.SimpleNamespace(content=self.content, reasoning=self.reasoning)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


class _SequencedCompletions:
    def __init__(self, contents):
        self.contents = iter(contents)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        message = types.SimpleNamespace(
            content=next(self.contents),
            reasoning="",
        )
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)])


class LocalLlmTests(unittest.TestCase):
    def setUp(self):
        self.original_client = llm._local_client
        self.original_effort = llm.LOCAL_LLM_REASONING_EFFORT
        self.original_directive = llm.LOCAL_LLM_NO_THINK_DIRECTIVE
        self.completions = _Completions()
        llm._local_client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=self.completions)
        )

    def tearDown(self):
        llm._local_client = self.original_client
        llm.LOCAL_LLM_REASONING_EFFORT = self.original_effort
        llm.LOCAL_LLM_NO_THINK_DIRECTIVE = self.original_directive

    def test_disables_reasoning_when_configured_for_ollama(self):
        llm.LOCAL_LLM_REASONING_EFFORT = "none"
        llm.LOCAL_LLM_NO_THINK_DIRECTIVE = "/no_think"

        result = llm._call_local_llm("Judge this", mode="check")

        self.assertEqual(result, "GOOD: sound")
        self.assertEqual(self.completions.request["reasoning_effort"], "none")
        self.assertNotIn("extra_body", self.completions.request)
        self.assertEqual(
            self.completions.request["messages"][0]["content"],
            "/no_think\nJudge this",
        )

    def test_omits_reasoning_control_for_other_openai_compatible_servers(self):
        llm.LOCAL_LLM_REASONING_EFFORT = ""
        llm.LOCAL_LLM_NO_THINK_DIRECTIVE = ""

        llm._call_local_llm("Judge this", mode="check")

        self.assertNotIn("reasoning_effort", self.completions.request)
        self.assertEqual(self.completions.request["messages"][0]["content"], "Judge this")

    def test_extracts_verdict_from_qwen_reasoning_markup(self):
        llm.LOCAL_LLM_NO_THINK_DIRECTIVE = "/no_think"
        self.completions.content = (
            "</think>\n<answer>\nGOOD Stable fundamentals.\n\n"
            "\\boxed{GOOD Stable fundamentals.}\n</answer>"
        )

        result = llm._call_local_llm("Judge this", mode="check")

        self.assertEqual(result, "GOOD Stable fundamentals.")

    def test_normalizes_a_schema_constrained_verdict(self):
        result = llm._normalize_local_response(
            '{"verdict":"GOOD","reason":"stable fundamentals"}',
            mode="check",
        )

        self.assertEqual(result, "GOOD: stable fundamentals")

    def test_fundamental_assessment_returns_compact_structured_evidence(self):
        self.completions.content = (
            '{"verdict":"PASS","reason_code":"NO_MATERIAL_RED_FLAG",'
            '"summary":"Stable ownership and no adverse announcements.",'
            '"evidence_ids":["SHAREHOLDING_2026-06-30","ANNOUNCEMENT_1"],'
            '"missing":[]}'
        )

        with patch.multiple(llm, USE_LOCAL_LLM=True, USE_REAL_LLM=False):
            assessment = llm.assess_fundamentals(
                "Judge these fundamentals",
                ("SHAREHOLDING_2026-06-30", "ANNOUNCEMENT_1"),
            )

        self.assertEqual(
            assessment.to_dict(),
            {
                "verdict": "PASS",
                "reason_code": "NO_MATERIAL_RED_FLAG",
                "summary": "Stable ownership and no adverse announcements.",
                "evidence_ids": ["SHAREHOLDING_2026-06-30", "ANNOUNCEMENT_1"],
                "missing": [],
            },
        )
        schema = self.completions.request["response_format"]["json_schema"][
            "schema"
        ]
        self.assertEqual(schema["properties"]["summary"]["maxLength"], 220)
        self.assertEqual(schema["properties"]["evidence_ids"]["maxItems"], 3)
        self.assertEqual(self.completions.request["temperature"], 0)

    def test_fundamental_assessment_deduplicates_repeated_evidence_tags(self):
        self.completions.content = (
            '{"verdict":"REVIEW","reason_code":"INSUFFICIENT_EVIDENCE",'
            '"summary":"The supplied evidence is incomplete.",'
            '"evidence_ids":["CORPORATE_ACTION_1","CORPORATE_ACTION_1"],'
            '"missing":["shareholding_history"]}'
        )

        with patch.multiple(llm, USE_LOCAL_LLM=True, USE_REAL_LLM=False):
            assessment = llm.assess_fundamentals(
                "Judge these fundamentals", ("CORPORATE_ACTION_1",)
            )

        self.assertEqual(assessment.evidence_ids, ("CORPORATE_ACTION_1",))

    def test_fundamental_assessment_repairs_one_invalid_structured_response(self):
        completions = _SequencedCompletions(
            [
                "The company appears stable.",
                (
                    '{"verdict":"PASS","reason_code":"NO_MATERIAL_RED_FLAG",'
                    '"summary":"No material red flag in the supplied evidence.",'
                    '"evidence_ids":["ANNOUNCEMENT_1"],"missing":[]}'
                ),
            ]
        )
        llm._local_client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=completions)
        )

        with patch.multiple(llm, USE_LOCAL_LLM=True, USE_REAL_LLM=False):
            assessment = llm.assess_fundamentals(
                "Judge these fundamentals", ("ANNOUNCEMENT_1",)
            )

        self.assertEqual(assessment.verdict, "PASS")
        self.assertEqual(len(completions.requests), 2)
        self.assertEqual(completions.requests[1]["max_tokens"], 192)
        self.assertIn(
            "The company appears stable.",
            completions.requests[1]["messages"][0]["content"],
        )

    def test_fundamental_assessment_repairs_a_contradictory_verdict_code_pair(self):
        completions = _SequencedCompletions(
            [
                (
                    '{"verdict":"REJECT","reason_code":"NO_MATERIAL_RED_FLAG",'
                    '"summary":"No material red flag was identified.",'
                    '"evidence_ids":["ANNOUNCEMENT_1"],"missing":[]}'
                ),
                (
                    '{"verdict":"PASS","reason_code":"NO_MATERIAL_RED_FLAG",'
                    '"summary":"No material red flag was identified.",'
                    '"evidence_ids":["ANNOUNCEMENT_1"],"missing":[]}'
                ),
            ]
        )
        llm._local_client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=completions)
        )

        with patch.multiple(llm, USE_LOCAL_LLM=True, USE_REAL_LLM=False):
            assessment = llm.assess_fundamentals(
                "Judge these fundamentals", ("ANNOUNCEMENT_1",)
            )

        self.assertEqual(assessment.verdict, "PASS")
        self.assertEqual(len(completions.requests), 2)

    def test_fundamental_assessment_repairs_wrong_json_field_types(self):
        completions = _SequencedCompletions(
            [
                (
                    '{"verdict":"PASS","reason_code":"NO_MATERIAL_RED_FLAG",'
                    '"summary":42,"evidence_ids":"","missing":[]}'
                ),
                (
                    '{"verdict":"PASS","reason_code":"NO_MATERIAL_RED_FLAG",'
                    '"summary":"No material red flag was identified.",'
                    '"evidence_ids":["EARNINGS_2026-06"],"missing":[]}'
                ),
            ]
        )
        llm._local_client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=completions)
        )

        with patch.multiple(llm, USE_LOCAL_LLM=True, USE_REAL_LLM=False):
            assessment = llm.assess_fundamentals(
                "Judge these fundamentals", ("EARNINGS_2026-06",)
            )

        self.assertEqual(assessment.reason, "No material red flag was identified.")
        self.assertEqual(len(completions.requests), 2)

    def test_fundamental_assessment_rejects_extra_model_generated_fields(self):
        completions = _SequencedCompletions(
            [
                (
                    '{"verdict":"PASS","reason_code":"NO_MATERIAL_RED_FLAG",'
                    '"summary":"No material red flag was identified.",'
                    '"evidence_ids":["EARNINGS_2026-06"],"missing":[],"confidence":0.9}'
                ),
                (
                    '{"verdict":"PASS","reason_code":"NO_MATERIAL_RED_FLAG",'
                    '"summary":"No material red flag was identified.",'
                    '"evidence_ids":["EARNINGS_2026-06"],"missing":[]}'
                ),
            ]
        )
        llm._local_client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=completions)
        )

        with patch.multiple(llm, USE_LOCAL_LLM=True, USE_REAL_LLM=False):
            assessment = llm.assess_fundamentals(
                "Judge these fundamentals", ("EARNINGS_2026-06",)
            )

        self.assertEqual(assessment.evidence_ids, ("EARNINGS_2026-06",))
        self.assertEqual(len(completions.requests), 2)

    def test_active_model_config_describes_the_backend_that_will_run(self):
        with patch.multiple(llm, USE_LOCAL_LLM=True, USE_REAL_LLM=False):
            self.assertEqual(
                llm.active_model_config(),
                {
                    "backend": "openai_compatible_local",
                    "name": llm.LOCAL_LLM_MODEL,
                    "max_tokens": llm.LOCAL_LLM_MAX_TOKENS,
                    "fundamental_max_tokens": llm.FUNDAMENTAL_LLM_MAX_TOKENS,
                },
            )
        with patch.multiple(llm, USE_LOCAL_LLM=False, USE_REAL_LLM=True):
            self.assertEqual(
                llm.active_model_config(),
                {
                    "backend": "anthropic",
                    "name": llm.ANTHROPIC_MODEL,
                    "max_tokens": llm.ANTHROPIC_MAX_TOKENS,
                    "fundamental_max_tokens": min(
                        llm.ANTHROPIC_MAX_TOKENS,
                        llm.FUNDAMENTAL_LLM_MAX_TOKENS,
                    ),
                },
            )
        with patch.multiple(llm, USE_LOCAL_LLM=False, USE_REAL_LLM=False):
            self.assertEqual(
                llm.active_model_config(),
                {
                    "backend": "stub",
                    "name": "deterministic-stub",
                    "max_tokens": None,
                    "fundamental_max_tokens": None,
                },
            )

    def test_repairs_a_verbose_check_response_that_omits_the_verdict(self):
        completions = _SequencedCompletions(
            [
                "The company has stable ownership and no obvious red flags.",
                (
                    '{"verdict":"GOOD","reason":'
                    '"stable ownership and no obvious red flags"}'
                ),
            ]
        )
        llm._local_client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=completions)
        )

        result = llm._call_local_llm("Judge the fundamentals", mode="check")

        self.assertEqual(
            result,
            "GOOD: stable ownership and no obvious red flags",
        )
        self.assertEqual(len(completions.requests), 2)
        self.assertEqual(completions.requests[1]["max_tokens"], 64)
        self.assertEqual(
            completions.requests[0]["response_format"]["type"],
            "json_schema",
        )
        self.assertIn(
            "The company has stable ownership",
            completions.requests[1]["messages"][0]["content"],
        )


if __name__ == "__main__":
    unittest.main()
