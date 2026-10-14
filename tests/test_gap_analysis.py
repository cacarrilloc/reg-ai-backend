import json
from types import SimpleNamespace

import pytest
from ai_fakes import FakeLLM, result

import app.services.gap_analysis_service as gap_service
from app.core.config import Settings
from app.models.core import BusinessChange, DocumentChunk, GapAnalysis, Policy, RegulatoryPublication
from app.schemas.agent import AgentRunRequest
from app.services.gap_analysis_service import (
    GAP_ANALYSIS_SYSTEM_PROMPT,
    ChunkNode,
    PolicyCandidate,
    RetrievedChunk,
    TriggerContext,
    calibrate_regulator_scoped_impact,
    candidates_from_nodes,
    ensure_publication_regulator_candidates,
    identify_gap,
    parse_gap_fields_loosely,
    parse_sentence_findings,
    response_json,
)
from app.services.llm_json import parse_llm_json_dict
from app.services.policy_comparison_service import GovernmentExcerpt


async def _async_empty_excerpts() -> list[object]:
    return []


def sample_trigger() -> TriggerContext:
    return TriggerContext(
        org_id='org_example',
        trigger_type='publication',
        trigger_id='publication-id',
        publication=None,
        business_change=None,
        text='FINRA requires firms to document supervisory approval for communications.',
        citation_source='FINRA',
        citation_reference='https://www.finra.org/rules-guidance/notices/example',
        label='FINRA Regulatory Notice',
    )


def sample_candidate() -> PolicyCandidate:
    return PolicyCandidate(
        policy_id='policy-id',
        policy_name='Electronic Communications',
        policy_version=3,
        retrieval_mode='SEMANTIC_PGVECTOR',
        chunks=[
            RetrievedChunk(
                id='chunk-1',
                index=2,
                content='Supervisors review retained electronic communications every month.',
                similarity=0.92,
            )
        ],
    )


@pytest.mark.asyncio
async def test_opus_gap_prompt_uses_ephemeral_cache_and_only_keeps_trigger_citations() -> None:
    llm = FakeLLM(
        json.dumps(
            {
                'impact_score': 'CRITICAL',
                'findings': [
                    {
                        'sentence': 'Supervisors review exceptions annually.',
                        'impact_score': 'HIGH',
                        'rule': 'FINRA Rule 3110 — supervisory procedures',
                        'gap': 'Policy lacks documented approval control for exceptions.',
                        'fix': 'Require written supervisory approval evidence for each exception.',
                    }
                ],
                'citations': [
                    {
                        'source': 'FINRA',
                        'reference': 'https://www.finra.org/rules-guidance/notices/example',
                        'quote': 'document supervisory approval',
                        'rationale': 'The trigger adds approval evidence requirements.',
                    },
                    {
                        'source': 'Invented source',
                        'reference': 'https://example.invalid/not-a-source',
                    },
                ],
            }
        ),
        model_label='claude-opus-4-6',
    )

    settings = Settings(ANTHROPIC_API_KEY='test-key')
    finding = await identify_gap(
        sample_trigger(),
        sample_candidate(),
        settings,
        llm,
    )

    assert finding.impact_score == 'HIGH'
    assert finding.analysis_mode == 'LLM'
    assert finding.analysis_model == 'claude-opus-4-6'
    assert finding.citations == [
        {
            'source': 'FINRA',
            'reference': 'https://www.finra.org/rules-guidance/notices/example',
            'quote': 'document supervisory approval',
            'rationale': 'The trigger adds approval evidence requirements.',
        }
    ]
    request = llm.calls[0]
    assert request['system'] == GAP_ANALYSIS_SYSTEM_PROMPT
    assert request['cache_system'] is True
    assert request['throttle'] == 'opus'
    assert request['temperature'] == 0


def test_llamaindex_results_are_grouped_by_policy_and_limited_to_relevant_chunks() -> None:
    matches = [
        SimpleNamespace(
            node=ChunkNode(
                id_='chunk-1',
                text='First relevant excerpt',
                metadata={
                    'policy_id': 'policy-a',
                    'policy_name': 'Policy A',
                    'policy_version': 2,
                    'chunk_index': 0,
                },
            ),
            score=0.94,
        ),
        SimpleNamespace(
            node=ChunkNode(
                id_='chunk-2',
                text='Second relevant excerpt',
                metadata={
                    'policy_id': 'policy-a',
                    'policy_name': 'Policy A',
                    'policy_version': 2,
                    'chunk_index': 1,
                },
            ),
            score=0.83,
        ),
        SimpleNamespace(
            node=ChunkNode(
                id_='chunk-3',
                text='A different policy excerpt',
                metadata={
                    'policy_id': 'policy-b',
                    'policy_name': 'Policy B',
                    'policy_version': 1,
                    'chunk_index': 0,
                },
            ),
            score=0.76,
        ),
    ]

    candidates = candidates_from_nodes(matches, 'SEMANTIC_PGVECTOR')

    assert [candidate.policy_id for candidate in candidates] == ['policy-a', 'policy-b']
    assert [chunk.id for chunk in candidates[0].chunks] == ['chunk-1', 'chunk-2']
    assert candidates[0].retrieval_mode == 'SEMANTIC_PGVECTOR'


def test_parse_llm_json_dict_repairs_trailing_commas() -> None:
    payload = parse_llm_json_dict(
        '{"impact_score":"MEDIUM","plain_english_summary":"Needs review.",'
        '"citations":[{"source":"FINRA","reference":"https://example.com","quote":"x",},],}',
        loose_parser=parse_gap_fields_loosely,
    )
    assert payload['impact_score'] == 'MEDIUM'
    assert payload['plain_english_summary'] == 'Needs review.'


def test_parse_llm_json_dict_recovers_loose_multiline_summary() -> None:
    text = """```json
{
  "impact_score": "HIGH",
  "plain_english_summary": "Section 4.1 requires supervisory approval.
The policy does not cover savings accounts.",
  "citations": [
    {
      "source": "Business change",
      "reference": "Business change abc (NEW_PRODUCT)",
      "quote": "new savings account",
      "rationale": "Introduces a new product",
    }
  ]
}
```"""
    payload = parse_llm_json_dict(text, loose_parser=parse_gap_fields_loosely)
    assert payload['impact_score'] == 'HIGH'
    assert 'savings accounts' in payload['plain_english_summary']


def test_parse_llm_json_dict_recovers_truncated_unterminated_summary() -> None:
    text = """{
  "impact_score": "HIGH",
  "plain_english_summary": "The AML Policy does not currently cover savings account onboarding controls.
Section 2 only lists brokerage accounts and needs expansion for the new college debt product.
Additional monitoring rules are required for outbound creditor payments and student onboarding."""
    payload = parse_llm_json_dict(text, loose_parser=parse_gap_fields_loosely)
    assert payload['impact_score'] == 'HIGH'
    assert 'savings account onboarding' in payload['plain_english_summary']
    assert 'student onboarding' in payload['plain_english_summary']


def test_normalize_impact_score_maps_none_to_info() -> None:
    from app.services.gap_analysis_service import normalize_impact_score

    assert normalize_impact_score('NONE') == 'INFO'
    assert normalize_impact_score('info') == 'INFO'
    assert normalize_impact_score('LOW') == 'INFO'


def test_calibrate_business_change_impact_elevates_info_for_flagged_regulator_policies() -> None:
    from app.services.gap_analysis_service import GapFinding, calibrate_business_change_impact

    trigger = TriggerContext(
        org_id='org_example',
        trigger_type='business_change',
        trigger_id='change-id',
        publication=None,
        business_change=BusinessChange(
            id='change-id',
            org_id='org_example',
            change_type='NEW_PROCESS',
            description='Launch overseas advisor supervision.',
            triggers_review=True,
        ),
        text='Launch overseas advisor supervision.',
        citation_source='Business change',
        citation_reference='Business change change-id (NEW_PROCESS)',
        label='New Process: Launch overseas advisor supervision.',
    )
    candidate = PolicyCandidate(
        policy_id='policy-finra',
        policy_name='FINRA',
        policy_version=1,
        retrieval_mode='MANDATORY_POLICY_CHUNKS',
        chunks=[
            RetrievedChunk(
                id='chunk-1',
                index=0,
                content='Supervisory procedures for registered representatives.',
                similarity=0.0,
            )
        ],
    )
    finding = GapFinding(
        impact_score='INFO',
        summary='No material gap identified.',
        citations=[],
        analysis_mode='LLM',
        analysis_model='claude-opus-4-6',
    )

    calibrated = calibrate_business_change_impact(trigger, candidate, finding)

    assert calibrated.impact_score == 'MEDIUM'
    assert 'NEW_PROCESS' in calibrated.summary
    assert 'requires CCO review' in calibrated.summary


def test_calibrate_business_change_impact_elevates_info_even_when_not_flagged() -> None:
    from app.services.gap_analysis_service import GapFinding, calibrate_business_change_impact

    trigger = TriggerContext(
        org_id='org_example',
        trigger_type='business_change',
        trigger_id='change-id',
        publication=None,
        business_change=BusinessChange(
            id='change-id',
            org_id='org_example',
            change_type='NEW_PROCESS',
            description='Launch overseas advisor supervision.',
            triggers_review=False,
        ),
        text='Launch overseas advisor supervision.',
        citation_source='Business change',
        citation_reference='Business change change-id (NEW_PROCESS)',
        label='New Process: Launch overseas advisor supervision.',
    )
    finding = GapFinding(
        impact_score='INFO',
        summary='No material gap identified.',
        citations=[],
        analysis_mode='LLM',
        analysis_model='claude-opus-4-6',
    )

    calibrated = calibrate_business_change_impact(trigger, sample_candidate(), finding)

    assert calibrated.impact_score == 'MEDIUM'


def test_calibrate_business_change_impact_leaves_publication_info_unchanged() -> None:
    from app.services.gap_analysis_service import GapFinding, calibrate_business_change_impact

    finding = GapFinding(
        impact_score='INFO',
        summary='No material gap identified.',
        citations=[],
        analysis_mode='LLM',
        analysis_model='claude-opus-4-6',
    )

    calibrated = calibrate_business_change_impact(sample_trigger(), sample_candidate(), finding)

    assert calibrated.impact_score == 'INFO'


def test_parse_sentence_findings_keeps_same_rule_with_distinct_gaps() -> None:
    """AML policies often cite FINRA 3310 on many sentences — must not collapse to 1."""
    raw = {
        'findings': [
            {
                'sentence': f'Sentence {index} about AML control {index}.',
                'rule': 'FINRA Rule 3310',
                'gap': f'Gap description number {index} for this control.',
                'fix': f'Fix description number {index} for this control.',
                'impact_score': 'HIGH',
            }
            for index in range(1, 13)
        ]
    }
    findings = parse_sentence_findings(raw)
    assert len(findings) == 12
    assert all(item.rule == 'FINRA Rule 3310' for item in findings)


def test_parse_sentence_findings_drops_identical_gap_duplicates() -> None:
    findings = parse_sentence_findings(
        {
            'findings': [
                {
                    'sentence': 'First sentence.',
                    'rule': 'FINRA Rule 3310',
                    'gap': 'Missing named AMLCO designation.',
                    'fix': 'Name the AMLCO.',
                    'impact_score': 'HIGH',
                },
                {
                    'sentence': 'Second sentence.',
                    'rule': 'FINRA Rule 3310 — AML Program',
                    'gap': 'Missing named AMLCO designation.',
                    'fix': 'Designate an AMLCO in writing.',
                    'impact_score': 'HIGH',
                },
            ]
        }
    )
    assert len(findings) == 1


def test_response_json_reads_standard_model_payload() -> None:
    payload = response_json(
        result(
            json.dumps(
                {
                    'impact_score': 'INFO',
                    'plain_english_summary': 'No change needed.',
                    'citations': [],
                }
            )
        )
    )
    assert payload['impact_score'] == 'INFO'


@pytest.mark.asyncio
async def test_shared_agent_workflow_persists_gap_without_creating_a_redline(monkeypatch) -> None:
    publication = RegulatoryPublication(
        id='publication-id',
        source='FINRA',
        title='FINRA Regulatory Notice',
        url='https://www.finra.org/rules-guidance/notices/example',
        raw_text='FINRA requires a documented supervisory approval.',
        processed=False,
    )

    class Query:
        def __init__(self, result: object) -> None:
            self.result = result

        def filter(self, *_args: object) -> object:
            return self

        def distinct(self, *_args: object) -> object:
            return self

        def outerjoin(self, *_args: object, **_kwargs: object) -> object:
            return self

        def order_by(self, *_args: object) -> object:
            return self

        def limit(self, *_args: object) -> object:
            return self

        def one_or_none(self) -> object:
            return self.result if not isinstance(self.result, list) else None

        def first(self) -> object:
            return self.result if not isinstance(self.result, list) else None

        def all(self) -> list[tuple[str]] | list[DocumentChunk]:
            if isinstance(self.result, list):
                return self.result
            return []

    class RecordingSession:
        def __init__(self) -> None:
            self.added: list[object] = []
            self.committed = False
            self.closed = False

        def query(self, entity: object) -> Query:
            if entity is RegulatoryPublication:
                return Query(publication)
            if entity is Policy:
                return Query(None)
            if entity is DocumentChunk:
                return Query([])
            return Query([])

        def add(self, value: object) -> None:
            self.added.append(value)

        def commit(self) -> None:
            self.committed = True

        def rollback(self) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    db = RecordingSession()
    settings = Settings(OPENAI_API_KEY='', ANTHROPIC_API_KEY='', AUTO_GENERATE_REDLINES=False)

    async def fake_candidates(*_args: object, **_kwargs: object) -> list[PolicyCandidate]:
        return [sample_candidate()]

    monkeypatch.setattr(gap_service, 'SessionLocal', lambda: db)
    monkeypatch.setattr(gap_service, 'get_settings', lambda: settings)
    monkeypatch.setattr(gap_service, 'retrieve_policy_candidates', fake_candidates)
    monkeypatch.setattr(gap_service, 'retrieve_context_excerpts', lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        gap_service,
        'retrieve_government_excerpts_for_internal_policy',
        lambda *_args, **_kwargs: _async_empty_excerpts(),
    )
    monkeypatch.setattr(
        gap_service,
        'retrieve_government_excerpts_for_business_change',
        lambda *_args, **_kwargs: _async_empty_excerpts(),
    )
    monkeypatch.setattr(
        gap_service,
        'compare_internal_policy_to_regulator_documents_with_fallback',
        lambda *_args, **_kwargs: _async_empty_excerpts(),
    )

    response = await gap_service.run_gap_analysis(
        AgentRunRequest(
            trigger_type='publication',
            trigger_id=publication.id,
            org_id='org_example',
        )
    )

    analyses = [value for value in db.added if isinstance(value, GapAnalysis)]
    assert response.status == 'completed'
    assert len(analyses) == 1
    assert analyses[0].trigger_type == 'publication'
    assert analyses[0].trigger_id == publication.id
    assert analyses[0].impact_score == 'HIGH'
    assert analyses[0].retrieval_metadata['chunk_ids'] == ['chunk-1']
    assert analyses[0].analysis_mode == 'RULE_FALLBACK'
    assert publication.processed is True
    assert db.committed is True
    assert db.closed is True


def test_resolve_mandatory_policy_ids_for_business_change() -> None:
    class PolicyQuery:
        def filter(self, *_args: object, **_kwargs: object) -> object:
            return self

        def order_by(self, *_args: object, **_kwargs: object) -> object:
            return self

        def all(self) -> list[tuple[str]]:
            return [('policy-a',), ('policy-b',)]

    class Session:
        def query(self, _entity: object) -> PolicyQuery:
            return PolicyQuery()

    trigger = TriggerContext(
        org_id='org_example',
        trigger_type='business_change',
        trigger_id='change-id',
        publication=None,
        business_change=None,
        text='New product launch',
        citation_source='Business change',
        citation_reference='Business change change-id (NEW_PRODUCT)',
        label='New Product: launch',
    )
    payload = AgentRunRequest(
        trigger_type='business_change',
        trigger_id='change-id',
        org_id='org_example',
        force_reanalysis=True,
    )

    assert gap_service.resolve_mandatory_policy_ids(Session(), trigger, payload) == [
        'policy-a',
        'policy-b',
    ]


def test_resolve_mandatory_policy_ids_for_policy_update() -> None:
    trigger = TriggerContext(
        org_id='org_example',
        trigger_type='policy_update',
        trigger_id='uploaded-policy-id',
        publication=None,
        business_change=None,
        text='Policy library update',
        citation_source='Policy library',
        citation_reference='Policy upload Example v1',
        label='Example v1',
    )
    payload = AgentRunRequest(
        trigger_type='policy_update',
        trigger_id='uploaded-policy-id',
        org_id='org_example',
        force_reanalysis=True,
    )

    assert gap_service.resolve_mandatory_policy_ids(None, trigger, payload) == ['uploaded-policy-id']


@pytest.mark.asyncio
async def test_business_change_run_analyzes_every_mandatory_policy(monkeypatch) -> None:
    change = BusinessChange(
        id='change-id',
        org_id='org_example',
        change_type='NEW_PRODUCT',
        description='Launching a new savings account.',
        triggers_review=True,
    )
    candidates = [
        PolicyCandidate(
            policy_id=f'policy-{index}',
            policy_name=f'Policy {index}',
            policy_version=1,
            retrieval_mode='MANDATORY_POLICY_CHUNKS',
            chunks=[
                RetrievedChunk(
                    id=f'chunk-{index}',
                    index=0,
                    content=f'Controls for policy {index}.',
                    similarity=0.0,
                )
            ],
        )
        for index in range(3)
    ]

    class Query:
        def __init__(self, result: object) -> None:
            self.result = result

        def filter(self, *_args: object, **_kwargs: object) -> object:
            return self

        def distinct(self, *_args: object) -> object:
            return self

        def outerjoin(self, *_args: object, **_kwargs: object) -> object:
            return self

        def order_by(self, *_args: object) -> object:
            return self

        def delete(self, **_kwargs: object) -> object:
            return self

        def one_or_none(self) -> object:
            return self.result

        def all(self) -> list[object]:
            if isinstance(self.result, list):
                return self.result
            return []

    class RecordingSession:
        def __init__(self) -> None:
            self.added: list[object] = []
            self.committed = False
            self.closed = False

        def query(self, entity: object) -> Query:
            if entity is BusinessChange:
                return Query(change)
            return Query([])

        def add(self, value: object) -> None:
            self.added.append(value)

        def commit(self) -> None:
            self.committed = True

        def rollback(self) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    db = RecordingSession()
    settings = Settings(OPENAI_API_KEY='', ANTHROPIC_API_KEY='', AUTO_GENERATE_REDLINES=False)

    async def fake_mandatory_candidates(*_args: object, **_kwargs: object) -> list[PolicyCandidate]:
        return candidates

    def fake_mandatory_policy_ids(*_args: object, **_kwargs: object) -> list[str]:
        return ['policy-0', 'policy-1', 'policy-2']

    monkeypatch.setattr(gap_service, 'SessionLocal', lambda: db)
    monkeypatch.setattr(gap_service, 'get_settings', lambda: settings)
    monkeypatch.setattr(gap_service, 'resolve_mandatory_policy_ids', fake_mandatory_policy_ids)
    monkeypatch.setattr(gap_service, 'mandatory_policy_candidates', fake_mandatory_candidates)
    monkeypatch.setattr(gap_service, 'retrieve_context_excerpts', lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        gap_service,
        'retrieve_government_excerpts_for_business_change',
        lambda *_args, **_kwargs: _async_empty_excerpts(),
    )
    monkeypatch.setattr(
        gap_service,
        'retrieve_government_excerpts_for_internal_policy',
        lambda *_args, **_kwargs: _async_empty_excerpts(),
    )
    monkeypatch.setattr(
        gap_service,
        'compare_internal_policy_to_regulator_documents_with_fallback',
        lambda *_args, **_kwargs: _async_empty_excerpts(),
    )

    response = await gap_service.run_gap_analysis(
        AgentRunRequest(
            trigger_type='business_change',
            trigger_id=change.id,
            org_id='org_example',
            force_reanalysis=True,
            auto_generate_redlines=False,
        )
    )

    analyses = [value for value in db.added if isinstance(value, GapAnalysis)]
    assert response.status == 'completed'
    assert len(analyses) == 3
    assert {analysis.policy_id for analysis in analyses} == {'policy-0', 'policy-1', 'policy-2'}
    assert all(analysis.trigger_type == 'business_change' for analysis in analyses)
    assert all(analysis.business_change_id == change.id for analysis in analyses)


def test_ensure_publication_regulator_candidates_prioritizes_matching_internal_policy() -> None:
    sec_policy = Policy(
        id='policy-sec',
        org_id='org_example',
        name='SEC',
        type='uploaded',
        version=1,
        status='ACTIVE',
    )
    sec_chunk = DocumentChunk(
        id='chunk-sec',
        org_id='org_example',
        policy_id='policy-sec',
        chunk_index=0,
        content='SEC supervisory procedures.',
    )

    class FakePolicyQuery:
        def filter(self, *_args: object, **_kwargs: object):
            return self

        def order_by(self, *_args: object, **_kwargs: object):
            return self

        def first(self) -> Policy | None:
            return sec_policy

    class FakeChunkQuery:
        def filter(self, *_args: object, **_kwargs: object):
            return self

        def order_by(self, *_args: object, **_kwargs: object):
            return self

        def limit(self, *_args: object, **_kwargs: object):
            return self

        def all(self) -> list[DocumentChunk]:
            return [sec_chunk]

    class FakeSession:
        def query(self, model: object) -> FakePolicyQuery | FakeChunkQuery:
            if model is Policy:
                return FakePolicyQuery()
            return FakeChunkQuery()

    existing = [
        PolicyCandidate(
            policy_id='policy-finra',
            policy_name='FINRA',
            policy_version=1,
            retrieval_mode='SEMANTIC_PGVECTOR',
            chunks=[
                RetrievedChunk(
                    id='chunk-finra',
                    index=0,
                    content='FINRA procedures.',
                    similarity=0.95,
                )
            ],
        )
    ]

    candidates = ensure_publication_regulator_candidates(
        FakeSession(),  # type: ignore[arg-type]
        'org_example',
        'SEC',
        existing,
    )

    assert candidates[0].policy_id == 'policy-sec'
    assert candidates[0].policy_name == 'SEC'
    assert any(candidate.policy_id == 'policy-finra' for candidate in candidates)


def test_calibrate_regulator_scoped_impact_keeps_info_when_findings_empty() -> None:
    trigger = TriggerContext(
        org_id='org_example',
        trigger_type='publication',
        trigger_id='publication-id',
        publication=RegulatoryPublication(
            id='publication-id',
            source='SEC',
            title='SEC Notice',
            url='https://www.sec.gov/example',
        ),
        business_change=None,
        text='The SEC requires additional disclosure controls.',
        citation_source='SEC',
        citation_reference='https://www.sec.gov/example',
        label='SEC Notice',
    )
    candidate = PolicyCandidate(
        policy_id='policy-sec',
        policy_name='SEC',
        policy_version=1,
        retrieval_mode='PUBLICATION_REGULATOR_MATCH',
        chunks=[
            RetrievedChunk(
                id='chunk-sec',
                index=0,
                content='Disclosure controls and procedures.',
                similarity=0.8,
            )
        ],
    )
    finding = calibrate_regulator_scoped_impact(
        trigger,
        candidate,
        gap_service.GapFinding(
            impact_score='INFO',
            summary='No material regulation discrepancies found for SEC (version 1).',
            citations=[],
            analysis_mode='LLM',
            analysis_model='claude-opus-4-6',
        ),
        [
            GovernmentExcerpt(
                policy_id='ref-sec',
                policy_name='SEC Newsroom Reference',
                regulator_source='SEC',
                chunk_id='ref-chunk',
                chunk_index=0,
                content='SEC supervisory guidance.',
                similarity=0.7,
            )
        ],
    )

    assert finding.impact_score == 'INFO'


def test_calibrate_regulator_scoped_impact_promotes_info_when_findings_exist() -> None:
    trigger = TriggerContext(
        org_id='org_example',
        trigger_type='policy_update',
        trigger_id='policy-sec',
        publication=None,
        business_change=None,
        text='Policy upload: SEC v2',
        citation_source='Policy Upload',
        citation_reference='policy-sec',
        label='Policy upload: SEC v2',
    )
    candidate = PolicyCandidate(
        policy_id='policy-sec',
        policy_name='SEC',
        policy_version=2,
        retrieval_mode='MANDATORY_POLICY',
        chunks=[
            RetrievedChunk(
                id='chunk-sec',
                index=0,
                content='Disclosure controls and procedures.',
                similarity=0.8,
            )
        ],
    )
    finding = calibrate_regulator_scoped_impact(
        trigger,
        candidate,
        gap_service.GapFinding(
            impact_score='INFO',
            summary='SEC has 1 regulation gap(s) requiring CCO review. First gap: missing review.',
            citations=[],
            analysis_mode='LLM',
            analysis_model='claude-opus-4-6',
            sentence_findings=(
                gap_service.SentenceGapFinding(
                    sentence='The firm reviews disclosures annually.',
                    rule='SEC Rule 206(4)-7',
                    gap='No documented annual review cadence.',
                    fix='Require annual documented review.',
                    impact_score='MEDIUM',
                ),
            ),
        ),
        [
            GovernmentExcerpt(
                policy_id='ref-sec',
                policy_name='SEC Newsroom Reference',
                regulator_source='SEC',
                chunk_id='ref-chunk',
                chunk_index=0,
                content='SEC supervisory guidance.',
                similarity=0.7,
            )
        ],
    )

    assert finding.impact_score == 'MEDIUM'


def test_regulator_similarity_fields_extracts_primary_match() -> None:
    fields = gap_service.regulator_similarity_fields(
        {
            'regulator_document_similarity': [
                {
                    'regulator_source': 'SEC',
                    'policy_name': 'SEC Newsroom Reference',
                    'cosine_similarity': 0.812345,
                }
            ]
        }
    )

    assert fields['regulator_cosine_similarity'] == pytest.approx(0.812345)
    assert fields['regulator_reference_policy_name'] == 'SEC Newsroom Reference'


def test_regulator_similarity_fields_prefers_all_regulators_score() -> None:
    fields = gap_service.regulator_similarity_fields(
        {
            'regulator_document_similarity': [
                {
                    'regulator_source': 'SEC',
                    'policy_name': 'SEC Regulations',
                    'cosine_similarity': 0.91,
                },
                {
                    'regulator_source': 'ALL',
                    'policy_name': 'FINRA, SEC, and CFTC Regulations',
                    'cosine_similarity': 0.64,
                },
            ]
        }
    )

    assert fields['regulator_cosine_similarity'] == pytest.approx(0.64)
    assert fields['regulator_reference_policy_name'] == 'FINRA, SEC, and CFTC Regulations'


def test_attach_regulator_similarity_fills_empty_policy_update_gap() -> None:
    from app.services.document_chunk_service import RegulatorDocumentSimilarity
    from app.services.policy_types import REGULATORY_REFERENCE_TYPE

    gap = GapAnalysis(
        id='gap-aml',
        org_id='org_example',
        trigger_type='policy_update',
        trigger_id='policy-1',
        policy_id='policy-1',
        impact_score='HIGH',
        gap_summary='Updated',
        retrieval_metadata={'retrieval_mode': 'VECTOR'},
        analysis_mode='ANTHROPIC',
    )

    class Query:
        def filter(self, *_args: object, **_kwargs: object):
            return self

        def all(self):
            return [gap]

    class SessionStub:
        def query(self, *_args: object):
            return Query()

    gap_service.attach_regulator_similarity_to_policy_update_gap(
        SessionStub(),  # type: ignore[arg-type]
        org_id='org_example',
        policy_id='policy-1',
        similarities=[
            RegulatorDocumentSimilarity(
                regulator_source='ALL',
                policy_id='all-regulators',
                policy_name='FINRA, SEC, and CFTC Regulations',
                document_type=REGULATORY_REFERENCE_TYPE,
                cosine_similarity=0.606,
                internal_chunk_count=4,
                reference_chunk_count=12,
            )
        ],
    )

    assert gap.retrieval_metadata['regulator_document_similarity'][0]['cosine_similarity'] == pytest.approx(
        0.606
    )
    fields = gap_service.regulator_similarity_fields(gap.retrieval_metadata)
    assert fields['regulator_cosine_similarity'] == pytest.approx(0.606)


def test_business_change_similarity_fields_reads_stored_match_score() -> None:
    fields = gap_service.business_change_similarity_fields(
        {'business_change_policy_cosine_similarity': 0.734}
    )

    assert fields['business_change_cosine_similarity'] == pytest.approx(0.734)


def test_persist_inserts_policy_update_beside_business_change_gap(monkeypatch) -> None:
    existing = GapAnalysis(
        id='gap-business-change',
        org_id='org_example',
        trigger_type='business_change',
        trigger_id='change-1',
        business_change_id='change-1',
        policy_id='policy-1',
        impact_score='HIGH',
        gap_summary='Business change gap.',
        regulatory_citations=[],
        retrieval_metadata={'retrieval_mode': 'VECTOR'},
        analysis_mode='ANTHROPIC',
    )
    policy_row = Policy(
        id='policy-1',
        org_id='org_example',
        name='AML Test Policy',
        type='INTERNAL',
        status='ACTIVE',
        version=2,
    )
    added: list[GapAnalysis] = []

    class EmptyQuery:
        def filter(self, *_args: object, **_kwargs: object):
            return self

        def one_or_none(self) -> None:
            return None

    class PolicyQuery:
        def filter(self, *_args: object, **_kwargs: object):
            return self

        def one_or_none(self) -> Policy:
            return policy_row

    class SessionStub:
        def query(self, *entities: object) -> object:
            first = entities[0] if entities else None
            if first is Policy:
                return PolicyQuery()
            return EmptyQuery()

        def add(self, value: object) -> None:
            if isinstance(value, GapAnalysis):
                added.append(value)

        def flush(self) -> None:
            return None

    monkeypatch.setattr(
        gap_service,
        '_gap_analyses_for_policy_name',
        lambda *_args, **_kwargs: [existing],
    )
    monkeypatch.setattr(
        'app.services.compliance_history_service.safe_upsert_compliance_history_for_gap',
        lambda *args, **kwargs: None,
    )

    finding = gap_service.GapFinding(
        impact_score='MEDIUM',
        summary='Policy upload analysis.',
        citations=[],
        analysis_mode='ANTHROPIC',
        analysis_model='claude-test',
    )
    gap_id = gap_service.persist_gap_analysis_result(
        SessionStub(),  # type: ignore[arg-type]
        org_id='org_example',
        publication_id=None,
        business_change_id=None,
        trigger_type='policy_update',
        trigger_id='policy-1',
        policy_id='policy-1',
        finding=finding,
        retrieval_metadata={'retrieval_mode': 'VECTOR'},
        force_update=True,
    )

    assert gap_id != 'gap-business-change'
    assert existing.trigger_type == 'business_change'
    assert existing.gap_summary == 'Business change gap.'
    assert added[0].id == gap_id
    assert added[0].trigger_type == 'policy_update'
    assert added[0].gap_summary == 'Policy upload analysis.'
    assert added[0].retrieval_metadata.get(gap_service.NEEDS_REVIEW_DECISION_KEY) is not True


def test_persist_inserts_business_change_beside_policy_update_gap(monkeypatch) -> None:
    existing = GapAnalysis(
        id='gap-policy-update',
        org_id='org_example',
        trigger_type='policy_update',
        trigger_id='policy-1',
        policy_id='policy-1',
        impact_score='MEDIUM',
        gap_summary='Upload gap.',
        regulatory_citations=[],
        retrieval_metadata={'retrieval_mode': 'VECTOR'},
        analysis_mode='ANTHROPIC',
    )
    policy_row = Policy(
        id='policy-1',
        org_id='org_example',
        name='AML Test Policy',
        type='INTERNAL',
        status='ACTIVE',
        version=2,
    )
    added: list[GapAnalysis] = []

    class EmptyQuery:
        def filter(self, *_args: object, **_kwargs: object):
            return self

        def one_or_none(self) -> None:
            return None

    class PolicyQuery:
        def filter(self, *_args: object, **_kwargs: object):
            return self

        def one_or_none(self) -> Policy:
            return policy_row

    class SessionStub:
        def query(self, *entities: object) -> object:
            first = entities[0] if entities else None
            if first is Policy:
                return PolicyQuery()
            return EmptyQuery()

        def add(self, value: object) -> None:
            if isinstance(value, GapAnalysis):
                added.append(value)

        def flush(self) -> None:
            return None

    monkeypatch.setattr(
        gap_service,
        '_gap_analyses_for_policy_name',
        lambda *_args, **_kwargs: [existing],
    )
    monkeypatch.setattr(
        'app.services.compliance_history_service.safe_upsert_compliance_history_for_gap',
        lambda *args, **kwargs: None,
    )

    finding = gap_service.GapFinding(
        impact_score='HIGH',
        summary='Business change analysis.',
        citations=[],
        analysis_mode='ANTHROPIC',
        analysis_model='claude-test',
    )
    gap_id = gap_service.persist_gap_analysis_result(
        SessionStub(),  # type: ignore[arg-type]
        org_id='org_example',
        publication_id=None,
        business_change_id='change-1',
        trigger_type='business_change',
        trigger_id='change-1',
        policy_id='policy-1',
        finding=finding,
        retrieval_metadata={'retrieval_mode': 'VECTOR'},
        force_update=True,
    )

    assert gap_id != 'gap-policy-update'
    assert existing.trigger_type == 'policy_update'
    assert existing.gap_summary == 'Upload gap.'
    assert added[0].id == gap_id
    assert added[0].trigger_type == 'business_change'
    assert added[0].gap_summary == 'Business change analysis.'


def test_persist_overwrites_same_business_change_gap_on_rerun(monkeypatch) -> None:
    existing = GapAnalysis(
        id='gap-business-change',
        org_id='org_example',
        trigger_type='business_change',
        trigger_id='change-1',
        business_change_id='change-1',
        policy_id='policy-v1',
        impact_score='MEDIUM',
        gap_summary='First business change analysis.',
        regulatory_citations=[],
        retrieval_metadata={'retrieval_mode': 'VECTOR'},
        analysis_mode='ANTHROPIC',
    )
    policy_row = Policy(
        id='policy-v2',
        org_id='org_example',
        name='AML Test Policy',
        type='INTERNAL',
        status='ACTIVE',
        version=2,
    )
    added: list[GapAnalysis] = []

    class EmptyQuery:
        def filter(self, *_args: object, **_kwargs: object):
            return self

        def one_or_none(self) -> None:
            return None

    class PolicyQuery:
        def filter(self, *_args: object, **_kwargs: object):
            return self

        def one_or_none(self) -> Policy:
            return policy_row

    class SessionStub:
        def query(self, *entities: object) -> object:
            first = entities[0] if entities else None
            if first is Policy:
                return PolicyQuery()
            return EmptyQuery()

        def add(self, value: object) -> None:
            if isinstance(value, GapAnalysis):
                added.append(value)

        def flush(self) -> None:
            return None

        def delete(self, obj: object) -> None:
            return None

    monkeypatch.setattr(
        gap_service,
        '_gap_analyses_for_policy_name',
        lambda *_args, **_kwargs: [existing],
    )
    monkeypatch.setattr(
        gap_service,
        '_rebind_gap_policy_revisions',
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        'app.services.compliance_history_service.safe_upsert_compliance_history_for_gap',
        lambda *args, **kwargs: None,
    )

    finding = gap_service.GapFinding(
        impact_score='HIGH',
        summary='Re-run of the same business change.',
        citations=[],
        analysis_mode='ANTHROPIC',
        analysis_model='claude-test',
    )
    gap_id = gap_service.persist_gap_analysis_result(
        SessionStub(),  # type: ignore[arg-type]
        org_id='org_example',
        publication_id=None,
        business_change_id='change-1',
        trigger_type='business_change',
        trigger_id='change-1',
        policy_id='policy-v2',
        finding=finding,
        retrieval_metadata={'retrieval_mode': 'VECTOR'},
        force_update=True,
    )

    assert gap_id == 'gap-business-change'
    assert added == []
    assert existing.policy_id == 'policy-v2'
    assert existing.trigger_id == 'change-1'
    assert existing.business_change_id == 'change-1'
    assert existing.impact_score == 'HIGH'
    assert existing.gap_summary == 'Re-run of the same business change.'


def test_persist_gap_analysis_result_updates_existing_instead_of_inserting() -> None:
    """Overwrite the same-name Policy Update row and prompt Request Review again."""
    existing = GapAnalysis(
        id='gap-existing',
        org_id='org_example',
        trigger_type='policy_update',
        trigger_id='policy-1',
        policy_id='policy-1',
        impact_score='INFO',
        gap_summary='Old summary',
        regulatory_citations=[],
        retrieval_metadata={'retrieval_mode': 'LEXICAL'},
        analysis_mode='RULE_FALLBACK',
    )

    class Query:
        def filter(self, *_args: object, **_kwargs: object) -> object:
            return self

        def one_or_none(self) -> GapAnalysis:
            return existing

    class SessionStub:
        def query(self, *_args: object) -> Query:
            return Query()

    finding = gap_service.GapFinding(
        impact_score='HIGH',
        summary='Updated summary',
        citations=[{'source': 'FINRA', 'reference': 'https://example.com'}],
        analysis_mode='ANTHROPIC',
        analysis_model='claude-test',
    )
    gap_id = gap_service.persist_gap_analysis_result(
        SessionStub(),  # type: ignore[arg-type]
        org_id='org_example',
        publication_id=None,
        business_change_id=None,
        trigger_type='policy_update',
        trigger_id='policy-1',
        policy_id='policy-1',
        finding=finding,
        retrieval_metadata={'retrieval_mode': 'VECTOR'},
        force_update=True,
    )

    assert gap_id == 'gap-existing'
    assert existing.impact_score == 'HIGH'
    assert existing.gap_summary == 'Updated summary'
    assert existing.analysis_mode == 'ANTHROPIC'
    assert existing.retrieval_metadata['retrieval_mode'] == 'VECTOR'
    assert existing.retrieval_metadata[gap_service.NEEDS_REVIEW_DECISION_KEY] is True


def test_persist_overwrites_same_policy_name_rebinds_revisions_before_policy_id(
    monkeypatch,
) -> None:
    """Composite FK on policy_revisions requires rebinding before gap.policy_id changes."""
    from app.models.core import PolicyRevision

    older = GapAnalysis(
        id='gap-v7',
        org_id='org_example',
        trigger_type='policy_update',
        trigger_id='policy-v7',
        policy_id='policy-v7',
        impact_score='HIGH',
        gap_summary='Old version gap',
        regulatory_citations=[],
        retrieval_metadata={},
        analysis_mode='ANTHROPIC',
    )
    events: list[str] = []

    policy_row = Policy(
        id='policy-v8',
        org_id='org_example',
        name='AML Test Policy',
        type='INTERNAL',
        status='ACTIVE',
        version=8,
        file_url='https://example.com/aml-v8.docx',
    )

    class IdentityQuery:
        def filter(self, *_args: object, **_kwargs: object):
            return self

        def one_or_none(self):
            return None

    class PolicyQuery:
        def filter(self, *_args: object, **_kwargs: object):
            return self

        def one_or_none(self):
            return policy_row

    class RevisionQuery:
        def filter(self, *_args: object, **_kwargs: object):
            return self

        def update(self, values: dict[str, object], synchronize_session: object = False):
            del synchronize_session
            events.append(f'revision_update:{values[PolicyRevision.policy_id]}')
            # Gap policy_id must still be the previous version at this point.
            assert older.policy_id == 'policy-v7'
            return 1

    class SessionStub:
        def query(self, *entities: object):
            first = entities[0] if entities else None
            if first is PolicyRevision:
                return RevisionQuery()
            if first is Policy:
                return PolicyQuery()
            return IdentityQuery()

        def flush(self) -> None:
            events.append('flush')

        def delete(self, obj: object) -> None:
            events.append(f'delete:{getattr(obj, "id", obj)}')

    monkeypatch.setattr(
        gap_service,
        '_gap_analyses_for_policy_name',
        lambda *_args, **_kwargs: [older],
    )
    monkeypatch.setattr(
        'app.services.compliance_history_service.safe_upsert_compliance_history_for_gap',
        lambda *args, **kwargs: None,
    )

    finding = gap_service.GapFinding(
        impact_score='MEDIUM',
        summary='New analysis for v8',
        citations=[],
        analysis_mode='ANTHROPIC',
        analysis_model='claude-test',
    )
    gap_id = gap_service.persist_gap_analysis_result(
        SessionStub(),  # type: ignore[arg-type]
        org_id='org_example',
        publication_id=None,
        business_change_id=None,
        trigger_type='policy_update',
        trigger_id='policy-v8',
        policy_id='policy-v8',
        finding=finding,
        retrieval_metadata={'mode': 'test'},
        force_update=True,
    )

    assert gap_id == 'gap-v7'
    assert older.policy_id == 'policy-v8'
    assert older.trigger_id == 'policy-v8'
    assert older.gap_summary == 'New analysis for v8'
    assert events[0] == 'revision_update:policy-v8'
    assert 'flush' in events
    assert events.index('revision_update:policy-v8') < events.index('flush')
    assert older.retrieval_metadata[gap_service.NEEDS_REVIEW_DECISION_KEY] is True


def test_persist_policy_update_overwrite_unqueues_pending_cco_review(monkeypatch) -> None:
    from datetime import UTC, datetime

    from app.models.core import PolicyRevision

    existing = GapAnalysis(
        id='gap-aml',
        org_id='org_example',
        trigger_type='policy_update',
        trigger_id='policy-1',
        policy_id='policy-1',
        impact_score='HIGH',
        gap_summary='Old summary',
        regulatory_citations=[],
        retrieval_metadata={},
        analysis_mode='ANTHROPIC',
    )
    pending = PolicyRevision(
        id='rev-aml',
        gap_analysis_id='gap-aml',
        policy_id='policy-1',
        org_id='org_example',
        status='PENDING',
        redline_file_url='https://example.com/old-redline.docx',
        review_requested_at=datetime.now(tz=UTC),
    )
    policy_row = Policy(
        id='policy-1',
        org_id='org_example',
        name='AML Test Policy',
        type='INTERNAL',
        status='ACTIVE',
        version=2,
        file_url='https://example.com/aml.docx',
    )

    class PolicyQuery:
        def filter(self, *_args: object, **_kwargs: object):
            return self

        def one_or_none(self):
            return policy_row

    class ExactGapQuery:
        def filter(self, *_args: object, **_kwargs: object):
            return self

        def one_or_none(self):
            return existing

    class RevisionQuery:
        def filter(self, *_args: object, **_kwargs: object):
            return self

        def update(self, values: dict[str, object], synchronize_session: object = False):
            del synchronize_session
            if PolicyRevision.policy_id in values:
                pending.policy_id = values[PolicyRevision.policy_id]  # type: ignore[assignment]
            return 1

        def first(self):
            return pending

        def all(self):
            return [pending]

    class SessionStub:
        def query(self, *entities: object):
            first = entities[0] if entities else None
            if first is PolicyRevision:
                return RevisionQuery()
            if first is Policy:
                return PolicyQuery()
            return ExactGapQuery()

        def flush(self) -> None:
            return None

        def delete(self, obj: object) -> None:
            del obj

    monkeypatch.setattr(
        gap_service,
        '_gap_analyses_for_policy_name',
        lambda *_args, **_kwargs: [existing],
    )
    monkeypatch.setattr(
        'app.services.compliance_history_service.safe_upsert_compliance_history_for_gap',
        lambda *args, **kwargs: None,
    )

    gap_id = gap_service.persist_gap_analysis_result(
        SessionStub(),  # type: ignore[arg-type]
        org_id='org_example',
        publication_id=None,
        business_change_id=None,
        trigger_type='policy_update',
        trigger_id='policy-1',
        policy_id='policy-1',
        finding=gap_service.GapFinding(
            impact_score='HIGH',
            summary='Refreshed AML findings.',
            rule='FINRA Rule 3310',
            fix='Add independent AML testing.',
            citations=[],
            analysis_mode='ANTHROPIC',
            analysis_model='claude-test',
        ),
        retrieval_metadata={'retrieval_mode': 'VECTOR'},
        force_update=True,
    )

    assert gap_id == 'gap-aml'
    assert existing.gap_summary == 'Refreshed AML findings.'
    assert existing.rule == 'FINRA Rule 3310'
    assert existing.fix == 'Add independent AML testing.'
    assert existing.retrieval_metadata[gap_service.NEEDS_REVIEW_DECISION_KEY] is True
    assert pending.review_requested_at is None
    assert pending.redline_file_url is None


def test_clear_gap_needs_review_decision_after_request_review() -> None:
    gap = GapAnalysis(
        id='gap-aml',
        org_id='org_example',
        trigger_type='policy_update',
        trigger_id='policy-1',
        policy_id='policy-1',
        impact_score='HIGH',
        gap_summary='Refreshed',
        retrieval_metadata={
            gap_service.NEEDS_REVIEW_DECISION_KEY: True,
            'retrieval_mode': 'VECTOR',
        },
        analysis_mode='ANTHROPIC',
    )
    gap_service.clear_gap_needs_review_decision(gap)
    assert gap.retrieval_metadata[gap_service.NEEDS_REVIEW_DECISION_KEY] is False
    assert gap.retrieval_metadata['retrieval_mode'] == 'VECTOR'


def test_persist_cleared_needs_review_decision_commits() -> None:
    gap = GapAnalysis(
        id='gap-aml',
        org_id='org_example',
        trigger_type='policy_update',
        trigger_id='policy-1',
        policy_id='policy-1',
        impact_score='HIGH',
        gap_summary='Refreshed',
        retrieval_metadata={gap_service.NEEDS_REVIEW_DECISION_KEY: True},
        analysis_mode='ANTHROPIC',
    )
    commits: list[str] = []

    class SessionStub:
        def commit(self) -> None:
            commits.append('commit')

    gap_service.persist_cleared_needs_review_decision(SessionStub(), gap)  # type: ignore[arg-type]
    assert gap.retrieval_metadata[gap_service.NEEDS_REVIEW_DECISION_KEY] is False
    assert commits == ['commit']


def test_needs_review_decision_prompt_survives_in_review_status() -> None:
    fields = gap_service.needs_review_decision_response_fields(
        {gap_service.NEEDS_REVIEW_DECISION_KEY: True},
        status='IN_REVIEW',
    )
    assert fields['needs_review_decision'] is True
    assert fields['needs_review_decision_message'] == (
        'A new gap analysis has been generated. Request Review again to send '
        'this gap to the CCO Review Queue with a new Redline doc.'
    )


def test_clear_pending_redline_url_for_refreshed_gap() -> None:
    from app.models.core import PolicyRevision

    pending = PolicyRevision(
        id='rev-aml',
        gap_analysis_id='gap-aml',
        policy_id='policy-1',
        org_id='org_example',
        status='PENDING',
        redline_file_url='https://example.com/old-redline.docx',
    )

    class RevisionQuery:
        def filter(self, *_args: object, **_kwargs: object):
            return self

        def all(self):
            return [pending]

    class SessionStub:
        def query(self, *_args: object):
            return RevisionQuery()

    cleared = gap_service.clear_pending_redline_url_for_gap(
        SessionStub(),  # type: ignore[arg-type]
        org_id='org_example',
        gap_analysis_id='gap-aml',
    )
    assert cleared == 1
    assert pending.redline_file_url is None
    assert gap_service.gap_needs_fresh_redline(
        GapAnalysis(
            id='gap-aml',
            org_id='org_example',
            trigger_type='policy_update',
            trigger_id='policy-1',
            policy_id='policy-1',
            impact_score='HIGH',
            gap_summary='Refreshed',
            retrieval_metadata={gap_service.NEEDS_REVIEW_DECISION_KEY: True},
            analysis_mode='ANTHROPIC',
        )
    )


def test_format_gap_rule_fix_comment_includes_structured_fields() -> None:
    from app.services.gap_analysis_service import format_gap_rule_fix_comment

    comment = format_gap_rule_fix_comment(
        rule='FINRA Rule 3110',
        gap='Missing supervisory review cadence.',
        fix='Add quarterly branch inspection procedures.',
    )
    assert 'RULE: FINRA Rule 3110' in comment
    assert 'GAP: Missing supervisory review cadence.' in comment
    assert 'FIX: Add quarterly branch inspection procedures.' in comment


def test_format_gap_rule_fix_comment_preserves_fix_when_gap_is_long() -> None:
    from app.services.gap_analysis_service import format_gap_rule_fix_comment

    long_gap = ' '.join(['The internal policy lacks required supervision controls'] * 40)
    comment = format_gap_rule_fix_comment(
        rule='FINRA Rule 3110(b) requires written supervisory procedures for each business line.',
        gap=long_gap,
        fix='Add a monthly CCO review of exception reports and retain evidence.',
    )
    assert comment.startswith('RULE:')
    assert '\n\nGAP:' in comment
    assert '\n\nFIX:' in comment
    assert 'Add a monthly CCO review' in comment
    assert long_gap in comment
    assert '…' not in comment
    assert len(comment) <= 12_000


def test_format_gap_rule_fix_comment_keeps_full_gap_and_fix() -> None:
    from app.services.gap_analysis_service import format_gap_rule_fix_comment

    gap = (
        "A 2-business-day internal escalation window for potential SAR situations may be "
        "too long given the 30-calendar-day filing deadline and the need for timely "
        "investigation. The policy does not address escalation for situations involving "
        "suspected terrorist financing."
    )
    fix = (
        "Require immediate (same-day) escalation to the CCO for potential SAR situations "
        "involving suspected terrorist financing, imminent harm, or ongoing criminal "
        "activity. For all other cases, escalate within one business day."
    )
    comment = format_gap_rule_fix_comment(
        rule='31 CFR 1023.320 and FINRA Rule 3310',
        gap=gap,
        fix=fix,
    )
    assert gap in comment
    assert fix in comment
    assert '…' not in comment


def test_summarize_text_for_redline_comment_limits_words() -> None:
    from app.services.gap_analysis_service import summarize_text_for_redline_comment

    text = ' '.join(f'word{i}' for i in range(1, 50))
    summarized = summarize_text_for_redline_comment(text, max_words=10)
    assert summarized.endswith('…')
    assert len(summarized.split()) == 10


def test_gap_workflow_status_tracks_review_and_policy_upload() -> None:
    from datetime import UTC, datetime

    from app.services.gap_analysis_service import gap_workflow_status

    assert gap_workflow_status(None) == 'ACTIVE'
    assert gap_workflow_status(
        SimpleNamespace(
            status='PENDING',
            review_requested_at=datetime.now(tz=UTC),
            reuploaded_at=None,
        )
    ) == 'IN_REVIEW'
    assert gap_workflow_status(
        SimpleNamespace(
            status='APPROVED',
            review_requested_at=datetime.now(tz=UTC),
            reuploaded_at=None,
        )
    ) == 'UPLOAD_PENDING'
    assert gap_workflow_status(
        SimpleNamespace(
            status='APPROVED',
            review_requested_at=datetime.now(tz=UTC),
            reuploaded_at=datetime.now(tz=UTC),
        )
    ) == 'ACTIVE'


def test_list_gap_analyses_for_org_hides_gaps_cleared_by_reupload(monkeypatch) -> None:
    from datetime import UTC, datetime

    from app.services.gap_analysis_service import list_gap_analyses_for_org

    analysis = GapAnalysis(
        id='gap-cleared',
        org_id='org_example',
        policy_id='policy-1',
        trigger_type='policy_update',
        trigger_id='policy-1',
        impact_score='HIGH',
        gap_summary='Needs remediation.',
        retrieval_metadata={'sentence_findings': [{'gap': 'Missing SAR escalation', 'impact_score': 'HIGH'}]},
        analysis_mode='ANTHROPIC',
        created_at=datetime.now(tz=UTC),
    )
    policy = Policy(
        id='policy-1',
        org_id='org_example',
        name='FINRA',
        type='uploaded',
        version=2,
        status='ACTIVE',
    )

    class _Query:
        def join(self, *_args, **_kwargs):
            return self

        def outerjoin(self, *_args, **_kwargs):
            return self

        def filter(self, *_args, **_kwargs):
            return self

        def order_by(self, *_args, **_kwargs):
            return self

        def limit(self, *_args, **_kwargs):
            return self

        def all(self):
            return [(analysis, policy, None, None)]

    class _Session:
        def query(self, *_args, **_kwargs):
            return _Query()

    monkeypatch.setattr(
        gap_service,
        'gap_analysis_ids_cleared_by_reupload',
        lambda db, org_id: {'gap-cleared'},
    )
    monkeypatch.setattr(
        gap_service,
        'load_latest_revisions_for_gaps',
        lambda db, org_id, gap_ids: {},
    )

    assert list_gap_analyses_for_org(_Session(), 'org_example') == []


def test_list_gap_analyses_for_org_can_keep_all_publication_policy_links(monkeypatch) -> None:
    from datetime import UTC, datetime

    from app.services.gap_analysis_service import list_gap_analyses_for_org

    policy = Policy(
        id='policy-1',
        org_id='org_example',
        name='AML Policy',
        type='uploaded',
        version=2,
        status='ACTIVE',
    )
    first = GapAnalysis(
        id='gap-pub-1',
        org_id='org_example',
        publication_id='pub-1',
        policy_id='policy-1',
        trigger_type='publication',
        trigger_id='pub-1',
        impact_score='HIGH',
        gap_summary='Gap from first notice.',
        retrieval_metadata={'sentence_findings': [{'gap': 'First', 'impact_score': 'HIGH'}]},
        analysis_mode='ANTHROPIC',
        created_at=datetime.now(tz=UTC),
    )
    second = GapAnalysis(
        id='gap-pub-2',
        org_id='org_example',
        publication_id='pub-2',
        policy_id='policy-1',
        trigger_type='publication',
        trigger_id='pub-2',
        impact_score='MEDIUM',
        gap_summary='Gap from second notice.',
        retrieval_metadata={'sentence_findings': [{'gap': 'Second', 'impact_score': 'MEDIUM'}]},
        analysis_mode='ANTHROPIC',
        created_at=datetime.now(tz=UTC),
    )

    class _Query:
        def join(self, *_args, **_kwargs):
            return self

        def outerjoin(self, *_args, **_kwargs):
            return self

        def filter(self, *_args, **_kwargs):
            return self

        def order_by(self, *_args, **_kwargs):
            return self

        def limit(self, *_args, **_kwargs):
            return self

        def all(self):
            return [(first, policy, None, None), (second, policy, None, None)]

    class _Session:
        def query(self, *_args, **_kwargs):
            return _Query()

    monkeypatch.setattr(
        gap_service,
        'gap_analysis_ids_cleared_by_reupload',
        lambda db, org_id: set(),
    )
    monkeypatch.setattr(
        gap_service,
        'load_latest_revisions_for_gaps',
        lambda db, org_id, gap_ids: {},
    )

    deduped = list_gap_analyses_for_org(
        _Session(),
        'org_example',
        trigger_type='publication',
    )
    all_links = list_gap_analyses_for_org(
        _Session(),
        'org_example',
        trigger_type='publication',
        dedupe_by_policy=False,
    )

    assert len(deduped) == 1
    assert deduped[0].trigger_id == 'pub-1'
    assert [item.trigger_id for item in all_links] == ['pub-1', 'pub-2']


def test_list_gap_analyses_keeps_single_newest_row_per_policy_name(
    monkeypatch,
) -> None:
    from datetime import UTC, datetime, timedelta

    from app.services.gap_analysis_service import list_gap_analyses_for_org

    older_policy = Policy(
        id='policy-v1',
        org_id='org_example',
        name='AML Policy',
        type='uploaded',
        version=1,
        status='SUPERSEDED',
    )
    newer_policy = Policy(
        id='policy-v2',
        org_id='org_example',
        name='AML Policy',
        type='uploaded',
        version=2,
        status='ACTIVE',
    )
    now = datetime.now(tz=UTC)
    older = GapAnalysis(
        id='gap-older',
        org_id='org_example',
        policy_id='policy-v1',
        trigger_type='policy_update',
        trigger_id='policy-v1',
        impact_score='HIGH',
        gap_summary='Older gap.',
        retrieval_metadata={
            'sentence_findings': [{'gap': 'Missing training', 'impact_score': 'HIGH'}]
        },
        analysis_mode='ANTHROPIC',
        created_at=now - timedelta(minutes=10),
    )
    newer = GapAnalysis(
        id='gap-newer',
        org_id='org_example',
        policy_id='policy-v2',
        trigger_type='policy_update',
        trigger_id='policy-v2',
        impact_score='MEDIUM',
        gap_summary='Newer gap.',
        retrieval_metadata={
            'sentence_findings': [{'gap': 'Missing scope', 'impact_score': 'MEDIUM'}]
        },
        analysis_mode='ANTHROPIC',
        created_at=now,
    )

    class _Query:
        def join(self, *_args, **_kwargs):
            return self

        def outerjoin(self, *_args, **_kwargs):
            return self

        def filter(self, *_args, **_kwargs):
            return self

        def order_by(self, *_args, **_kwargs):
            return self

        def limit(self, *_args, **_kwargs):
            return self

        def all(self):
            # Newest first (created_at desc), as list_gap_analyses_for_org requests.
            return [
                (newer, newer_policy, None, None),
                (older, older_policy, None, None),
            ]

    class _Session:
        def query(self, *_args, **_kwargs):
            return _Query()

    monkeypatch.setattr(
        gap_service,
        'gap_analysis_ids_cleared_by_reupload',
        lambda db, org_id: set(),
    )
    monkeypatch.setattr(
        gap_service,
        'load_latest_revisions_for_gaps',
        lambda db, org_id, gap_ids: {},
    )

    rows = list_gap_analyses_for_org(_Session(), 'org_example')

    assert len(rows) == 1
    assert rows[0].id == 'gap-newer'
    assert rows[0].policy_version == 2
    assert rows[0].policy_name == 'AML Policy'


def test_list_gap_analyses_keeps_business_change_and_policy_update_rows(
    monkeypatch,
) -> None:
    from datetime import UTC, datetime, timedelta

    from app.services.gap_analysis_service import list_gap_analyses_for_org

    policy = Policy(
        id='policy-1',
        org_id='org_example',
        name='AML Policy',
        type='uploaded',
        version=2,
        status='ACTIVE',
    )
    now = datetime.now(tz=UTC)
    policy_update = GapAnalysis(
        id='gap-policy-update',
        org_id='org_example',
        policy_id='policy-1',
        trigger_type='policy_update',
        trigger_id='policy-1',
        impact_score='MEDIUM',
        gap_summary='Upload gap.',
        retrieval_metadata={
            'sentence_findings': [{'gap': 'Missing training', 'impact_score': 'MEDIUM'}]
        },
        analysis_mode='ANTHROPIC',
        created_at=now - timedelta(minutes=5),
    )
    business_change = GapAnalysis(
        id='gap-business-change',
        org_id='org_example',
        business_change_id='change-1',
        policy_id='policy-1',
        trigger_type='business_change',
        trigger_id='change-1',
        impact_score='HIGH',
        gap_summary='Business change gap.',
        retrieval_metadata={
            'sentence_findings': [{'gap': 'Missing scope', 'impact_score': 'HIGH'}]
        },
        analysis_mode='ANTHROPIC',
        created_at=now,
    )

    class _Query:
        def join(self, *_args, **_kwargs):
            return self

        def outerjoin(self, *_args, **_kwargs):
            return self

        def filter(self, *_args, **_kwargs):
            return self

        def order_by(self, *_args, **_kwargs):
            return self

        def limit(self, *_args, **_kwargs):
            return self

        def all(self):
            return [
                (business_change, policy, None, None),
                (policy_update, policy, None, None),
            ]

    class _Session:
        def query(self, *_args, **_kwargs):
            return _Query()

    monkeypatch.setattr(
        gap_service,
        'gap_analysis_ids_cleared_by_reupload',
        lambda db, org_id: set(),
    )
    monkeypatch.setattr(
        gap_service,
        'load_latest_revisions_for_gaps',
        lambda db, org_id, gap_ids: {},
    )

    rows = list_gap_analyses_for_org(_Session(), 'org_example')

    assert {row.id for row in rows} == {'gap-business-change', 'gap-policy-update'}
    assert {row.trigger_type for row in rows} == {'business_change', 'policy_update'}


def test_list_gap_analyses_includes_info_business_change_without_findings(
    monkeypatch,
) -> None:
    from datetime import UTC, datetime

    from app.services.gap_analysis_service import list_gap_analyses_for_org

    policy = Policy(
        id='policy-1',
        org_id='org_example',
        name='New Process Policy',
        type='uploaded',
        version=1,
        status='ACTIVE',
    )
    analysis = GapAnalysis(
        id='gap-business-change-info',
        org_id='org_example',
        business_change_id='change-1',
        policy_id='policy-1',
        trigger_type='business_change',
        trigger_id='change-1',
        impact_score='INFO',
        gap_summary='Business change produced a new policy draft.',
        retrieval_metadata={'sentence_findings': []},
        analysis_mode='BUSINESS_CHANGE_NEW_POLICY',
        created_at=datetime.now(tz=UTC),
    )

    class _Query:
        def join(self, *_args, **_kwargs):
            return self

        def outerjoin(self, *_args, **_kwargs):
            return self

        def filter(self, *_args, **_kwargs):
            return self

        def order_by(self, *_args, **_kwargs):
            return self

        def limit(self, *_args, **_kwargs):
            return self

        def all(self):
            return [(analysis, policy, None, None)]

    class _Session:
        def query(self, *_args, **_kwargs):
            return _Query()

    monkeypatch.setattr(
        gap_service,
        'gap_analysis_ids_cleared_by_reupload',
        lambda db, org_id: set(),
    )
    monkeypatch.setattr(
        gap_service,
        'load_latest_revisions_for_gaps',
        lambda db, org_id, gap_ids: {},
    )

    rows = list_gap_analyses_for_org(_Session(), 'org_example')

    assert len(rows) == 1
    assert rows[0].id == 'gap-business-change-info'
    assert rows[0].trigger_type == 'business_change'


def test_list_gap_analyses_includes_info_policy_update_after_refresh(
    monkeypatch,
) -> None:
    from datetime import UTC, datetime

    from app.services.gap_analysis_service import list_gap_analyses_for_org

    policy = Policy(
        id='policy-1',
        org_id='org_example',
        name='AML Test Policy',
        type='uploaded',
        version=3,
        status='ACTIVE',
    )
    analysis = GapAnalysis(
        id='gap-policy-update-info',
        org_id='org_example',
        policy_id='policy-1',
        trigger_type='policy_update',
        trigger_id='policy-1',
        impact_score='INFO',
        gap_summary='Cron refresh found no material gaps.',
        retrieval_metadata={
            'sentence_findings': [],
            gap_service.NEEDS_REVIEW_DECISION_KEY: True,
        },
        analysis_mode='ANTHROPIC',
        created_at=datetime.now(tz=UTC),
    )

    class _Query:
        def join(self, *_args, **_kwargs):
            return self

        def outerjoin(self, *_args, **_kwargs):
            return self

        def filter(self, *_args, **_kwargs):
            return self

        def order_by(self, *_args, **_kwargs):
            return self

        def limit(self, *_args, **_kwargs):
            return self

        def all(self):
            return [(analysis, policy, None, None)]

    class _Session:
        def query(self, *_args, **_kwargs):
            return _Query()

    monkeypatch.setattr(
        gap_service,
        'gap_analysis_ids_cleared_by_reupload',
        lambda db, org_id: set(),
    )
    monkeypatch.setattr(
        gap_service,
        'load_latest_revisions_for_gaps',
        lambda db, org_id, gap_ids: {},
    )

    rows = list_gap_analyses_for_org(_Session(), 'org_example')

    assert len(rows) == 1
    assert rows[0].id == 'gap-policy-update-info'
    assert rows[0].trigger_type == 'policy_update'
    assert rows[0].needs_review_decision is True
    assert rows[0].needs_review_decision_message is not None


def test_cleared_reupload_does_not_hide_overwritten_policy_update() -> None:
    from datetime import UTC, datetime, timedelta

    from app.models.core import PolicyRevision
    from app.services.gap_analysis_service import gap_analysis_ids_cleared_by_reupload

    reuploaded_at = datetime.now(tz=UTC) - timedelta(hours=1)
    created_at = datetime.now(tz=UTC)

    class _Query:
        def join(self, *_args, **_kwargs):
            return self

        def filter(self, *_args, **_kwargs):
            return self

        def all(self):
            return [('gap-1', reuploaded_at, created_at), ('gap-2', reuploaded_at, reuploaded_at)]

    class _Session:
        def query(self, *_args, **_kwargs):
            assert _args[0] == PolicyRevision.gap_analysis_id
            return _Query()

    cleared = gap_analysis_ids_cleared_by_reupload(_Session(), 'org_example')

    assert cleared == {'gap-2'}

