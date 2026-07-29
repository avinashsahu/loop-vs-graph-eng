import json
import os

from dotenv import load_dotenv

load_dotenv()

USE_REAL_LLM = os.environ.get("USE_REAL_LLM") == "1"
USE_LOCAL_LLM = os.environ.get("USE_LOCAL_LLM") == "1"
LOCAL_LLM_URL = os.environ.get("LOCAL_LLM_URL", "http://localhost:11434/v1")
LOCAL_LLM_MODEL = os.environ.get(
    "LOCAL_LLM_MODEL", "hf.co/alexsabaka/ODA-Fin-RL-8B-GGUF:Q4_K_M"
)
LOCAL_LLM_MAX_TOKENS = int(os.environ.get("LOCAL_LLM_MAX_TOKENS", "800"))
LOCAL_LLM_REASONING_EFFORT = os.environ.get("LOCAL_LLM_REASONING_EFFORT", "none")
LOCAL_LLM_NO_THINK_DIRECTIVE = os.environ.get(
    "LOCAL_LLM_NO_THINK_DIRECTIVE", "/no_think"
)
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
ANTHROPIC_MAX_TOKENS = 200

_call_count = 0
_local_client = None
LOCAL_CHECK_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "financial_verdict",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "verdict": {
                    "type": "string",
                    "enum": ["GOOD", "BAD"],
                },
                "reason": {
                    "type": "string",
                    "maxLength": 240,
                },
            },
            "required": ["verdict", "reason"],
            "additionalProperties": False,
        },
    },
}


def active_model_config():
    if USE_LOCAL_LLM:
        return {
            "backend": "openai_compatible_local",
            "name": LOCAL_LLM_MODEL,
            "max_tokens": LOCAL_LLM_MAX_TOKENS,
        }
    if USE_REAL_LLM:
        return {
            "backend": "anthropic",
            "name": ANTHROPIC_MODEL,
            "max_tokens": ANTHROPIC_MAX_TOKENS,
        }
    return {
        "backend": "stub",
        "name": "deterministic-stub",
        "max_tokens": None,
    }


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
        try:
            payload = json.loads(text)
            verdict = str(payload["verdict"]).upper()
            reason = str(payload["reason"]).strip()
            if verdict in {"GOOD", "BAD"}:
                return f"{verdict}: {reason}" if reason else verdict
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass
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
    if mode == "check":
        request["response_format"] = LOCAL_CHECK_RESPONSE_FORMAT
        request["temperature"] = 0
    if LOCAL_LLM_REASONING_EFFORT:
        request["reasoning_effort"] = LOCAL_LLM_REASONING_EFFORT

    resp = _local_client.chat.completions.create(**request)
    message = resp.choices[0].message
    # Fall back in case a compatible server returns only a reasoning channel.
    text = message.content if message.content is not None else getattr(message, "reasoning", "")
    normalized = _normalize_local_response(text, mode)
    if mode != "check" or normalized.startswith(("GOOD", "BAD")):
        return normalized

    repair_prompt = (
        "Return exactly one line beginning GOOD: or BAD:. "
        "Do not explain your process. Classify the assessment below.\n\n"
        f"ASSESSMENT:\n{text[:4000]}"
    )
    if LOCAL_LLM_NO_THINK_DIRECTIVE:
        repair_prompt = f"{LOCAL_LLM_NO_THINK_DIRECTIVE}\n{repair_prompt}"
    repair_request = dict(
        model=LOCAL_LLM_MODEL,
        max_tokens=64,
        messages=[{"role": "user", "content": repair_prompt}],
    )
    repair_request["response_format"] = LOCAL_CHECK_RESPONSE_FORMAT
    repair_request["temperature"] = 0
    if LOCAL_LLM_REASONING_EFFORT:
        repair_request["reasoning_effort"] = LOCAL_LLM_REASONING_EFFORT
    repair_response = _local_client.chat.completions.create(**repair_request)
    repair_message = repair_response.choices[0].message
    repaired_text = (
        repair_message.content
        if repair_message.content is not None
        else getattr(repair_message, "reasoning", "")
    )
    repaired = _normalize_local_response(repaired_text, mode)
    return repaired if repaired.startswith(("GOOD", "BAD")) else normalized


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
            model=ANTHROPIC_MODEL,
            max_tokens=ANTHROPIC_MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text

    if mode == "answer":
        return "stub answer: 2+2=5" if _call_count == 1 else "stub answer: 2+2=4"
    if mode == "check":
        return "BAD: wrong" if _call_count == 2 else "GOOD"
    return "stub"
