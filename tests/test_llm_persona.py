"""Tests for shared LLM persona helpers."""

from app.services.business_change_analysis_service import BUSINESS_CHANGE_ANALYSIS_SYSTEM_PROMPT
from app.services.business_change_assessment import BUSINESS_CHANGE_ASSESSMENT_SYSTEM_PROMPT
from app.services.business_change_policy_workflow_service import (
    BUSINESS_CHANGE_POLICY_MATCH_SYSTEM_PROMPT,
)
from app.services.compliance_report_service import COMPLIANCE_REPORT_SYSTEM_PROMPT
from app.services.gap_analysis_service import (
    BUSINESS_CHANGE_GAP_ANALYSIS_SYSTEM_PROMPT,
    GAP_ANALYSIS_SYSTEM_PROMPT,
    parse_phase1_brief,
)
from app.services.llm_persona import (
    FINANCIAL_LAW_EXPERT_PERSONA,
    INTERNAL_POLICY_DEEP_CONTEXT_INSTRUCTION,
    REG_AI_EXPERT_PERSONA,
    SENIOR_STAFF_CCO_PERSONA,
    internal_policy_analysis_workflow,
    with_financial_law_expert_persona,
    with_internal_policy_analysis_persona,
    with_senior_cco_persona,
)
from app.services.policy_revision_document import build_redline_generation_system_prompt


def test_financial_law_expert_persona_describes_senior_attorney_authority() -> None:
    lowered = FINANCIAL_LAW_EXPERT_PERSONA.lower()
    assert "regulatory attorney" in lowered
    assert "financial-law" in lowered or "financial law" in lowered
    assert "sec" in lowered and "finra" in lowered


def test_senior_staff_cco_persona_covers_operating_cco_expertise() -> None:
    lowered = SENIOR_STAFF_CCO_PERSONA.lower()
    assert "chief compliance officer" in lowered
    assert "financial sector" in lowered or "financial-sector" in lowered
    assert "30 years" in lowered
    assert "broker-dealer" in lowered or "broker dealers" in lowered
    assert "investment adviser" in lowered or "investment advisers" in lowered
    assert "finra" in lowered and "sec" in lowered and "cftc" in lowered
    assert "aml" in lowered
    assert "examin" in lowered  # exam / examination
    assert "wsp" in lowered or "supervis" in lowered
    assert "generic" in lowered  # explicitly not a generic corporate CCO


def test_combined_reg_ai_persona_includes_attorney_and_cco() -> None:
    assert FINANCIAL_LAW_EXPERT_PERSONA in REG_AI_EXPERT_PERSONA
    assert SENIOR_STAFF_CCO_PERSONA in REG_AI_EXPERT_PERSONA
    assert "dual-competence" in REG_AI_EXPERT_PERSONA.lower() or "together you are reg ai" in (
        REG_AI_EXPERT_PERSONA.lower()
    )


def test_with_financial_law_expert_persona_prefixes_combined_persona() -> None:
    prompt = with_financial_law_expert_persona("Perform gap analysis.")
    assert prompt.startswith(REG_AI_EXPERT_PERSONA.splitlines()[0])
    assert "Chief Compliance Officer" in prompt
    assert prompt.endswith("Perform gap analysis.")


def test_with_senior_cco_persona_prefixes_cco_only() -> None:
    prompt = with_senior_cco_persona("Review this marketing piece.")
    assert prompt.startswith(SENIOR_STAFF_CCO_PERSONA.splitlines()[0])
    assert "regulatory attorney" not in prompt.lower()
    assert prompt.endswith("Review this marketing piece.")


def test_with_internal_policy_analysis_persona_includes_deep_context() -> None:
    prompt = with_internal_policy_analysis_persona("Analyze this policy.")
    assert FINANCIAL_LAW_EXPERT_PERSONA.splitlines()[0] in prompt
    assert "Chief Compliance Officer" in prompt
    assert "Mandatory deep-context process" in prompt
    assert "Phase 1A — Deep Internal Policy understanding" in prompt
    assert "Phase 1B — Deep companion-source understanding" in prompt
    assert "business_change" in prompt
    assert "marketing_content" in prompt
    assert "phase1_brief" in prompt
    assert prompt.endswith("Analyze this policy.")


def test_internal_policy_analysis_workflow_lists_both_phases() -> None:
    workflow = internal_policy_analysis_workflow()
    assert len(workflow) == 3
    assert "Phase 1A" in workflow[0]
    assert "Phase 1B" in workflow[1]
    assert "Phase 2" in workflow[2]
    assert "sentence" in workflow[2].lower()
    assert "holistic" in workflow[2].lower() or "business_change" in workflow[2]
    assert workflow is not INTERNAL_POLICY_DEEP_CONTEXT_INSTRUCTION


def test_deep_context_requires_sentence_by_sentence_with_holistic_companions() -> None:
    lowered = INTERNAL_POLICY_DEEP_CONTEXT_INSTRUCTION.lower()
    assert "one sentence at a time" in lowered
    assert "business_change" in lowered
    assert "marketing_content" in lowered or "marketing" in lowered
    assert "false positives" in lowered
    assert "must emit" in lowered or "must" in lowered
    assert "must not reduce" in lowered or "finding count" in lowered


def test_cco_persona_does_not_authorize_collapsing_sentence_findings() -> None:
    lowered = (SENIOR_STAFF_CCO_PERSONA + REG_AI_EXPERT_PERSONA).lower()
    assert "finding cardinality" in lowered or "twelve findings" in lowered
    assert "theoretical maximalism" not in lowered
    assert "collapse" in lowered


def test_gap_and_redline_prompts_require_sentence_by_sentence_with_context() -> None:
    for prompt in (
        GAP_ANALYSIS_SYSTEM_PROMPT,
        BUSINESS_CHANGE_GAP_ANALYSIS_SYSTEM_PROMPT,
        build_redline_generation_system_prompt(),
    ):
        lowered = prompt.lower()
        assert "sentence-by-sentence" in lowered or "one sentence at a time" in lowered
        assert "holistic" in lowered
        assert "phase 1a" in lowered
        assert "business_change" in lowered or "business change" in lowered


def test_all_regulatory_system_prompts_include_shared_personas() -> None:
    for prompt in (
        GAP_ANALYSIS_SYSTEM_PROMPT,
        BUSINESS_CHANGE_ASSESSMENT_SYSTEM_PROMPT,
        build_redline_generation_system_prompt(),
    ):
        lowered = prompt.lower()
        assert "regulatory attorney" in lowered
        assert "chief compliance officer" in lowered


def test_all_internal_policy_analysis_prompts_require_deep_context() -> None:
    for prompt in (
        GAP_ANALYSIS_SYSTEM_PROMPT,
        BUSINESS_CHANGE_GAP_ANALYSIS_SYSTEM_PROMPT,
        BUSINESS_CHANGE_POLICY_MATCH_SYSTEM_PROMPT,
        COMPLIANCE_REPORT_SYSTEM_PROMPT,
        build_redline_generation_system_prompt(),
    ):
        assert "Mandatory deep-context process" in prompt
        assert "Phase 1A — Deep Internal Policy understanding" in prompt
        assert "phase1_brief" in prompt
        assert "Chief Compliance Officer" in prompt


def test_business_change_and_analysis_prompts_require_deep_read_first() -> None:
    assert "deeply read and understand the business-change description" in (
        BUSINESS_CHANGE_ASSESSMENT_SYSTEM_PROMPT.lower()
    )
    assert "phase1_brief" in BUSINESS_CHANGE_ANALYSIS_SYSTEM_PROMPT
    assert "Mandatory sequencing" in BUSINESS_CHANGE_ANALYSIS_SYSTEM_PROMPT


def test_gap_prompts_allow_repeated_rule_citation_across_findings() -> None:
    for prompt in (GAP_ANALYSIS_SYSTEM_PROMPT, BUSINESS_CHANGE_GAP_ANALYSIS_SYSTEM_PROMPT):
        lowered = prompt.lower()
        assert "same rule citation" in lowered
        assert "collapse" in lowered
        assert "one rule/gap/fix finding per" in lowered or "one finding with" in lowered


def test_redline_prompt_allows_repeated_rule_citation() -> None:
    lowered = build_redline_generation_system_prompt().lower()
    assert "same rule citation may appear" in lowered
    assert "do not reuse the same rule, gap, or fix wording" not in lowered


def test_parse_phase1_brief_keeps_nonempty_fields() -> None:
    brief = parse_phase1_brief(
        {
            "phase1_brief": {
                "policy_understanding": "AML program ownership and CIP controls.",
                "companion_understanding": "  FINRA 3310 AMLCO designation.  ",
                "covered_well": "",
                "silent_or_thin": "No named AMLCO.",
            }
        }
    )
    assert brief == {
        "policy_understanding": "AML program ownership and CIP controls.",
        "companion_understanding": "FINRA 3310 AMLCO designation.",
        "silent_or_thin": "No named AMLCO.",
    }
