"""Qualitative disclosure policy shared by prompt and response validation."""

QUALITATIVE_REASON_CODE_DEFINITIONS = {
    "NO_MATERIAL_RED_FLAG": (
        "verdict PASS. The supplied disclosures contain no explicit adverse "
        "qualitative event."
    ),
    "INSUFFICIENT_EVIDENCE": (
        "verdict REVIEW. A supplied disclosure identifies a potentially "
        "material event, but its text is too terse to determine whether it is "
        "adverse."
    ),
    "GOVERNANCE_OR_REGULATORY": (
        "verdict REJECT. Supplied text explicitly states regulatory action, "
        "non-compliance, a penalty, an auditor qualification or departure, or "
        "a material governance failure."
    ),
    "PROMOTER_OR_DILUTION": (
        "verdict REJECT. Supplied text explicitly states a promoter pledge or "
        "stake concern, or an issuance that dilutes existing holders."
    ),
    "ADVERSE_CORPORATE_EVENT": (
        "verdict REJECT. Supplied text explicitly states default, fraud, "
        "material litigation, a rating downgrade, a lost order, a plant "
        "shutdown, or a comparable adverse event."
    ),
}
QUALITATIVE_REASON_CODES = tuple(QUALITATIVE_REASON_CODE_DEFINITIONS)
QUALITATIVE_EXPECTED_VERDICTS = {
    "NO_MATERIAL_RED_FLAG": "PASS",
    "INSUFFICIENT_EVIDENCE": "REVIEW",
    "GOVERNANCE_OR_REGULATORY": "REJECT",
    "PROMOTER_OR_DILUTION": "REJECT",
    "ADVERSE_CORPORATE_EVENT": "REJECT",
}
QUALITATIVE_REJECT_REASON_CODES = frozenset(
    code
    for code, verdict in QUALITATIVE_EXPECTED_VERDICTS.items()
    if verdict == "REJECT"
)


def render_qualitative_policy() -> str:
    definitions = "\n".join(
        f"- {code}: {QUALITATIVE_REASON_CODE_DEFINITIONS[code]}"
        for code in QUALITATIVE_REASON_CODES
    )
    return (
        "You are a bounded qualitative-disclosure classifier. Use only "
        "EVIDENCE facts. Do not infer motives, causality, missing "
        "transactions, financial weakness, or promoter behavior.\n\n"
        "QUALITATIVE DECISION POLICY:\n"
        f"{definitions}\n\n"
        "ROUTINE DISCLOSURES:\n"
        "- Routine cash dividends, analyst/investor meeting schedules or "
        "transcripts, newspaper publications, and AGM notices are PASS unless "
        "their supplied text explicitly states an adverse event.\n"
        "- Repetition does not make a routine event adverse.\n"
        "- A cash dividend is a distribution, not dilution, and is never "
        "evidence for PROMOTER_OR_DILUTION.\n\n"
        "OUTPUT INVARIANTS:\n"
        "- Use exactly the verdict/reason-code pair defined above.\n"
        "- PASS and REJECT require 1-3 supplied evidence_ids and `missing: []`.\n"
        "- REVIEW requires at least one supplied evidence_id and 1-3 short "
        "factual `missing` labels; never put evidence IDs in `missing`.\n"
        "- Cite at most three supplied evidence IDs. Return JSON only."
    )
