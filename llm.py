import os

from dotenv import load_dotenv

load_dotenv()

USE_REAL_LLM = os.environ.get("USE_REAL_LLM") == "1"
USE_LOCAL_LLM = os.environ.get("USE_LOCAL_LLM") == "1"
LOCAL_LLM_URL = os.environ.get("LOCAL_LLM_URL", "http://localhost:8080/v1")
LOCAL_LLM_MODEL = os.environ.get("LOCAL_LLM_MODEL", "mlx-community/gemma-4-12B-it-4bit")
LOCAL_LLM_MAX_TOKENS = int(os.environ.get("LOCAL_LLM_MAX_TOKENS", "300"))

_call_count = 0
_local_client = None


def _call_local_llm(prompt):
    global _local_client
    if _local_client is None:
        from openai import OpenAI
        _local_client = OpenAI(base_url=LOCAL_LLM_URL, api_key="not-needed")

    resp = _local_client.chat.completions.create(
        model=LOCAL_LLM_MODEL,
        max_tokens=LOCAL_LLM_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    message = resp.choices[0].message
    # fallback in case thinking still leaks through and eats the whole budget
    return message.content if message.content is not None else getattr(message, "reasoning", "")


def call_llm(prompt, mode):
    """mode: 'answer' | 'check'. Stub by default — set USE_REAL_LLM=1 (Anthropic) or USE_LOCAL_LLM=1 (gemma4) for a real call."""
    global _call_count
    _call_count += 1

    if USE_LOCAL_LLM:
        return _call_local_llm(prompt)

    if USE_REAL_LLM:
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text

    if mode == "answer":
        return "stub answer: 2+2=5" if _call_count == 1 else "stub answer: 2+2=4"
    if mode == "check":
        return "BAD: wrong" if _call_count == 2 else "GOOD"
    return "stub"
