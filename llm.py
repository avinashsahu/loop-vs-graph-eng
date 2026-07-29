import os

from dotenv import load_dotenv

load_dotenv()

USE_REAL_LLM = os.environ.get("USE_REAL_LLM") == "1"
USE_LOCAL_LLM = os.environ.get("USE_LOCAL_LLM") == "1"
LOCAL_LLM_URL = os.environ.get("LOCAL_LLM_URL", "http://localhost:11434/v1")
LOCAL_LLM_MODEL = os.environ.get(
    "LOCAL_LLM_MODEL", "hf.co/alexsabaka/ODA-Fin-RL-8B-GGUF:Q4_K_M"
)
LOCAL_LLM_MAX_TOKENS = int(os.environ.get("LOCAL_LLM_MAX_TOKENS", "400"))
LOCAL_LLM_REASONING_EFFORT = os.environ.get("LOCAL_LLM_REASONING_EFFORT", "none")
LOCAL_LLM_NO_THINK_DIRECTIVE = os.environ.get(
    "LOCAL_LLM_NO_THINK_DIRECTIVE", "/no_think"
)

_call_count = 0
_local_client = None


def _normalize_local_response(text, mode):
    text = text or ""
    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    if "<answer>" in text:
        text = text.split("<answer>", 1)[1]
    if "</answer>" in text:
        text = text.split("</answer>", 1)[0]
    text = text.strip()

    if mode == "check":
        for line in text.splitlines():
            line = line.strip()
            if line.startswith(("GOOD", "BAD")):
                return line
    return text


def _call_local_llm(prompt, mode):
    global _local_client
    if _local_client is None:
        from openai import OpenAI
        _local_client = OpenAI(base_url=LOCAL_LLM_URL, api_key="not-needed")

    if LOCAL_LLM_NO_THINK_DIRECTIVE:
        prompt = f"{LOCAL_LLM_NO_THINK_DIRECTIVE}\n{prompt}"

    request = dict(
        model=LOCAL_LLM_MODEL,
        max_tokens=LOCAL_LLM_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    if LOCAL_LLM_REASONING_EFFORT:
        request["reasoning_effort"] = LOCAL_LLM_REASONING_EFFORT

    resp = _local_client.chat.completions.create(**request)
    message = resp.choices[0].message
    # Fall back in case a compatible server returns only a reasoning channel.
    text = message.content if message.content is not None else getattr(message, "reasoning", "")
    return _normalize_local_response(text, mode)


def call_llm(prompt, mode):
    """Use the stub, Anthropic, or configured OpenAI-compatible local backend."""
    global _call_count
    _call_count += 1

    if USE_LOCAL_LLM:
        return _call_local_llm(prompt, mode)

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
