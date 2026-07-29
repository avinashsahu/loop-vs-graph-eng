import json
import os
from dataclasses import dataclass

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
FUNDAMENTAL_REASON_CODES = (
    "NO_MATERIAL_RED_FLAG",
    "GOVERNANCE_OR_REGULATORY",
    "PROMOTER_OR_DILUTION",
    "ADVERSE_CORPORATE_EVENT",
    "PEER_OR_EARNINGS_WEAKNESS",
    "INSUFFICIENT_EVIDENCE",
)
FUNDAMENTAL_EVIDENCE_CATEGORIES = (
    "CORPORATE_ACTIONS",
    "ANNOUNCEMENTS",
    "SHAREHOLDING",
    "YEARWISE_RETURNS",
    "PEER_COMPARISON",
    "DELIVERY_CONTEXT",
)
FUNDAMENTAL_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "fundamental_assessment",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "verdict": {
                    "type": "string",
                    "enum": ["GOOD", "BAD"],
                },
                "reason_code": {
                    "type": "string",
                    "enum": list(FUNDAMENTAL_REASON_CODES),
                },
                "reason": {
                    "type": "string",
                    "maxLength": 160,
                },
                "evidence": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": list(FUNDAMENTAL_EVIDENCE_CATEGORIES),
                    },
                    "maxItems": 2,
                    "uniqueItems": True,
                },
            },
            "required": ["verdict", "reason_code", "reason", "evidence"],
            "additionalProperties": False,
        },
    },
}


@dataclass(frozen=True)
class FundamentalAssessment:
    verdict: str
    reason_code: str
    reason: str
    evidence: tuple[str, ...]

    @property
    def summary(self):
        return f"{self.verdict}: {self.reason}"

    def to_dict(self):
        return {
            "verdict": self.verdict,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "evidence": list(self.evidence),
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


def _call_local_structured(prompt, response_format, *, max_tokens=None):
    global _local_client
    if _local_client is None:
        from openai import OpenAI

        _local_client = OpenAI(base_url=LOCAL_LLM_URL, api_key="not-needed")

    if LOCAL_LLM_NO_THINK_DIRECTIVE:
        prompt = f"{LOCAL_LLM_NO_THINK_DIRECTIVE}\n{prompt}"
    request = {
        "model": LOCAL_LLM_MODEL,
        "max_tokens": max_tokens or LOCAL_LLM_MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
        "response_format": response_format,
        "temperature": 0,
    }
    if LOCAL_LLM_REASONING_EFFORT:
        request["reasoning_effort"] = LOCAL_LLM_REASONING_EFFORT
    response = _local_client.chat.completions.create(**request)
    message = response.choices[0].message
    return (
        message.content
        if message.content is not None
        else getattr(message, "reasoning", "")
    )


def _parse_fundamental_assessment(text):
    try:
        payload = json.loads(text)
        verdict_value = payload["verdict"]
        reason_code_value = payload["reason_code"]
        reason_value = payload["reason"]
        evidence_value = payload["evidence"]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid fundamental assessment JSON") from exc
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {"verdict", "reason_code", "reason", "evidence"}
        or not isinstance(verdict_value, str)
        or not isinstance(reason_code_value, str)
        or not isinstance(reason_value, str)
        or not isinstance(evidence_value, list)
        or any(not isinstance(item, str) for item in evidence_value)
    ):
        raise ValueError("invalid fundamental assessment field types")
    verdict = verdict_value.upper()
    reason_code = reason_code_value.upper()
    reason = reason_value.strip()
    evidence = tuple(dict.fromkeys(item.upper() for item in evidence_value))
    if verdict not in {"GOOD", "BAD"}:
        raise ValueError("invalid fundamental verdict")
    if reason_code not in FUNDAMENTAL_REASON_CODES:
        raise ValueError("invalid fundamental reason code")
    if (verdict == "GOOD") != (reason_code == "NO_MATERIAL_RED_FLAG"):
        raise ValueError("fundamental verdict and reason code disagree")
    if not reason or len(reason) > 160:
        raise ValueError("fundamental reason must contain 1-160 characters")
    if (
        len(evidence) > 2
        or any(item not in FUNDAMENTAL_EVIDENCE_CATEGORIES for item in evidence)
    ):
        raise ValueError("invalid fundamental evidence categories")
    return FundamentalAssessment(
        verdict=verdict,
        reason_code=reason_code,
        reason=reason,
        evidence=evidence,
    )


def assess_fundamentals(prompt):
    """Return a compact typed assessment through the configured model adapter."""
    global _call_count
    _call_count += 1

    classification_prompt = (
        "Classify only the supplied evidence. GOOD means no material red flag was "
        "identified and must use reason_code NO_MATERIAL_RED_FLAG. BAD means a "
        "material concern or insufficient evidence and must use another reason code. "
        "Do not infer missing facts.\n\n"
        f"{prompt}"
    )
    if USE_LOCAL_LLM:
        text = _call_local_structured(
            classification_prompt,
            FUNDAMENTAL_RESPONSE_FORMAT,
        )
    elif USE_REAL_LLM:
        import anthropic

        client = anthropic.Anthropic()
        schema = FUNDAMENTAL_RESPONSE_FORMAT["json_schema"]["schema"]
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=ANTHROPIC_MAX_TOKENS,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"{classification_prompt}\nReturn only JSON matching this schema: "
                        f"{json.dumps(schema, separators=(',', ':'))}"
                    ),
                }
            ],
        )
        text = response.content[0].text
    else:
        return FundamentalAssessment(
            verdict="GOOD",
            reason_code="NO_MATERIAL_RED_FLAG",
            reason="Stub found no material red flag.",
            evidence=(),
        )
    try:
        return _parse_fundamental_assessment(text)
    except ValueError:
        if USE_LOCAL_LLM:
            repair_prompt = (
                "Convert the invalid assessment below to the requested JSON schema. "
                "Preserve its conclusion, use at most two evidence categories, and "
                "return JSON only.\n\n"
                f"INVALID ASSESSMENT:\n{text[:4000]}"
            )
            repaired = _call_local_structured(
                repair_prompt,
                FUNDAMENTAL_RESPONSE_FORMAT,
                max_tokens=192,
            )
            try:
                return _parse_fundamental_assessment(repaired)
            except ValueError:
                pass
        return FundamentalAssessment(
            verdict="BAD",
            reason_code="INSUFFICIENT_EVIDENCE",
            reason="Model did not return a valid structured assessment.",
            evidence=(),
        )


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
