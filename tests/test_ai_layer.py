import asyncio
import os
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.ai import litellm_adapter, network, settings_service
from app.ai.catalog import ModelCatalog
from app.ai.credentials import KEYLESS_PLACEHOLDER, CredentialResolver, normalize_credential
from app.ai.crypto import credential_context, decrypt_secret, encrypt_secret, generate_encryption_key, key_hint
from app.ai.reembedding import STALE_AFTER, ReembedInProgress, ReembedJob, ReembedRunner, visible_job
from app.ai.settings_service import (
    EmbeddingProfileLocked,
    ModelSelection,
    OrgAISettingsService,
    OwnKeyChoice,
    RegAIBillingNotAccepted,
)
from app.ai.store import AISettingsRecord, CredentialStatus
from app.ai.types import (
    AIAuthError,
    AIConfigurationError,
    AICredentialMissing,
    AIEncryptionUnavailable,
    AIProviderError,
    ChatMessage,
    ImagePart,
    ModelMapEntry,
    ResolvedModel,
    TextPart,
)
from app.core.config import Settings

ORG = 'org_1'
PROVIDERS = ('anthropic', 'openai', 'gemini', 'xai', 'ollama', 'hosted_vllm', 'groq', 'bedrock',
             'github_copilot', 'mistral')


def _settings(**overrides) -> Settings:
    values = {
        'ANTHROPIC_API_KEY': 'platform-anthropic',
        'OPENAI_API_KEY': 'platform-openai',
        'AI_CREDENTIAL_ENCRYPTION_KEYS': generate_encryption_key(),
        'AI_MAX_RETRIES': 0,
        'AI_REQUESTS_PER_SECOND': 1000,
    }
    values.update(overrides)
    return Settings(**values)


def _catalog(settings=None) -> ModelCatalog:
    return ModelCatalog(settings or _settings(), provider_ids=lambda: PROVIDERS)


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    def fake_getaddrinfo(host, *_args, **_kwargs):
        address = {'internal.example': '10.0.0.5', 'metadata.example': '169.254.169.254'}.get(host, '93.184.216.34')
        return [(None, None, None, '', (address, 443))]

    monkeypatch.setattr(network.socket, 'getaddrinfo', fake_getaddrinfo)
    monkeypatch.setattr(network, 'get_settings', _settings)


class FakeStore:
    def __init__(self, record=None, credentials=None, embedded_chunks=0):
        self.record = record
        self.credentials = dict(credentials or {})
        self.api_bases: dict[str, str | None] = {}
        self.enabled: dict[str, list[str]] = {}
        self.embedded_chunks = embedded_chunks

    def load_settings(self, org_id):
        return self.record

    def save_settings(self, record, *, actor_id):
        self.record = record
        return record

    def load_credential(self, org_id, provider):
        return self.credentials.get(provider)

    def _status(self, provider, hint=None):
        fields = self.credentials[provider]
        return CredentialStatus(
            provider, hint or key_hint(fields.get('api_key', '')), None, provider_name=provider,
            api_base=self.api_bases.get(provider), enabled_models=tuple(self.enabled.get(provider, ())))

    def save_credential(self, org_id, provider, fields, *, hint, actor_id, provider_name='', api_base=None):
        self.credentials[provider] = fields
        self.api_bases[provider] = api_base
        return self._status(provider, hint)

    def set_model_enabled(self, org_id, provider, model, *, enabled, actor_id):
        if provider not in self.credentials:
            return None
        models = [m for m in self.enabled.get(provider, []) if m != model]
        self.enabled[provider] = sorted([*models, model] if enabled else models)
        return self._status(provider)

    def credential_statuses(self, org_id):
        return [self._status(p) for p in sorted(self.credentials)]

    def ensure_encryption_available(self):
        return None

    def embedded_chunk_count(self, org_id):
        return self.embedded_chunks


class FakeReembedJobs:
    """In-memory ReembedStore: job rows plus one org's chunks and staged vectors."""

    def __init__(self, chunks=None):
        self.jobs: dict[str, ReembedJob] = {}
        self.chunks: dict[str, dict] = dict(chunks or {})
        self.staged: dict[str, list[float]] = {}
        self.on_stage = None

    def latest(self, org_id):
        jobs = [j for j in self.jobs.values() if j.org_id == org_id]
        return max(jobs, key=lambda j: j.started_at) if jobs else None

    def load(self, job_id):
        return self.jobs.get(job_id)

    def create(self, org_id, target, *, total, actor_id):
        if any(j.active for j in self.jobs.values() if j.org_id == org_id):
            raise ReembedInProgress('busy')
        now = datetime.utcnow()
        job = ReembedJob(id=f'job-{len(self.jobs) + 1}', org_id=org_id, status='running', target=target,
                         total_chunks=total, embedded_chunks=0, started_at=now, updated_at=now,
                         created_by=actor_id)
        self.jobs[job.id] = job
        return job

    def update(self, job_id, **fields):
        self.jobs[job_id] = replace(self.jobs[job_id], updated_at=datetime.utcnow(), **fields)

    def clear_staged(self, org_id):
        self.staged.clear()

    def unstaged_chunks(self, org_id, limit):
        ids = sorted(i for i, c in self.chunks.items() if c['embedding'] is not None and i not in self.staged)
        return [(i, self.chunks[i]['content']) for i in ids[:limit]]

    def stage(self, org_id, vectors):
        self.staged.update(vectors)
        if self.on_stage:
            self.on_stage()

    def swap(self, org_id, label):
        if self.unstaged_chunks(org_id, 1):
            return None
        for chunk_id, vector in self.staged.items():
            if chunk_id in self.chunks:
                self.chunks[chunk_id].update(embedding=vector, label=label)
        count = len(self.staged)
        self.staged.clear()
        return count

    def chunks_since(self, org_id, since, *, after_id, limit):
        ids = sorted(i for i, c in self.chunks.items()
                     if c['embedding'] is not None and c['created_at'] >= since and i > after_id)
        return [(i, self.chunks[i]['content']) for i in ids[:limit]]

    def write(self, org_id, vectors, *, label):
        for chunk_id, vector in vectors:
            self.chunks[chunk_id].update(embedding=vector, label=label)


def _record(mode='byok', generation=('openai', 'gpt-5', None),
            embedding=('gemini', 'gemini-embedding-001', None, 3072)):
    return AISettingsRecord(
        auth_org_id=ORG, mode=mode,
        generation_provider=generation[0] if generation else None,
        generation_model=generation[1] if generation else None,
        generation_api_base=generation[2] if generation else None,
        embedding_provider=embedding[0], embedding_model=embedding[1],
        embedding_api_base=embedding[2], embedding_dimensions=embedding[3],
    )


def _resolver(store, settings=None):
    settings = settings or _settings()
    return CredentialResolver(settings=settings, catalog=_catalog(settings), store=store)


def _service(store, jobs=None, dispatched=None):
    catalog = _catalog()
    return OrgAISettingsService(
        store=store, catalog=catalog,
        resolver=CredentialResolver(settings=_settings(), catalog=catalog, store=store),
        reembed_jobs=jobs or FakeReembedJobs(),
        dispatch_reembed=dispatched.append if dispatched is not None else None,
    )


def test_defaults_are_anthropic_generation_and_openai_embeddings() -> None:
    settings = _settings()
    catalog = _catalog(settings)
    assert (catalog.platform_default('redline').provider,
            catalog.platform_default('redline').model) == ('anthropic', settings.resolved_anthropic_redline_model)
    assert catalog.platform_default('analysis').model == settings.resolved_anthropic_gap_analysis_model
    embedding = catalog.platform_default('embedding')
    assert (embedding.provider, embedding.model, embedding.dimensions) == ('openai', 'text-embedding-3-large', 3072)
    assert all(m.is_default for m in (catalog.platform_default('redline'), embedding))


def test_any_litellm_provider_and_custom_model_is_allowed() -> None:
    catalog = _catalog()
    spec = catalog.resolve('mistral', 'open-mixtral-8x22b', 'generation')
    assert (spec.curated, spec.provider, spec.model) == (False, 'mistral', 'open-mixtral-8x22b')
    local = catalog.resolve('ollama', 'qwen2.5:72b', 'generation', api_base='https://llm.customer.com')
    assert local.api_base == 'https://llm.customer.com'
    embed = catalog.resolve('hosted_vllm', 'BAAI/bge-large-en-v1.5', 'embedding',
                            api_base='https://vllm.customer.com/v1', dimensions=1024)
    assert embed.dimensions == 1024
    assert 'github_copilot' not in {p.id for p in catalog.providers()}
    with pytest.raises(AIConfigurationError):
        catalog.resolve('github_copilot', 'gpt-4o', 'generation')
    with pytest.raises(AIConfigurationError):
        catalog.resolve('ollama', 'llama3.3', 'generation')  # needs api_base
    with pytest.raises(AIConfigurationError):
        catalog.resolve('hosted_vllm', 'e5-mistral', 'embedding', api_base='https://x.com', dimensions=4096)
    with pytest.raises(AIConfigurationError):
        catalog.resolve('openai', 'bad model; rm -rf', 'generation')


def test_api_base_blocks_private_networks() -> None:
    assert network.validate_api_base('https://llm.customer.com/v1/') == 'https://llm.customer.com/v1'
    for url in ('http://llm.customer.com', 'https://internal.example', 'https://metadata.example',
                'https://user:pw@llm.customer.com'):
        with pytest.raises(AIConfigurationError):
            network.validate_api_base(url)
    dev = _settings(AI_ALLOW_PRIVATE_API_BASE='true')
    assert network.validate_api_base('http://localhost:11434', dev) == 'http://localhost:11434'


def test_org_without_settings_uses_platform_defaults() -> None:
    resolved = _resolver(FakeStore()).for_org(ORG, 'redline')
    assert (resolved.provider, resolved.source) == ('anthropic', 'platform')
    assert resolved.auth == {'api_key': 'platform-anthropic'}
    assert _resolver(FakeStore()).for_org(None, 'embedding').auth == {'api_key': 'platform-openai'}


def test_byok_never_falls_back_to_platform_keys() -> None:
    store = FakeStore(_record(), credentials={'openai': {'api_key': 'sk-org-openai-1234'}})
    resolved = _resolver(store).for_org(ORG, 'redline')
    assert (resolved.model, resolved.source, resolved.auth['api_key']) == ('gpt-5', 'byok', 'sk-org-openai-1234')
    with pytest.raises(AICredentialMissing):
        _resolver(store).for_org(ORG, 'embedding')


def test_keyless_self_hosted_gets_placeholder_not_env_key() -> None:
    record = _record(generation=('ollama', 'llama3.3', 'https://llm.customer.com'),
                     embedding=('ollama', 'nomic-embed-text', 'https://llm.customer.com', 768))
    resolved = _resolver(FakeStore(record)).for_org(ORG, 'redline')
    assert resolved.auth == {'api_key': KEYLESS_PLACEHOLDER}
    assert resolved.api_base == 'https://llm.customer.com'
    assert 'platform' not in repr(resolved)


def test_cloud_credentials_are_validated() -> None:
    with pytest.raises(AIConfigurationError):
        normalize_credential('bedrock', {'api_key': 'x'})
    fields, hint = normalize_credential('bedrock', {
        'aws_access_key_id': 'AKIAEXAMPLE1234', 'aws_secret_access_key': 's', 'aws_region_name': 'us-east-1'})
    assert hint == '…1234' and set(fields) == {'aws_access_key_id', 'aws_secret_access_key', 'aws_region_name'}
    with pytest.raises(AIConfigurationError):
        normalize_credential('openai', {'api_key': 'sk-x-12345678', 'base_url': 'https://evil'})


def test_encryption_is_bound_to_org_and_provider() -> None:
    settings = _settings()
    token = encrypt_secret('sk-secret-value', context=credential_context(ORG, 'openai'), settings=settings)
    assert 'sk-secret-value' not in token
    assert decrypt_secret(token, context=credential_context(ORG, 'openai'), settings=settings) == 'sk-secret-value'
    with pytest.raises(AIEncryptionUnavailable):
        decrypt_secret(token, context=credential_context('org_2', 'openai'), settings=settings)
    rotated = _settings(AI_CREDENTIAL_ENCRYPTION_KEYS=f'{generate_encryption_key()},'
                        f'{settings.ai_credential_encryption_keys}')
    assert decrypt_secret(token, context=credential_context(ORG, 'openai'), settings=rotated) == 'sk-secret-value'


def _target(kind='generation', provider='openai', model='gpt-5', dimensions=None, api_base=None, supports=False):
    return ResolvedModel(provider=provider, model=model, kind=kind, source='byok', org_id=ORG,
                         auth={'api_key': 'sk-org-key-9876'}, api_base=api_base,
                         dimensions=dimensions, supports_dimensions=supports)


def test_generate_passes_explicit_auth_and_endpoint(monkeypatch) -> None:
    calls = []

    async def acompletion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='drafted'), finish_reason='stop')],
            usage=SimpleNamespace(prompt_tokens=12, completion_tokens=3),
        )

    monkeypatch.setattr(litellm_adapter, '_litellm', lambda: SimpleNamespace(acompletion=acompletion))
    provider = litellm_adapter.LiteLLMGenerationProvider(
        _target(provider='ollama', model='llama3.3', api_base='https://llm.customer.com'), settings=_settings())
    message = ChatMessage('user', [TextPart('read this'), ImagePart('image/png', 'AAAA')])
    result = asyncio.run(provider.generate([message], system='be precise', max_tokens=50))

    assert (result.text, result.input_tokens, result.output_tokens) == ('drafted', 12, 3)
    assert calls[0]['num_retries'] == 0
    assert calls[0]['drop_params'] is True
    assert calls[0]['model'] == 'ollama/llama3.3'
    assert (calls[0]['api_key'], calls[0]['api_base']) == ('sk-org-key-9876', 'https://llm.customer.com')
    assert calls[0]['messages'][1]['content'][1]['image_url']['url'] == 'data:image/png;base64,AAAA'


def test_generate_asks_litellm_to_drop_unsupported_temperature(monkeypatch) -> None:
    calls = []

    async def acompletion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='ok'), finish_reason='stop')],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    monkeypatch.setattr(litellm_adapter, '_litellm', lambda: SimpleNamespace(acompletion=acompletion))
    provider = litellm_adapter.LiteLLMGenerationProvider(
        replace(_target(provider='anthropic', model='claude-mythos-5-1'), source='platform', org_id=None),
        settings=_settings(),
    )
    asyncio.run(provider.generate([ChatMessage('user', 'hi')], max_tokens=16, temperature=0))
    assert calls[0]['temperature'] == 0
    assert calls[0]['drop_params'] is True
    assert calls[0]['model'] == 'anthropic/claude-mythos-5-1'


def test_generate_flattens_list_content_blocks(monkeypatch) -> None:
    async def acompletion(**kwargs):
        del kwargs
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=[
                {'type': 'thinking', 'thinking': 'scratch'},
                {'type': 'text', 'text': '{"edits":[{"anchor_text":"A","replacement_text":"B"}]}'},
            ]), finish_reason='stop')],
            usage=SimpleNamespace(prompt_tokens=4, completion_tokens=8),
        )

    monkeypatch.setattr(litellm_adapter, '_litellm', lambda: SimpleNamespace(acompletion=acompletion))
    provider = litellm_adapter.LiteLLMGenerationProvider(_target(), settings=_settings())
    result = asyncio.run(provider.generate([ChatMessage('user', 'draft')], max_tokens=50))
    assert result.text == '{"edits":[{"anchor_text":"A","replacement_text":"B"}]}'
    assert result.stop_reason == 'stop'
    assert result.truncated is False


def test_generate_treats_anthropic_end_turn_as_complete(monkeypatch) -> None:
    async def acompletion(**kwargs):
        del kwargs
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content='{"findings":[]}'),
                finish_reason='end_turn',
            )],
            usage=SimpleNamespace(prompt_tokens=4, completion_tokens=8),
        )

    monkeypatch.setattr(litellm_adapter, '_litellm', lambda: SimpleNamespace(acompletion=acompletion))
    provider = litellm_adapter.LiteLLMGenerationProvider(_target(), settings=_settings())
    result = asyncio.run(provider.generate([ChatMessage('user', 'draft')], max_tokens=50))
    assert result.stop_reason == 'stop'
    assert result.truncated is False


def test_provider_errors_are_translated_and_redacted(monkeypatch) -> None:
    class AuthenticationError(Exception):
        pass

    async def acompletion(**kwargs):
        raise AuthenticationError('bad key sk-org-key-9876')

    monkeypatch.setattr(litellm_adapter, '_litellm', lambda: SimpleNamespace(acompletion=acompletion))
    provider = litellm_adapter.LiteLLMGenerationProvider(_target(), settings=_settings())
    with pytest.raises(AIAuthError) as excinfo:
        asyncio.run(provider.generate([ChatMessage('user', 'hi')], max_tokens=5))
    assert 'sk-org-key-9876' not in str(excinfo.value)


def test_http_403_is_an_auth_failure() -> None:
    error = type('APIError', (Exception,), {})('Error code: 403 - Forbidden: not authorized')
    translated = litellm_adapter._translate_error(error, _target())
    assert isinstance(translated, AIAuthError)


def test_byok_call_cannot_use_the_platform_env_key(monkeypatch) -> None:
    monkeypatch.setenv('OPENAI_API_KEY', 'platform-openai-key')
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'platform-aws-key')
    seen: dict[str, str | None] = {}

    async def acompletion(**kwargs):
        seen['api_key'] = kwargs.get('api_key')
        seen['env'] = os.environ.get('OPENAI_API_KEY')
        seen['aws'] = os.environ.get('AWS_ACCESS_KEY_ID')
        seen['metadata'] = os.environ.get('AWS_EC2_METADATA_DISABLED')
        raise type('AuthenticationError', (Exception,), {})('401 Unauthorized')

    monkeypatch.setattr(litellm_adapter, '_litellm', lambda: SimpleNamespace(acompletion=acompletion))
    provider = litellm_adapter.LiteLLMGenerationProvider(_target(), settings=_settings())
    with pytest.raises(AIAuthError):
        asyncio.run(provider.generate([ChatMessage('user', 'hi')], max_tokens=5))
    assert seen == {
        'api_key': 'sk-org-key-9876',
        'env': 'sk-org-key-9876',
        'aws': None,
        'metadata': 'true',
    }
    assert os.environ.get('OPENAI_API_KEY') == 'platform-openai-key'
    assert os.environ.get('AWS_ACCESS_KEY_ID') == 'platform-aws-key'


def test_platform_completion_waits_out_byok_env_strip(monkeypatch) -> None:
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'platform-anthropic')
    monkeypatch.setenv('OPENAI_API_KEY', 'platform-openai')
    entered_byok = asyncio.Event()
    release_byok = asyncio.Event()
    seen: dict[str, str | None] = {}

    class FakeLiteLLM:
        api_key = 'stale-global'
        openai_key = None
        anthropic_key = None
        azure_key = None

        async def aembedding(self, **kwargs):
            del kwargs
            entered_byok.set()
            await release_byok.wait()
            return SimpleNamespace(
                data=[{'index': 0, 'embedding': [1.0] * 1536}],
                usage={'total_tokens': 1},
            )

        async def acompletion(self, **kwargs):
            seen['anthropic_env'] = os.environ.get('ANTHROPIC_API_KEY')
            seen['api_key'] = kwargs.get('api_key')
            seen['litellm_key'] = self.api_key
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content='ok'),
                    finish_reason='stop',
                )],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            )

    fake = FakeLiteLLM()
    monkeypatch.setattr(litellm_adapter, '_litellm', lambda: fake)
    embedder = litellm_adapter.LiteLLMEmbeddingProvider(
        replace(
            _target(kind='embedding', provider='openai', model='text-embedding-3-small', dimensions=1536),
            auth={'api_key': 'sk-org-embed'},
        ),
        settings=_settings(),
    )
    generator = litellm_adapter.LiteLLMGenerationProvider(
        replace(
            _target(provider='anthropic', model='claude-opus-4-6'),
            source='platform',
            org_id=None,
            auth={'api_key': 'platform-anthropic'},
        ),
        settings=_settings(),
    )

    async def run() -> None:
        embed_task = asyncio.create_task(embedder.embed(['hello']))
        await entered_byok.wait()
        generate_task = asyncio.create_task(
            generator.generate([ChatMessage('user', 'hi')], max_tokens=5)
        )
        await asyncio.sleep(0)
        release_byok.set()
        await asyncio.gather(generate_task, embed_task)

    asyncio.run(run())
    assert seen == {
        'anthropic_env': 'platform-anthropic',
        'api_key': 'platform-anthropic',
        'litellm_key': 'platform-anthropic',
    }
    assert os.environ.get('ANTHROPIC_API_KEY') == 'platform-anthropic'
    assert os.environ.get('OPENAI_API_KEY') == 'platform-openai'


def test_user_error_message_keeps_auth_for_auth_errors_only() -> None:
    from app.ai.errors import user_error_message

    assert 'authentication failed' in user_error_message(AIAuthError('nope', provider='anthropic'))
    blocked = AIProviderError('anthropic request failed: prompt is too long', provider='anthropic')
    blocked.retryable = False
    assert 'authentication failed' not in user_error_message(blocked)
    assert 'prompt is too long' in user_error_message(blocked)


def _embedding_stub(monkeypatch, width, calls):
    async def aembedding(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            data=[{'index': i, 'embedding': [1.0] * width} for i in range(len(kwargs['input']))],
            usage={'total_tokens': 7},
        )

    monkeypatch.setattr(litellm_adapter, '_litellm', lambda: SimpleNamespace(aembedding=aembedding))


def test_embed_batches_and_requests_dimensions_for_matryoshka_models(monkeypatch) -> None:
    calls: list[dict] = []
    _embedding_stub(monkeypatch, 3072, calls)
    target = _target('embedding', 'openai', 'text-embedding-3-large', 3072, supports=True)
    result = asyncio.run(litellm_adapter.LiteLLMEmbeddingProvider(target, settings=_settings()).embed(
        [f't{i}' for i in range(70)]))
    assert len(result.vectors) == 70 and result.total_tokens == 14
    assert [len(c['input']) for c in calls] == [64, 6] and calls[0]['dimensions'] == 3072


def test_smaller_open_source_embeddings_are_zero_padded(monkeypatch) -> None:
    calls: list[dict] = []
    _embedding_stub(monkeypatch, 768, calls)
    target = _target('embedding', 'ollama', 'nomic-embed-text', 768, api_base='https://llm.customer.com')
    result = asyncio.run(litellm_adapter.LiteLLMEmbeddingProvider(target, settings=_settings()).embed(['a']))
    vector = result.vectors[0]
    assert len(vector) == 3072 and vector[:768] == [1.0] * 768 and set(vector[768:]) == {0.0}
    assert 'dimensions' not in calls[0]

    _embedding_stub(monkeypatch, 1024, calls)
    with pytest.raises(AIProviderError):
        asyncio.run(litellm_adapter.LiteLLMEmbeddingProvider(target, settings=_settings()).embed(['a']))


def test_byok_selection_defaults_and_credential_checks() -> None:
    store = FakeStore(credentials={'openai': {'api_key': 'sk-org-openai-1234'}})
    with pytest.raises(AICredentialMissing):  # default generation is Anthropic, no key stored
        _service(store).update(ORG, mode='byok', generation=None, embedding=None, actor_id='u')
    store.credentials['anthropic'] = {'api_key': 'sk-ant-org-1234'}
    view = _service(store).update(ORG, mode='byok', generation=None, embedding=None, actor_id='u')
    assert (view.generation.provider, view.embedding.provider, view.embedding.dimensions) == (
        'anthropic', 'openai', 3072)


def test_self_hosted_models_need_no_stored_key() -> None:
    view = _service(FakeStore()).update(
        ORG, mode='byok',
        generation=ModelSelection('ollama', 'llama3.3', 'https://llm.customer.com'),
        embedding=ModelSelection('ollama', 'nomic-embed-text', 'https://llm.customer.com', 768),
        actor_id='u',
    )
    assert (view.generation.model, view.embedding.dimensions) == ('llama3.3', 768)


_CREDENTIALS = {'openai': {'api_key': 'sk-a-12345678'}, 'gemini': {'api_key': 'g-a-12345678'}}
_TO_GEMINI = {'mode': 'byok', 'generation': ModelSelection('openai', 'gpt-5'),
              'embedding': ModelSelection('gemini', 'gemini-embedding-001')}


def test_embedding_model_is_free_to_change_until_documents_are_embedded() -> None:
    store = FakeStore(_record(embedding=('openai', 'text-embedding-3-large', None, 3072)), credentials=_CREDENTIALS)
    view = _service(store).update(ORG, actor_id='u', **_TO_GEMINI)
    assert view.embedding.provider == 'gemini' and not view.embedding_locked


def test_embedding_model_is_locked_once_documents_are_embedded() -> None:
    store = FakeStore(credentials=_CREDENTIALS, embedded_chunks=73)
    with pytest.raises(EmbeddingProfileLocked, match='73 document chunks are embedded with openai/text-embedding'):
        _service(store).update(ORG, actor_id='u', **_TO_GEMINI)
    with pytest.raises(AIConfigurationError, match='only be started from Organization Settings'):
        _service(store).update(ORG, actor_id='u', reembed=True, **_TO_GEMINI)
    assert _service(store).view(ORG).embedding_locked
    # Same embedding model on the org's own key: vectors stay comparable, nothing to re-embed.
    view = _service(store).update(ORG, mode='byok', generation=ModelSelection('openai', 'gpt-5'),
                                  embedding=ModelSelection('openai', 'text-embedding-3-large', dimensions=3072),
                                  actor_id='u')
    assert view.mode == 'byok' and view.reembed is None


def test_confirmed_embedding_switch_starts_a_reembed_and_keeps_current_model() -> None:
    store = FakeStore(credentials=_CREDENTIALS, embedded_chunks=73)
    jobs, dispatched = FakeReembedJobs(), []
    view = _service(store, jobs, dispatched).update(ORG, actor_id='u', reembed=True, **_TO_GEMINI)
    assert store.record is None and view.embedding.model == 'text-embedding-3-large'
    assert view.reembed is not None and view.reembed.status == 'running' and view.reembed.total_chunks == 73
    assert dispatched == [view.reembed.id]
    assert view.reembed.target.embedding_provider == 'gemini' and view.reembed.target.generation_model == 'gpt-5'

    with pytest.raises(ReembedInProgress):
        _service(store, jobs, dispatched).update(ORG, actor_id='u', reembed=True, **_TO_GEMINI)
    with pytest.raises(ReembedInProgress):
        _service(store, jobs).delete_credential(ORG, 'gemini', actor_id='u')
    with pytest.raises(AIConfigurationError, match='in use'):
        _service(store, jobs).set_model_enabled(ORG, 'gemini', 'gemini-embedding-001', enabled=False, actor_id='u')


def test_stale_reembed_reads_as_failed_and_finished_jobs_expire() -> None:
    now = datetime.utcnow()
    job = ReembedJob(id='j', org_id=ORG, status='running', target=_record(), total_chunks=1, embedded_chunks=0,
                     started_at=now - STALE_AFTER * 2, updated_at=now - STALE_AFTER * 2)
    stale = visible_job(job, now=now)
    assert stale is not None and stale.status == 'failed' and 'stopped responding' in (stale.error or '')
    done = replace(job, status='completed', updated_at=now, finished_at=now - timedelta(days=2))
    assert visible_job(done, now=now) is None


class _FakeEmbedder:
    model_label = 'gemini/gemini-embedding-001'
    vector_space = 'gemini'

    def __init__(self, fail=False):
        self.fail = fail
        self.calls: list[list[str]] = []

    async def embed(self, texts, *, trace_name='ai.embed'):
        self.calls.append(list(texts))
        if self.fail:
            raise AIAuthError('invalid key', provider='gemini')
        return SimpleNamespace(vectors=[[float(len(t))] for t in texts])


class _Runner(ReembedRunner):
    def __init__(self, embedder, **kwargs):
        super().__init__(sleep=self._no_sleep, batch_size=2, catch_up_delay=0, **kwargs)
        self.embedder = embedder

    @staticmethod
    async def _no_sleep(_seconds):
        return None

    def _provider(self, job):
        return self.embedder


def _chunks(created_at):
    return {f'c{i}': {'content': 'x' * i, 'embedding': [0.0], 'created_at': created_at, 'label': 'old'}
            for i in range(1, 4)}


def test_reembed_runner_swaps_vectors_then_saves_settings_and_catches_up() -> None:
    before = datetime.utcnow() - timedelta(minutes=5)
    store = FakeStore(credentials=_CREDENTIALS, embedded_chunks=3)
    jobs = FakeReembedJobs(_chunks(before))
    service = _service(store, jobs, dispatched := [])
    service.update(ORG, actor_id='u', reembed=True, **_TO_GEMINI)

    def upload_during_job():
        jobs.on_stage = None
        jobs.chunks['c9'] = {'content': 'new upload', 'embedding': [0.0], 'created_at': datetime.utcnow(),
                             'label': 'old'}

    jobs.on_stage = upload_during_job
    embedder = _FakeEmbedder()
    assert asyncio.run(_Runner(embedder, jobs=jobs, settings_store=store).run(dispatched[0])) == 'completed'

    assert store.record is not None and store.record.embedding_provider == 'gemini'
    assert {c['label'] for c in jobs.chunks.values()} == {'gemini/gemini-embedding-001'}
    assert jobs.chunks['c9']['embedding'] == [float(len('new upload'))] and jobs.staged == {}
    job = jobs.jobs[dispatched[0]]
    assert (job.status, job.embedded_chunks) == ('completed', 4) and job.swapped_at and job.finished_at
    # The upload made during the job is embedded again after the swap, once workers use the new model.
    assert embedder.calls[-1] == ['new upload']


def test_reembed_failure_keeps_current_model_and_clears_staged_vectors() -> None:
    store = FakeStore(credentials=_CREDENTIALS, embedded_chunks=3)
    jobs = FakeReembedJobs(_chunks(datetime.utcnow()))
    _service(store, jobs, dispatched := []).update(ORG, actor_id='u', reembed=True, **_TO_GEMINI)
    runner = _Runner(_FakeEmbedder(fail=True), jobs=jobs, settings_store=store)
    assert asyncio.run(runner.run(dispatched[0])) == 'failed'
    job = jobs.jobs[dispatched[0]]
    assert job.status == 'failed' and 'authentication failed' in (job.error or '')
    assert store.record is None and jobs.staged == {}
    assert {c['label'] for c in jobs.chunks.values()} == {'old'}


def test_platform_mode_rejects_custom_models() -> None:
    with pytest.raises(AIConfigurationError):
        _service(FakeStore()).update(ORG, mode='platform', generation=ModelSelection('openai', 'gpt-5'),
                                     embedding=None, actor_id='u', accept_billing=True)
    view = _service(FakeStore()).update(ORG, mode='platform', generation=None, embedding=None, actor_id='u',
                                        accept_billing=True)
    assert view.mode == 'platform' and view.embedding.model == 'text-embedding-3-large'


def test_probe_reports_native_embedding_width(monkeypatch) -> None:
    calls: list[dict] = []
    _embedding_stub(monkeypatch, 1024, calls)
    result = asyncio.run(_service(FakeStore()).probe(
        ORG, 'embedding', ModelSelection('hosted_vllm', 'BAAI/bge-large-en-v1.5', 'https://vllm.customer.com/v1')))
    assert (result.ok, result.dimensions) == (True, 1024)
    assert calls[0]['api_key'] == KEYLESS_PLACEHOLDER


def test_use_reg_ai_default_models_option() -> None:
    credentials = {'anthropic': {'api_key': 'sk-ant-org-1234'}, 'openai': {'api_key': 'sk-org-openai-1234'}}
    store = FakeStore(_record(generation=('anthropic', 'claude-sonnet-4-6', None),
                              embedding=('openai', 'text-embedding-3-large', None, 3072)), credentials=credentials)
    with pytest.raises(RegAIBillingNotAccepted):
        _service(store).use_default_models(ORG, actor_id='u')
    assert store.record.mode == 'byok'
    view = _service(store).use_default_models(ORG, actor_id='u', accept_billing=True)
    assert view.mode == 'platform' and store.record.generation_provider is None
    resolved = _resolver(store).for_org(ORG, 'redline')
    assert (resolved.source, resolved.auth) == ('platform', {'api_key': 'platform-anthropic'})

    custom = FakeStore(_record(embedding=('gemini', 'gemini-embedding-001', None, 3072)),
                       credentials={'gemini': {'api_key': 'g-a-12345678'}}, embedded_chunks=5)
    with pytest.raises(EmbeddingProfileLocked):
        _service(custom).use_default_models(ORG, actor_id='u', accept_billing=True)
    view = _service(custom, dispatched=[]).use_default_models(ORG, actor_id='u', accept_billing=True, reembed=True)
    assert view.mode == 'byok' and view.reembed is not None
    assert (view.reembed.target.mode, view.reembed.target.embedding_model) == ('platform', 'text-embedding-3-large')


def test_reg_ai_default_models_endpoint_requires_billing_acceptance() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    assert client.post('/api/v1/ai/settings/use-default-models', json={}).status_code in {401, 403, 422}
    assert client.put('/api/v1/ai/settings', json={'mode': 'platform'}).status_code in {401, 403, 422}


def test_missing_provider_keys_are_reported() -> None:
    store = FakeStore(_record(generation=('anthropic', 'claude-sonnet-4-6', None),
                              embedding=('openai', 'text-embedding-3-large', None, 3072)),
                      credentials={'anthropic': {'api_key': 'sk-ant-org-1234'}})
    missing = _service(store).view(ORG).missing_credentials
    assert missing == ()
    assert _service(FakeStore(_record(mode='platform', generation=None,
                                      embedding=('openai', 'text-embedding-3-large', None, 3072)))
                    ).view(ORG).missing_credentials == ()
    chosen_own_embedding = _service(FakeStore(
        _record(generation=('anthropic', 'claude-3-haiku-20240307', None),
                embedding=('openai', 'text-embedding-3-small', None, 1536)),
        credentials={'anthropic': {'api_key': 'sk-ant-org-1234'}},
    )).view(ORG).missing_credentials
    assert [(m.provider, m.purpose) for m in chosen_own_embedding] == [('openai', 'embedding')]


def test_registration_key_is_validated_then_stored_and_used() -> None:
    service = _service(store := FakeStore())
    with pytest.raises(AIConfigurationError):
        service.validate_own_key(OwnKeyChoice('openai', 'gpt-4o', 'generation', {}))
    with pytest.raises(AIConfigurationError):
        service.validate_own_key(OwnKeyChoice('github_copilot', 'gpt-4o', 'generation', {'api_key': 'x'}))
    assert store.credentials == {}

    choice = OwnKeyChoice(
        'anthropic', 'claude-sonnet-4-6', 'generation', {'api_key': 'sk-ant-org-9999'}, role='redline')
    view = service.apply_workflow_choices(ORG, [choice], actor_id='creator')
    assert store.credentials == {'anthropic': {'api_key': 'sk-ant-org-9999'}}
    assert store.enabled == {'anthropic': ['claude-sonnet-4-6']}
    assert (view.mode, view.generation.model, view.embedding.provider) == ('byok', 'claude-sonnet-4-6', 'openai')
    assert view.analysis.model == service._catalog.platform_default('analysis').model
    assert view.missing_credentials == ()
    resolved = _resolver(store).for_org(ORG, 'redline')
    assert (resolved.source, resolved.auth) == ('byok', {'api_key': 'sk-ant-org-9999'})
    embedding = _resolver(store).for_org(ORG, 'embedding')
    assert (embedding.source, embedding.auth) == ('platform', {'api_key': 'platform-openai'})


def test_embedding_choice_connects_the_provider_key_without_a_chat_model() -> None:
    service = _service(store := FakeStore())
    choice = OwnKeyChoice(
        'openai', 'text-embedding-ada-002', 'embedding', {'api_key': 'sk-org-openai-1234'},
        dimensions=1536, role='embedding')
    view = service.apply_workflow_choices(ORG, [choice], actor_id='creator')
    assert store.credentials == {'openai': {'api_key': 'sk-org-openai-1234'}}
    assert store.record is not None
    assert store.record.generation_provider is None
    assert store.record.analysis_provider is None
    assert store.record.embedding_model == 'text-embedding-ada-002'
    assert view.generation.model == service._catalog.platform_default('redline').model
    assert view.analysis.model == service._catalog.platform_default('analysis').model
    assert view.embedding.model == 'text-embedding-ada-002'
    assert view.missing_credentials == ()
    chat = _resolver(store).for_org(ORG, 'redline')
    assert chat.source == 'platform'
    embedding = _resolver(store).for_org(ORG, 'embedding')
    assert (embedding.source, embedding.model, embedding.auth['api_key']) == (
        'byok', 'text-embedding-ada-002', 'sk-org-openai-1234')


def test_analysis_and_redlines_can_use_different_models() -> None:
    service = _service(store := FakeStore())
    view = service.apply_workflow_choices(ORG, [
        OwnKeyChoice('openai', 'gpt-4o', 'generation', {'api_key': 'sk-org-openai-1234'}, role='redline'),
        OwnKeyChoice('anthropic', 'claude-opus-4-6', 'generation', {'api_key': 'sk-ant-org-9999'}, role='analysis'),
    ], actor_id='creator')
    assert (view.generation.model, view.analysis.model) == ('gpt-4o', 'claude-opus-4-6')
    assert view.missing_credentials == ()
    redline = _resolver(store).for_org(ORG, 'redline')
    analysis = _resolver(store).for_org(ORG, 'analysis')
    assert (redline.model, redline.source, redline.auth['api_key']) == ('gpt-4o', 'byok', 'sk-org-openai-1234')
    assert (analysis.model, analysis.source, analysis.auth['api_key']) == ('claude-opus-4-6', 'byok', 'sk-ant-org-9999')

    redline_only = _service(store := FakeStore())
    view = redline_only.apply_workflow_choices(ORG, [
        OwnKeyChoice('openai', 'gpt-4o', 'generation', {'api_key': 'sk-org-openai-1234'}, role='redline'),
    ], actor_id='creator')
    assert view.generation.model == 'gpt-4o'
    assert view.analysis.model == _service(store)._catalog.platform_default('analysis').model
    assert view.missing_credentials == ()
    leftover = _resolver(store).for_org(ORG, 'analysis')
    assert leftover.source == 'platform' and leftover.model == view.analysis.model


def test_usage_is_attributed_for_future_billing(monkeypatch) -> None:
    from app.ai import metering

    events = []
    monkeypatch.setattr(metering.logger, 'info', lambda event, **fields: events.append((event, fields)))
    _embedding_stub(monkeypatch, 3072, [])
    platform = _resolver(FakeStore()).for_org(ORG, 'embedding')
    asyncio.run(litellm_adapter.LiteLLMEmbeddingProvider(platform, settings=_settings()).embed(['a', 'b']))
    byok = _target('embedding', 'openai', 'text-embedding-3-large', 3072, supports=True)
    asyncio.run(litellm_adapter.LiteLLMEmbeddingProvider(byok, settings=_settings()).embed(['a']))
    assert [(e, f['billed_to'], f['org_id'], f['items']) for e, f in events] == [
        ('ai_usage', 'reg_ai', ORG, 2), ('ai_usage', 'customer', ORG, 1)]


def test_catalog_lists_default_option_first() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    body = TestClient(app).get('/api/v1/ai/catalog').json()
    assert body['modes'][0] == {
        'id': 'platform', 'label': 'Use Reg-AI default models',
        'description': body['modes'][0]['description'], 'billed_to': 'reg_ai', 'is_default': True,
    }
    assert body['modes'][1]['billed_to'] == 'customer'
    assert body['default_generation']['provider'] == 'anthropic'
    assert body['default_embedding'] == {
        'provider': 'openai', 'model': 'text-embedding-3-large', 'api_base': None, 'dimensions': 3072}


def _model_entries():
    return (
        ModelMapEntry('openai', 'gpt-4o', 'generation', vision=True),
        ModelMapEntry('openai', 'text-embedding-3-small', 'embedding', dimensions=1536),
        ModelMapEntry('openai', 'huge-embedding', 'embedding', dimensions=8192),
        ModelMapEntry('openai', 'unknown-width-embedding', 'embedding'),
        ModelMapEntry('mistral', 'mistral-small-latest', 'generation'),
        ModelMapEntry('github_copilot', 'gpt-4o', 'generation'),
        ModelMapEntry('not_a_provider', 'x', 'generation'),
    )


def test_model_list_covers_litellm_models_with_defaults_first() -> None:
    catalog = ModelCatalog(_settings(), provider_ids=lambda: PROVIDERS, model_entries=_model_entries)
    models = catalog.available_models()
    assert models[0].is_default
    keys = {(m.provider, m.model, m.kind) for m in models}
    assert {('openai', 'gpt-4o', 'generation'), ('openai', 'text-embedding-3-small', 'embedding'),
            ('mistral', 'mistral-small-latest', 'generation')} <= keys
    # Blocked or unknown providers and unusable embedding widths are left out.
    assert not {m.model for m in models} & {'huge-embedding', 'unknown-width-embedding', 'x'}
    assert all(m.provider != 'github_copilot' for m in models)

    page = catalog.search_models('OpenAI embedding', kind='embedding', limit=1)
    assert page.total == 2 and len(page.items) == 1
    assert [m.model for m in catalog.search_models('mistral small').items] == ['mistral-small-latest']


def test_one_provider_key_serves_many_models() -> None:
    store = FakeStore()
    service = _service(store)
    with pytest.raises(AICredentialMissing):
        service.set_model_enabled(ORG, 'openai', 'gpt-4o', enabled=True, actor_id='u')
    saved = service.save_credential(ORG, 'openai', {'api_key': 'sk-org-openai-5678'}, actor_id='u')
    assert saved.key_hint.endswith('5678') and 'sk-org' not in saved.key_hint
    service.set_model_enabled(ORG, 'openai', 'gpt-4o', enabled=True, actor_id='u')
    status = service.set_model_enabled(ORG, 'openai', 'gpt-5', enabled=True, actor_id='u')
    assert status.enabled_models == ('gpt-4o', 'gpt-5')
    assert list(store.credentials) == ['openai']
    for model in ('gpt-4o', 'gpt-5'):
        spec = _catalog().resolve('openai', model, 'generation')
        bound = _resolver(store).bind(spec, org_id=ORG, byok=True)
        assert bound.auth == {'api_key': 'sk-org-openai-5678'}


def test_model_in_use_cannot_be_turned_off() -> None:
    store = FakeStore(_record(generation=('openai', 'gpt-5', None),
                              embedding=('openai', 'text-embedding-3-large', None, 3072)),
                      credentials={'openai': {'api_key': 'sk-org-openai-1234'}})
    service = _service(store)
    service.set_model_enabled(ORG, 'openai', 'gpt-5', enabled=True, actor_id='u')
    with pytest.raises(AIConfigurationError):
        service.set_model_enabled(ORG, 'openai', 'gpt-5', enabled=False, actor_id='u')


def test_self_hosted_provider_key_is_optional_and_endpoint_is_reused() -> None:
    store = FakeStore()
    service = _service(store)
    with pytest.raises(AIConfigurationError):
        service.save_credential(ORG, 'ollama', {}, actor_id='u')
    saved = service.save_credential(ORG, 'ollama', {}, actor_id='u', api_base='https://llm.customer.com')
    assert (saved.key_hint, saved.api_base) == ('no key', 'https://llm.customer.com')
    with pytest.raises(AIConfigurationError):
        service.save_credential(ORG, 'openai', {}, actor_id='u')
    view = service.update(
        ORG, mode='byok', generation=ModelSelection('ollama', 'llama3.3'),
        embedding=ModelSelection('ollama', 'nomic-embed-text', dimensions=768), actor_id='u')
    assert view.generation.api_base == 'https://llm.customer.com'
    assert view.embedding.api_base == 'https://llm.customer.com'


def test_provider_keys_live_in_the_org_tenant_schema() -> None:
    from app.models.core import AIProviderCredential
    from app.services.schema_tenant_provisioner import DATA_PLANE_TABLES
    from app.services.tenant_migrations import TENANT_MIGRATION_FILES

    assert AIProviderCredential.__tablename__ in DATA_PLANE_TABLES
    assert 'tenant_phase_45_ai_provider_credentials.sql' in TENANT_MIGRATION_FILES
    columns = set(AIProviderCredential.__table__.columns.keys())
    assert {'org_id', 'provider', 'provider_name', 'auth_key_ciphertext', 'key_hint', 'enabled_models'} <= columns
    assert not columns & {'api_key', 'auth_key', 'secret'}
    migration = (Path(__file__).resolve().parents[1] / 'supabase' / 'migrations'
                 / 'phase_45_organization_ai_settings.sql').read_text()
    assert 'organization_ai_credentials' not in migration
    assert "apply_phase_45_ai_provider_credentials(tenant_schema)" in migration


def test_store_reads_provider_keys_through_the_tenant_session(monkeypatch) -> None:
    from contextlib import contextmanager

    from app.ai import store as store_mod

    scopes: list[str | None] = []

    class Query:
        def filter(self, *_args):
            return self

        def one_or_none(self):
            return None

    class Session:
        def query(self, *_args):
            return Query()

    @contextmanager
    def fake_scope(*, commit=True, org_id=None):
        scopes.append(org_id)
        yield Session()

    monkeypatch.setattr(store_mod.db_session, 'SessionLocal', object())
    monkeypatch.setattr(store_mod, 'session_scope', fake_scope)

    @contextmanager
    def empty_platform_scope(**_kwargs):
        class Query:
            def filter(self, *_args):
                return self

            def all(self):
                return []

        class Session:
            def query(self, *_args):
                return Query()

        yield Session()

    monkeypatch.setattr(store_mod, 'platform_session_scope', empty_platform_scope)
    assert store_mod.AISettingsStore().load_credential(ORG, 'openai') is None
    assert scopes == [ORG]


def test_schema_isolated_org_writes_keys_to_public_until_tenant_exists(monkeypatch) -> None:
    from contextlib import contextmanager

    from app.ai import store as store_mod

    saved: list[str] = []

    class Query:
        def filter(self, *_args):
            return self

        def one_or_none(self):
            return None

    class Session:
        def query(self, *_args):
            return Query()

        def add(self, row):
            saved.append(row.provider)

        def flush(self):
            return None

    @contextmanager
    def fake_platform_scope(*, commit=True):
        yield Session()

    monkeypatch.setattr(store_mod.db_session, 'SessionLocal', object())
    monkeypatch.setattr(store_mod, '_tenant_schema_ready', lambda *_a, **_k: False)
    monkeypatch.setattr(store_mod, 'platform_session_scope', fake_platform_scope)
    monkeypatch.setattr(store_mod, 'encrypt_secret', lambda *_a, **_k: 'cipher')
    status = store_mod.AISettingsStore().save_credential(
        ORG, 'anthropic', {'api_key': 'sk-ant'}, hint='••••', actor_id='u', provider_name='Anthropic')
    assert saved == ['anthropic'] and status.provider == 'anthropic'


def test_public_anthropic_key_is_merged_when_tenant_already_has_openai(monkeypatch) -> None:
    from contextlib import contextmanager
    from types import SimpleNamespace

    from app.ai import store as store_mod

    openai_row = SimpleNamespace(
        id='cred-openai', org_id=ORG, provider='openai', provider_name='OpenAI',
        auth_key_ciphertext='cipher-o', key_hint='••••1234', api_base=None,
        enabled_models=['text-embedding-3-small'], updated_by='u',
        created_at=None, updated_at=None,
    )
    public_row = SimpleNamespace(
        id='cred-ant', org_id=ORG, provider='anthropic', provider_name='Anthropic',
        auth_key_ciphertext='cipher-a', key_hint='••••9999', api_base=None,
        enabled_models=['claude-sonnet-4-6'], updated_by='u',
        created_at=None, updated_at=None,
    )
    tenant_rows: list[object] = [openai_row]

    class TenantQuery:
        def filter(self, *_args):
            return self

        def order_by(self, *_args):
            return self

        def all(self):
            return list(tenant_rows)

        def one_or_none(self):
            return None

    class PublicQuery:
        def filter(self, *_args):
            return self

        def all(self):
            return [public_row]

        def delete(self, synchronize_session=False):
            return 1

    class TenantSession:
        """Matches production sessions: autoflush is off, so a query misses an add until flush."""

        def __init__(self) -> None:
            self.pending: list[object] = []

        def query(self, *_args):
            return TenantQuery()

        def add(self, row):
            self.pending.append(row)

        def flush(self):
            tenant_rows.extend(self.pending)
            self.pending.clear()

    class PublicSession:
        def query(self, *_args):
            return PublicQuery()

    @contextmanager
    def fake_tenant_scope(*, commit=True, org_id=None):
        yield TenantSession()

    @contextmanager
    def fake_platform_scope(*, commit=True):
        yield PublicSession()

    monkeypatch.setattr(store_mod.db_session, 'SessionLocal', object())
    monkeypatch.setattr(store_mod.db_session, 'resolve_schema_for_org', lambda *_a, **_k: 'acme_tenant')
    monkeypatch.setattr(store_mod, 'session_scope', fake_tenant_scope)
    monkeypatch.setattr(store_mod, 'platform_session_scope', fake_platform_scope)
    statuses = store_mod.AISettingsStore().credential_statuses(ORG)
    assert {status.provider for status in statuses} == {'anthropic', 'openai'}


def test_model_list_endpoint_searches_litellm_map() -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    body = TestClient(app).get('/api/v1/ai/models', params={'q': 'gpt-4o', 'kind': 'generation',
                                                            'limit': 5}).json()
    assert body['total'] >= 1 and len(body['items']) <= 5
    first = body['items'][0]
    assert first['provider_name'] and 'gpt-4o' in first['model']
    assert first['credential_fields']


def test_providers_group_their_models_alphabetically() -> None:
    catalog = ModelCatalog(_settings(), provider_ids=lambda: PROVIDERS, model_entries=lambda: [
        ModelMapEntry('xai', 'grok-3', 'generation'),
        ModelMapEntry('openai', 'gpt-4o', 'generation'),
        ModelMapEntry('openai', 'text-embedding-3-small', 'embedding', dimensions=1536),
        ModelMapEntry('mistral', 'mistral-small', 'generation'),
    ])
    summaries = catalog.provider_summaries()
    labels = [s.provider.label for s in summaries]
    assert labels == sorted(labels, key=str.casefold) and len(labels) == len(set(labels))
    openai = next(s for s in summaries if s.provider.id == 'openai')
    assert (openai.generation_models, openai.embedding_models) == (2, 2)
    assert [s.provider.id for s in catalog.provider_summaries('grok')] == ['xai']
    assert all(s.embedding_models and not s.generation_models
               for s in catalog.provider_summaries(kind='embedding'))


def _completion_stub(monkeypatch, calls, error=None):
    async def acompletion(**kwargs):
        calls.append(kwargs)
        if error is not None:
            raise error
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='OK'), finish_reason='stop')],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=1),
        )

    monkeypatch.setattr(settings_service, 'get_settings', _settings)
    monkeypatch.setattr(litellm_adapter, '_litellm', lambda: SimpleNamespace(acompletion=acompletion))


def _models_list_stub(monkeypatch, calls, *, status=200, error=None):
    class FakeResponse:
        status_code = status

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers=None):
            calls.append({'url': url, 'headers': headers})
            if error is not None:
                raise error
            return FakeResponse()

    monkeypatch.setattr(settings_service, 'get_settings', _settings)
    monkeypatch.setattr(litellm_adapter.httpx, 'AsyncClient', FakeClient)


def test_new_key_is_tested_with_the_model_being_turned_on(monkeypatch) -> None:
    calls: list[dict] = []
    _models_list_stub(monkeypatch, calls)
    store = FakeStore()
    asyncio.run(_service(store).verify_credential(
        ORG, 'openai', {'api_key': ' sk-new-12345678 '}, model='gpt-4o', kind='generation'))
    assert calls[0]['url'] == 'https://api.openai.com/v1/models'
    assert calls[0]['headers']['Authorization'] == 'Bearer sk-new-12345678'
    assert store.credentials == {}


def test_anthropic_key_check_uses_models_list_not_a_completion(monkeypatch) -> None:
    calls: list[dict] = []
    _models_list_stub(monkeypatch, calls)
    asyncio.run(_service(FakeStore()).verify_credential(
        None, 'anthropic', {'api_key': 'sk-ant-valid-key'}, model='claude-3-7-sonnet-20250219',
        kind='generation'))
    assert calls[0]['url'] == 'https://api.anthropic.com/v1/models'
    assert calls[0]['headers']['x-api-key'] == 'sk-ant-valid-key'


@pytest.mark.parametrize(('status', 'message'), [
    (401, 'rejected this API key: it is invalid or wrong'),
    (403, 'rejected this API key: it is invalid or wrong'),
    (429, 'rate-limiting this key'),
    (500, 'did not respond correctly'),
])
def test_key_the_provider_does_not_accept_is_reported(monkeypatch, status, message) -> None:
    _models_list_stub(monkeypatch, [], status=status)
    with pytest.raises(settings_service.AICredentialInvalid) as excinfo:
        asyncio.run(_service(FakeStore()).verify_credential(
            ORG, 'openai', {'api_key': 'sk-bad-12345678'}, model='gpt-4o', kind='generation'))
    text = str(excinfo.value)
    assert message in text and 'The key was not saved.' in text and text.startswith('OpenAI')
    assert 'sk-bad-12345678' not in text


def test_key_check_picks_a_model_when_none_is_given() -> None:
    store = FakeStore(credentials={'openai': {'api_key': 'sk-org-12345678'}})
    store.enabled['openai'] = ['text-embedding-3-large']
    spec = _service(store)._key_check_model(ORG, 'openai', None, None, None)
    assert (spec.model, spec.kind) == ('text-embedding-3-large', 'embedding')
    spec = _service(FakeStore())._key_check_model(None, 'gemini', None, None, None)
    assert (spec.model, spec.kind) == ('gemini-2.5-pro', 'generation')


def test_credentials_endpoint_saves_only_keys_the_provider_accepts(monkeypatch) -> None:
    from fastapi import HTTPException

    from app.api.v1.endpoints import ai as ai_endpoints
    from app.schemas.ai import AICredentialUpdateRequest

    store = FakeStore()
    monkeypatch.setattr(ai_endpoints, 'OrgAISettingsService', lambda: _service(store))
    user = SimpleNamespace(org_id=ORG, user_id='u')
    request = AICredentialUpdateRequest(fields={'api_key': 'sk-bad-12345678'}, model='gpt-4o', kind='generation')

    _models_list_stub(monkeypatch, [], status=401)
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(ai_endpoints.put_ai_credential('openai', request, user=user))  # type: ignore[arg-type]
    assert excinfo.value.status_code == 422 and 'invalid or wrong' in str(excinfo.value.detail)
    assert store.credentials == {}

    _models_list_stub(monkeypatch, [])
    saved = asyncio.run(ai_endpoints.put_ai_credential('openai', request, user=user))  # type: ignore[arg-type]
    assert saved.provider == 'openai' and store.credentials['openai'] == {'api_key': 'sk-bad-12345678'}


def test_registration_key_is_tested_before_the_org_exists(monkeypatch) -> None:
    from fastapi import HTTPException

    from app.schemas.organizations import OrganizationCreateRequest
    from app.services import organization_invite_service as invite_service

    monkeypatch.setattr(invite_service, 'OrgAISettingsService', lambda: _service(FakeStore()))
    setup = OrganizationCreateRequest(name='Acme', ai_setup={
        'option': 'own_key',
        'workflows': [{
            'role': 'redline', 'provider': 'openai', 'model': 'gpt-4o',
            'fields': {'api_key': 'sk-bad-12345678'},
        }],
    }).ai_setup
    calls: list[dict] = []
    _models_list_stub(monkeypatch, calls, status=401)
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(invite_service.verify_organization_ai_key(setup))
    assert excinfo.value.status_code == 422 and 'invalid or wrong' in str(excinfo.value.detail)
    assert calls[0]['headers']['Authorization'] == 'Bearer sk-bad-12345678'

    _models_list_stub(monkeypatch, [])
    asyncio.run(invite_service.verify_organization_ai_key(setup))


def test_verify_endpoint_rejects_invalid_key_and_does_not_save(monkeypatch) -> None:
    from fastapi import HTTPException

    from app.api.v1.endpoints import ai as ai_endpoints
    from app.schemas.ai import AICredentialVerifyRequest

    store = FakeStore()
    monkeypatch.setattr(ai_endpoints, 'OrgAISettingsService', lambda: _service(store))
    user = SimpleNamespace(org_id='platform', user_id='u')
    request = AICredentialVerifyRequest(
        provider='openai', model='gpt-4o', kind='generation', fields={'api_key': 'sk-bad-12345678'})

    _models_list_stub(monkeypatch, [], status=401)
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(ai_endpoints.post_ai_credential_verify(request, _user=user))  # type: ignore[arg-type]
    assert excinfo.value.status_code == 422 and 'invalid or wrong' in str(excinfo.value.detail)
    assert store.credentials == {}

    calls: list[dict] = []
    _models_list_stub(monkeypatch, calls)
    result = asyncio.run(ai_endpoints.post_ai_credential_verify(request, _user=user))  # type: ignore[arg-type]
    assert result.ok is True and store.credentials == {}
    assert calls[0]['headers']['Authorization'] == 'Bearer sk-bad-12345678'


def test_registration_key_is_staged_to_an_encrypted_file(tmp_path, monkeypatch) -> None:
    from app.ai import key_staging

    settings = _settings(AI_KEY_STAGING_DIR=str(tmp_path))
    monkeypatch.setattr(key_staging, 'get_settings', lambda: settings)
    monkeypatch.setattr('app.ai.crypto.get_settings', lambda: settings)
    monkeypatch.setattr(key_staging, 'db_backed', lambda: False)
    choice = OwnKeyChoice(
        'anthropic', 'claude-sonnet-4-6', 'generation', {'api_key': 'sk-ant-org-9999'}, role='redline')
    path = key_staging.stage_workflows('user-1', [choice])
    assert path.is_file()
    assert 'sk-ant-org-9999' not in path.read_text()
    loaded = key_staging.load_staged_workflows('user-1')
    assert len(loaded) == 1
    assert loaded[0].provider == 'anthropic'
    assert loaded[0].role == 'redline'
    assert loaded[0].fields == {'api_key': 'sk-ant-org-9999'}
    key_staging.discard_staged_key('user-1')
    assert key_staging.load_staged_workflows('user-1') == []


def test_registration_key_is_staged_in_the_public_database(tmp_path, monkeypatch) -> None:
    from contextlib import contextmanager

    from app.ai import key_staging

    settings = _settings(AI_KEY_STAGING_DIR=str(tmp_path))
    monkeypatch.setattr(key_staging, 'get_settings', lambda: settings)
    monkeypatch.setattr('app.ai.crypto.get_settings', lambda: settings)
    monkeypatch.setattr(key_staging, 'db_backed', lambda: True)
    held: list[object] = []

    class Session:
        def query(self, *_args):
            return self

        def filter(self, *_args):
            return self

        def one_or_none(self):
            return held[0] if held else None

        def add(self, row):
            held[:] = [row]

        def delete(self, synchronize_session=False):
            held.clear()
            return 1

        def flush(self):
            return None

    @contextmanager
    def fake_platform_scope(*, commit=True):
        yield Session()

    monkeypatch.setattr('app.db.session.platform_session_scope', fake_platform_scope)
    choice = OwnKeyChoice(
        'anthropic', 'claude-sonnet-4-6', 'generation', {'api_key': 'sk-ant-org-9999'}, role='redline')
    path = key_staging.stage_workflows('user-1', [choice])
    path.unlink()
    assert held and 'sk-ant-org-9999' not in held[0].auth_key_ciphertext
    key_staging.bind_staged_org('user-1', 'org_acme')
    loaded = key_staging.load_staged_workflows('user-1')
    assert len(loaded) == 1 and loaded[0].fields == {'api_key': 'sk-ant-org-9999'}
    assert key_staging.staged_dest_org_id('user-1') == 'org_acme'
    key_staging.discard_staged_key('user-1')
    assert key_staging.load_staged_workflows('user-1') == []


def test_settings_claim_moves_the_registration_key_into_the_org(monkeypatch) -> None:
    store = FakeStore()
    service = _service(store)
    choice = OwnKeyChoice(
        'openai', 'gpt-4o', 'generation', {'api_key': 'sk-org-openai-5678'}, role='redline')
    monkeypatch.setattr('app.ai.key_staging.load_staged_workflows', lambda _user_id: [choice])
    monkeypatch.setattr('app.ai.key_staging.staged_dest_org_id', lambda _user_id: ORG)
    discarded: list[bool] = []
    monkeypatch.setattr(
        'app.ai.key_staging.discard_staged_key', lambda _user_id: discarded.append(True))
    assert service.claim_staged_registration_key(ORG, 'user-1') is True
    assert store.credentials['openai'] == {'api_key': 'sk-org-openai-5678'}
    assert store.record is not None and store.record.mode == 'byok'
    assert store.record.generation_model == 'gpt-4o'
    assert discarded


def test_claim_still_saves_models_when_the_key_was_already_stored(monkeypatch) -> None:
    store = FakeStore()
    store.credentials['anthropic'] = {'api_key': 'sk-ant-org-9999'}
    store.credentials['openai'] = {'api_key': 'sk-org-openai-1234'}
    service = _service(store)
    choices = [
        OwnKeyChoice(
            'anthropic', 'claude-4-sonnet-20250514', 'generation',
            {'api_key': 'sk-ant-org-9999'}, role='analysis'),
        OwnKeyChoice(
            'anthropic', 'claude-3-haiku-20240307', 'generation',
            {'api_key': 'sk-ant-org-9999'}, role='redline'),
        OwnKeyChoice(
            'openai', 'text-embedding-ada-002', 'embedding',
            {'api_key': 'sk-org-openai-1234'}, dimensions=1536, role='embedding'),
    ]
    monkeypatch.setattr('app.ai.key_staging.load_staged_workflows', lambda _user_id: choices)
    monkeypatch.setattr('app.ai.key_staging.staged_dest_org_id', lambda _user_id: ORG)
    discarded: list[bool] = []
    monkeypatch.setattr(
        'app.ai.key_staging.discard_staged_key', lambda _user_id: discarded.append(True))
    assert service.claim_staged_registration_key(ORG, 'user-1') is True
    assert store.record is not None
    assert store.record.mode == 'byok'
    assert store.record.analysis_model == 'claude-4-sonnet-20250514'
    assert store.record.generation_model == 'claude-3-haiku-20240307'
    assert store.record.embedding_model == 'text-embedding-ada-002'
    assert discarded


def test_only_the_adapter_imports_litellm() -> None:
    app_dir = Path(__file__).resolve().parents[1] / 'app'
    offenders = [
        str(path.relative_to(app_dir)) for path in app_dir.rglob('*.py')
        if path.name != 'litellm_adapter.py'
        and any(line.lstrip().startswith(('import litellm', 'from litellm')) for line in path.read_text().splitlines())
    ]
    assert offenders == []
