from __future__ import annotations

import re
from dataclasses import dataclass

from medicare_agent.schemas import AgentProposal, CustomerSnapshot, RuleAssessment

_TREATMENT_VERBS = (
    r"increase|decrease|reduce|lower|raise|double|halve|change|adjust|start|stop|skip|"
    r"resume|switch|take|quit"
)
_TREATMENT_OBJECTS = (
    r"dose|doses|dosage|medication|medications|medicine|medicines|meds|drug|drugs|pill|pills|"
    r"tablet|tablets|insulin|prescription|prescriptions|taking"
)
TREATMENT_DIRECTIVE = re.compile(
    rf"\b(?:{_TREATMENT_VERBS})\b.{{0,35}}\b(?:{_TREATMENT_OBJECTS})\b"
    r"|\b(?:discontinue|stop taking|quit taking)\b",
    re.IGNORECASE,
)
DIAGNOSTIC_CERTAINTY = re.compile(
    r"\b(?:you have|this confirms|definitely|certainly)\b.{0,50}"
    r"\b(?:disease|condition|diagnosis|infection|failure)\b",
    re.IGNORECASE,
)
# A phone number has a 3-3-4 shape with an optional country code. Dates, blood pressure pairs
# and measurement lists do not, so they no longer trigger false contact-target holds.
PHONE_NUMBER = re.compile(
    r"(?<![\w-])(?:\+\d{1,3}[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?![\w-])"
)
WEB_ADDRESS = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
EMAIL_ADDRESS = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
URGENT_LANGUAGE = re.compile(r"\b(?:urgent|emergency|immediate|immediately)\b", re.IGNORECASE)

TEXT_HOLD_BY_CHECK = {
    "no_treatment_directive": "guardrail_treatment_instruction",
    "no_diagnostic_certainty": "guardrail_diagnostic_claim",
    "no_embedded_contact_target": "guardrail_embedded_contact_target",
}
HOLD_BY_CHECK = {
    "human_review_required": "guardrail_human_review_missing",
    **TEXT_HOLD_BY_CHECK,
    "contact_channel_present": "guardrail_contact_channel_missing",
    "consent_channel_compatible": "guardrail_consent_channel_conflict",
    "urgent_escalation_language": "guardrail_urgent_escalation_missing",
    "reasoning_is_not_duplicated": "guardrail_duplicate_reasoning",
}


@dataclass(frozen=True)
class GuardrailResult:
    holds: list[str]
    checks: dict[str, bool]


def _contains_contact_target(text: str) -> bool:
    return any(
        pattern.search(text) is not None for pattern in (PHONE_NUMBER, WEB_ADDRESS, EMAIL_ADDRESS)
    )


def text_checks(text: str) -> dict[str, bool]:
    return {
        "no_treatment_directive": TREATMENT_DIRECTIVE.search(text) is None,
        "no_diagnostic_certainty": DIAGNOSTIC_CERTAINTY.search(text) is None,
        "no_embedded_contact_target": not _contains_contact_target(text),
    }


def evaluate_patient_message(message: str) -> list[str]:
    """Holds for reviewer-edited outbound text; the edit must pass the same content checks."""

    return sorted(
        TEXT_HOLD_BY_CHECK[name] for name, passed in text_checks(message).items() if not passed
    )


def evaluate_proposal(
    proposal: AgentProposal,
    assessment: RuleAssessment,
    snapshot: CustomerSnapshot,
) -> GuardrailResult:
    """Apply deterministic safety and quality checks to untrusted model output."""

    content = " ".join(
        [
            proposal.summary,
            proposal.recommended_action,
            proposal.patient_message,
            *proposal.reasoning_bullets,
        ]
    )
    checks = {
        "human_review_required": proposal.requires_human_review is True,
        **text_checks(content),
        "contact_channel_present": proposal.contact_channel.value != "none",
        "consent_channel_compatible": not (
            snapshot.consent_status != "synthetic_opted_in"
            and proposal.contact_channel.value != "none"
        ),
        "urgent_escalation_language": assessment.tier.value != "urgent"
        or URGENT_LANGUAGE.search(proposal.recommended_action) is not None,
        "reasoning_is_not_duplicated": len(set(proposal.reasoning_bullets))
        == len(proposal.reasoning_bullets),
    }
    return GuardrailResult(
        holds=sorted(HOLD_BY_CHECK[name] for name, passed in checks.items() if not passed),
        checks=checks,
    )
