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
        self.completions = _Completions()
        llm._local_client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=self.completions)
        )

    def tearDown(self):
        llm._local_client = self.original_client
        llm.LOCAL_LLM_REASONING_EFFORT = self.original_effort

    def test_disables_reasoning_when_configured_for_ollama(self):
        llm.LOCAL_LLM_REASONING_EFFORT = "none"

        result = llm._call_local_llm("Judge this")

        self.assertEqual(result, "GOOD: sound")
        self.assertEqual(self.completions.request["reasoning_effort"], "none")
        self.assertNotIn("extra_body", self.completions.request)

    def test_omits_reasoning_control_for_other_openai_compatible_servers(self):
        llm.LOCAL_LLM_REASONING_EFFORT = ""

        llm._call_local_llm("Judge this")

        self.assertNotIn("reasoning_effort", self.completions.request)


if __name__ == "__main__":
    unittest.main()
