import types
import unittest

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


if __name__ == "__main__":
    unittest.main()
