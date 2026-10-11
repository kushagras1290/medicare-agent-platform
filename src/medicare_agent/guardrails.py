from __future__ import annotations

import re
from dataclasses import dataclass

from medicare_agent.schemas import AgentProposal, CustomerSnapshot, RuleAssessment


@dataclass(frozen=True)
class GuardrailResult:
    holds: list[str]
    checks: dict[str, bool]


TREATMENT_DIRECTIVE = re.compile(
    r"\b(?:increase|decrease|change|start|stop|skip)\b.{0,35}\b(?:dose|medication|medicine|drug|taking)\b",
    re.IGNORECASE,
)
DIAGNOSTIC_CERTAINTY = re.compile(
    r"\b(?:you have|this confirms|definitely|certainly)\b.{0,50}"
    r"\b(?:disease|condition|diagnosis|infection|failure)\b",
    re.IGNORECASE,
)
DIRECT_CONTACT = re.compile(r"(?:https?://|www\.)|(?:\+?\d[\d .()\-]{7,}\d)", re.IGNORECASE)
URGENT_LANGUAGE = re.compile(r"\b(?:urgent|emergency|immediate|immediately)\b", re.IGNORECASE)


def evaluate_proposal(
    proposal: AgentProposal,
    assessment: RuleAssessment,
    snapshot: CustomerSnapshot,
) -> GuardrailResult:
    """Apply deterministic safety and quality checks to untrusted model output."""

    content = " ".join([proposal.summary, proposal.recommended_action, *proposal.reasoning_bullets])
    checks = {
        "human_review_required": proposal.requires_human_review is True,
        "no_treatment_directive": TREATMENT_DIRECTIVE.search(content) is None,
        "no_diagnostic_certainty": DIAGNOSTIC_CERTAINTY.search(content) is None,
        "no_embedded_contact_target": DIRECT_CONTACT.search(content) is None,
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
    hold_by_check = {
        "human_review_required": "guardrail_human_review_missing",
        "no_treatment_directive": "guardrail_treatment_instruction",
        "no_diagnostic_certainty": "guardrail_diagnostic_claim",
        "no_embedded_contact_target": "guardrail_embedded_contact_target",
        "contact_channel_present": "guardrail_contact_channel_missing",
        "consent_channel_compatible": "guardrail_consent_channel_conflict",
        "urgent_escalation_language": "guardrail_urgent_escalation_missing",
        "reasoning_is_not_duplicated": "guardrail_duplicate_reasoning",
    }
    return GuardrailResult(
        holds=sorted(hold_by_check[name] for name, passed in checks.items() if not passed),
        checks=checks,
    )
