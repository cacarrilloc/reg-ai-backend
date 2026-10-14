from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from app.core.config import (
    PUBLIC_FRONTEND_URL_DEV,
    PUBLIC_FRONTEND_URL_PRODUCTION,
    PUBLIC_FRONTEND_URL_STAGING,
    Settings,
)
from app.core.security import PUBLIC_PATHS
from app.schemas.newsletter import NewsletterSubscribeRequest
from app.services.newsletter_html import (
    build_newsletter_invite_email_html,
    build_summary_html,
    build_weekly_newsletter_html,
    classify_publication_kind,
)
from app.services.newsletter_service import (
    NEWSLETTER_LIST_ORG,
    NEWSLETTER_LIST_PUBLIC,
    create_newsletter_invite_token,
    invite_newsletter_subscriber,
    parse_newsletter_invite_token,
    send_weekly_newsletter,
    subscribe_newsletter,
)


def test_public_newsletter_publications_use_shared_feed(monkeypatch) -> None:
    from app.api.v1.endpoints import newsletter as newsletter_endpoint

    captured: dict[str, object] = {}

    def fake_list(*, sources: object, limit: int) -> list[object]:
        captured['sources'] = sources
        captured['limit'] = limit
        return []

    monkeypatch.setattr(newsletter_endpoint, 'list_shared_feed_publications', fake_list)
    assert newsletter_endpoint.get_newsletter_publications(limit=50) == []
    assert captured['limit'] == 50
    assert captured['sources'] == newsletter_endpoint.NEWSLETTER_SOURCES


def test_weekly_brief_backfills_missing_finra_and_cftc(monkeypatch) -> None:
    from types import SimpleNamespace

    from app.services import newsletter_service as service

    sec = SimpleNamespace(
        id='sec-1',
        source='SEC',
        title='SEC item',
        url='https://www.sec.gov/1',
        published_at=datetime(2026, 3, 20),
        created_at=datetime(2026, 3, 20),
    )
    finra = SimpleNamespace(
        id='finra-1',
        source='FINRA',
        title='FINRA notice',
        url='https://www.finra.org/1',
        published_at=datetime(2026, 2, 1),
        created_at=datetime(2026, 2, 1),
    )
    cftc = SimpleNamespace(
        id='cftc-1',
        source='CFTC',
        title='CFTC order',
        url='https://www.cftc.gov/1',
        published_at=datetime(2026, 1, 15),
        created_at=datetime(2026, 1, 15),
    )

    class Query:
        def filter(self, *_args: object) -> 'Query':
            return self

        def order_by(self, *_args: object) -> 'Query':
            return self

        def all(self) -> list[object]:
            return [sec]

    class Db:
        def query(self, *_args: object) -> Query:
            return Query()

    class Scope:
        def __enter__(self) -> Db:
            return Db()

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(service, 'platform_session_scope', lambda **_kwargs: Scope())
    monkeypatch.setattr(service, 'use_shared_search_path', lambda _db: None)
    monkeypatch.setattr(
        service,
        'newest_publication_for_source',
        lambda _db, source, exclude_ids=None: {'FINRA': finra, 'CFTC': cftc}.get(source),
    )
    monkeypatch.setattr(service, '_newsletter_table_summary', lambda pub: f'{pub.source} summary')

    _week, items = service.collect_weekly_publication_items(now=datetime(2026, 3, 23))
    assert {item['source'] for item in items} == {'SEC', 'FINRA', 'CFTC'}
    assert [item['title'] for item in items] == ['SEC item', 'FINRA notice', 'CFTC order']


def test_newsletter_http_routes_and_public_subscribe_path() -> None:
    from app.api.v1.endpoints.newsletter import router

    paths = {route.path for route in router.routes}
    assert '/newsletter/invites' in paths
    assert '/newsletter/subscribe' in paths
    assert '/newsletter/publications' in paths
    assert '/newsletter/subscribers' in paths
    assert '/newsletter/subscribers/{subscriber_id}' in paths
    assert '/api/v1/newsletter/subscribe' in PUBLIC_PATHS
    assert '/api/v1/newsletter/publications' in PUBLIC_PATHS
    assert '/api/v1/newsletter/subscribers' not in PUBLIC_PATHS


def test_only_master_and_cco_can_manage_org_newsletter_subscribers() -> None:
    from app.api.v1.endpoints.newsletter import require_newsletter_subscriber_manager
    from app.core.security import CurrentUser

    for role in ('master_user', 'chief_compliance_officer', 'cco', 'org:master_user'):
        user = CurrentUser(user_id='u1', org_id='org_acme', role=role, roles=frozenset({role}))
        assert require_newsletter_subscriber_manager(user) is user

    for role in ('product_manager', 'nominated_team_member', 'reviewer'):
        user = CurrentUser(user_id='u1', org_id='org_acme', role=role, roles=frozenset({role}))
        with pytest.raises(HTTPException) as exc:
            require_newsletter_subscriber_manager(user)
        assert exc.value.status_code == 403


def test_list_org_newsletter_subscribers_is_private_to_the_org(monkeypatch) -> None:
    from types import SimpleNamespace

    from app.core.security import CurrentUser
    from app.services import newsletter_service as service

    rows = [
        SimpleNamespace(
            id='sub-2',
            email='b@firm.com',
            source='invite',
            invited_by='user-1',
            subscribed_at=datetime(2026, 3, 20),
        ),
        SimpleNamespace(
            id='sub-1',
            email='a@firm.com',
            source='invite',
            invited_by=None,
            subscribed_at=datetime(2026, 3, 21),
        ),
    ]
    captured: dict[str, object] = {}

    class Query:
        def filter(self, *_args: object) -> 'Query':
            return self

        def order_by(self, *_args: object) -> 'Query':
            return self

        def all(self) -> list[object]:
            return rows

    class Db:
        def query(self, model: object) -> Query:
            captured['model'] = model
            return Query()

    class Scope:
        def __enter__(self) -> Db:
            return Db()

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(
        service,
        'session_scope',
        lambda **kwargs: captured.update(kwargs) or Scope(),
    )
    result = service.list_org_newsletter_subscribers(
        CurrentUser(user_id='u1', org_id='org_acme', role='master_user'),
    )
    assert captured['org_id'] == 'org_acme'
    assert captured['commit'] is False
    assert captured['model'] is service.NewsletterSubscriber
    assert result.total == 2
    assert [item.email for item in result.subscribers] == ['b@firm.com', 'a@firm.com']


def test_delete_org_newsletter_subscriber_removes_matching_row(monkeypatch) -> None:
    from types import SimpleNamespace

    from app.core.security import CurrentUser
    from app.services import newsletter_service as service

    row = SimpleNamespace(
        id='sub-1',
        email='cco@firm.com',
        source='invite',
        invited_by='user-1',
        subscribed_at=datetime(2026, 3, 20),
        auth_org_id='org_acme',
    )
    deleted: list[object] = []

    class Query:
        def filter(self, *_args: object) -> 'Query':
            return self

        def first(self) -> object:
            return row

    class Db:
        def query(self, model: object) -> Query:
            assert model is service.NewsletterSubscriber
            return Query()

        def delete(self, item: object) -> None:
            deleted.append(item)

    class Scope:
        def __enter__(self) -> Db:
            return Db()

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(service, 'session_scope', lambda **_kwargs: Scope())
    result = service.delete_org_newsletter_subscriber(
        'sub-1',
        CurrentUser(user_id='u1', org_id='org_acme', role='chief_compliance_officer'),
    )
    assert deleted == [row]
    assert result.status == 'deleted'
    assert result.id == 'sub-1'
    assert result.email == 'cco@firm.com'


def test_delete_org_newsletter_subscriber_404_when_missing(monkeypatch) -> None:
    from app.core.security import CurrentUser
    from app.services import newsletter_service as service

    class Query:
        def filter(self, *_args: object) -> 'Query':
            return self

        def first(self) -> None:
            return None

    class Db:
        def query(self, *_args: object) -> Query:
            return Query()

    class Scope:
        def __enter__(self) -> Db:
            return Db()

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(service, 'session_scope', lambda **_kwargs: Scope())
    with pytest.raises(HTTPException) as exc:
        service.delete_org_newsletter_subscriber(
            'missing',
            CurrentUser(user_id='u1', org_id='org_acme', role='master_user'),
        )
    assert exc.value.status_code == 404


def test_newsletter_invite_token_round_trip() -> None:
    settings = Settings(NEWSLETTER_TOKEN_SECRET='newsletter-test-secret')
    token = create_newsletter_invite_token(
        email='cco@example.com',
        org_id='org_123',
        list_kind=NEWSLETTER_LIST_ORG,
        invited_by='user_1',
        settings=settings,
    )
    claims = parse_newsletter_invite_token(token, settings=settings)
    assert claims.email == 'cco@example.com'
    assert claims.org_id == 'org_123'
    assert claims.list_kind == NEWSLETTER_LIST_ORG
    assert claims.invited_by == 'user_1'


def test_public_newsletter_invite_token_round_trip() -> None:
    settings = Settings(NEWSLETTER_TOKEN_SECRET='newsletter-test-secret')
    token = create_newsletter_invite_token(
        email='reader@firm.com',
        org_id='',
        list_kind=NEWSLETTER_LIST_PUBLIC,
        settings=settings,
    )
    claims = parse_newsletter_invite_token(token, settings=settings)
    assert claims.email == 'reader@firm.com'
    assert claims.org_id == ''
    assert claims.list_kind == NEWSLETTER_LIST_PUBLIC
    assert claims.invited_by is None


def test_public_token_strips_org_id_even_if_supplied() -> None:
    settings = Settings(NEWSLETTER_TOKEN_SECRET='newsletter-test-secret')
    token = create_newsletter_invite_token(
        email='reader@firm.com',
        org_id='org_should_not_stick',
        list_kind=NEWSLETTER_LIST_PUBLIC,
        settings=settings,
    )
    claims = parse_newsletter_invite_token(token, settings=settings)
    assert claims.list_kind == NEWSLETTER_LIST_PUBLIC
    assert claims.org_id == ''


def test_newsletter_invite_token_rejects_tampering() -> None:
    settings = Settings(NEWSLETTER_TOKEN_SECRET='newsletter-test-secret')
    token = create_newsletter_invite_token(
        email='cco@example.com',
        org_id='org_123',
        settings=settings,
    )
    with pytest.raises(HTTPException) as exc:
        parse_newsletter_invite_token(token + 'x', settings=settings)
    assert exc.value.status_code == 400


def test_newsletter_invite_token_rejects_expired_link() -> None:
    settings = Settings(NEWSLETTER_TOKEN_SECRET='newsletter-test-secret')
    token = create_newsletter_invite_token(
        email='cco@example.com',
        org_id='org_123',
        expires_at=datetime.now(UTC) - timedelta(hours=1),
        settings=settings,
    )
    with pytest.raises(HTTPException) as exc:
        parse_newsletter_invite_token(token, settings=settings)
    assert exc.value.status_code == 400
    assert 'expired' in str(exc.value.detail).lower()


def test_invite_email_uses_reg_ai_chrome_and_accept_link() -> None:
    html = build_newsletter_invite_email_html(
        accept_url='https://reg-ai.ipxenterprises.com/newsletter?token=abc',
        organization_name='Acme Compliance',
        invitee_email='reader@firm.com',
    )
    assert 'Reg AI' in html
    assert 'Accept invitation' in html
    assert 'https://reg-ai.ipxenterprises.com/newsletter?token=abc' in html
    assert 'Weekly' in html or 'Regulatory Feed' in html
    assert '#0b1f3a' in html


def test_weekly_html_uses_publication_summaries_and_signup_cta() -> None:
    long_summary = (
        'The Commission asked for comment on safeguarding client assets and explained '
        'how the proposed amendments would change custody practices for investment advisers, '
        'qualified custodians, and the clients whose cash and securities are at issue. '
        'Compliance teams should map the proposal to existing 206(4)-2 procedures and '
        'comment if operational constraints make the timeline unworkable.'
    )
    html = build_weekly_newsletter_html(
        week_ending=datetime(2026, 3, 20),
        items=[
            {
                'source': 'SEC',
                'title': 'SEC proposes custody amendments',
                'summary': long_summary,
                'kind': classify_publication_kind('SEC proposes custody amendments'),
                'published_label': 'Mar 18, 2026',
                'url': 'https://www.sec.gov/news/example',
                'comment_period': '1',
            },
            {
                'source': 'FINRA',
                'title': 'FINRA charges member firm',
                'summary': 'Enforcement alleged supervisory failures.',
                'kind': classify_publication_kind('FINRA charges member firm'),
                'published_label': 'Mar 17, 2026',
                'url': 'https://www.finra.org/example',
                'comment_period': '0',
            },
        ],
        signup_url='https://reg-ai.ipxenterprises.com/sign-up',
    )
    assert 'Weekly Regulatory Brief' in html
    assert html.count(long_summary) == 1
    assert 'SEC proposes custody amendments' in html
    assert 'Show more' not in html
    assert '-webkit-line-clamp' not in html
    assert 'FINRA charges member firm' in html
    assert 'Create a Reg-AI account' in html
    assert 'https://reg-ai.ipxenterprises.com/sign-up' in html
    assert 'Read More' in html
    assert 'https://reg-ai-production.vercel.app/newsletter' in html
    assert html.find('Read More') < html.find('Comment periods closing soon')
    assert 'policy scan' in html.lower() or 'redline' in html.lower()


def test_summary_html_includes_the_full_feed_text() -> None:
    short = 'The CFTC issued a brief staff letter.'
    long = (
        'The CFTC has issued a final rule modifying the mandatory clearing requirement '
        'under the Commodity Exchange Act for certain interest rate swaps denominated in '
        'Canadian dollars and Mexican pesos, including who must clear and by when.'
    )
    assert short in build_summary_html(short)
    long_html = build_summary_html(long)
    assert long in long_html
    assert long_html.count(long) == 1
    assert 'Show more' not in long_html


def test_public_invite_email_asks_for_confirmation() -> None:
    html = build_newsletter_invite_email_html(
        accept_url='https://reg-ai-dev.vercel.app/newsletter?token=abc',
        organization_name=None,
        invitee_email='reader@firm.com',
        public_signup=True,
    )
    assert 'Accept invitation' in html
    assert 'We will not add you until you accept this invitation' in html
    assert 'A colleague at' not in html


def test_subscribe_email_sends_invite_without_storing(monkeypatch) -> None:
    stored: list[dict[str, object]] = []
    sent: list[dict[str, str]] = []

    def fake_persist(
        *,
        email: str,
        org_id: str | None,
        source: str,
        invited_by: str | None = None,
        list_kind: str | None = None,
    ) -> bool:
        stored.append({
            'email': email,
            'org_id': org_id,
            'source': source,
            'invited_by': invited_by,
            'list_kind': list_kind,
        })
        return True

    def fake_send(*, to_email: str, subject: str, html: str) -> dict[str, str]:
        sent.append({'to_email': to_email, 'subject': subject, 'html': html})
        return {'status': 'sent'}

    monkeypatch.setattr(
        'app.services.newsletter_service._persist_subscriber',
        fake_persist,
    )
    monkeypatch.setattr('app.services.newsletter_service.send_resend_email', fake_send)
    response = subscribe_newsletter(
        NewsletterSubscribeRequest(email='Reader@Firm.com'),
        settings=Settings(
            NEWSLETTER_TOKEN_SECRET='newsletter-test-secret',
            FRONTEND_URL='https://reg-ai-dev.vercel.app',
        ),
    )
    assert response.status == 'sent'
    assert response.email == 'reader@firm.com'
    assert response.already_subscribed is False
    assert stored == []
    assert len(sent) == 1
    assert sent[0]['to_email'] == 'reader@firm.com'
    assert 'token=' in sent[0]['html']
    assert 'https://reg-ai-dev.vercel.app/newsletter?token=' in sent[0]['html']
    assert 'We will not add you until you accept this invitation' in sent[0]['html']


def test_subscribe_token_stores_public_confirm_on_general_list(monkeypatch) -> None:
    settings = Settings(NEWSLETTER_TOKEN_SECRET='newsletter-test-secret')
    token = create_newsletter_invite_token(
        email='reader@firm.com',
        org_id='',
        settings=settings,
    )
    stored: list[dict[str, object]] = []

    def fake_persist(
        *,
        email: str,
        org_id: str | None,
        source: str,
        invited_by: str | None = None,
        list_kind: str | None = None,
    ) -> bool:
        stored.append({
            'email': email,
            'org_id': org_id,
            'source': source,
            'invited_by': invited_by,
            'list_kind': list_kind,
        })
        return True

    monkeypatch.setattr(
        'app.services.newsletter_service._persist_subscriber',
        fake_persist,
    )
    response = subscribe_newsletter(
        NewsletterSubscribeRequest(token=token),
        settings=settings,
    )
    assert response.status == 'subscribed'
    assert response.email == 'reader@firm.com'
    assert stored == [{
        'email': 'reader@firm.com',
        'org_id': None,
        'source': 'public',
        'invited_by': None,
        'list_kind': NEWSLETTER_LIST_PUBLIC,
    }]


def test_subscribe_token_stores_invite_on_org(monkeypatch) -> None:
    settings = Settings(NEWSLETTER_TOKEN_SECRET='newsletter-test-secret')
    token = create_newsletter_invite_token(
        email='cco@example.com',
        org_id='org_abc',
        list_kind=NEWSLETTER_LIST_ORG,
        invited_by='user_1',
        settings=settings,
    )
    stored: list[dict[str, object]] = []

    def fake_persist(
        *,
        email: str,
        org_id: str | None,
        source: str,
        invited_by: str | None = None,
        list_kind: str | None = None,
    ) -> bool:
        stored.append({
            'email': email,
            'org_id': org_id,
            'source': source,
            'invited_by': invited_by,
            'list_kind': list_kind,
        })
        return True

    monkeypatch.setattr(
        'app.services.newsletter_service._persist_subscriber',
        fake_persist,
    )
    response = subscribe_newsletter(
        NewsletterSubscribeRequest(token=token),
        settings=settings,
    )
    assert response.email == 'cco@example.com'
    assert stored == [{
        'email': 'cco@example.com',
        'org_id': 'org_abc',
        'source': 'invite',
        'invited_by': 'user_1',
        'list_kind': NEWSLETTER_LIST_ORG,
    }]


def test_legacy_tokens_without_list_kind_still_route_by_org_id(monkeypatch) -> None:
    import base64
    import hashlib
    import hmac
    import json

    settings = Settings(NEWSLETTER_TOKEN_SECRET='newsletter-test-secret')

    def mint(email: str, org_id: str) -> str:
        payload = {
            'e': email,
            'o': org_id,
            'exp': int((datetime.now(UTC) + timedelta(days=1)).timestamp()),
        }
        raw = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(',', ':')).encode('utf-8')
        ).decode('ascii').rstrip('=')
        signature = hmac.new(
            settings.resolved_newsletter_token_secret.encode('utf-8'),
            raw.encode('ascii'),
            hashlib.sha256,
        ).hexdigest()
        return f'{raw}.{signature}'

    stored: list[dict[str, object]] = []

    def fake_persist(
        *,
        email: str,
        org_id: str | None,
        source: str,
        invited_by: str | None = None,
        list_kind: str | None = None,
    ) -> bool:
        stored.append({'email': email, 'org_id': org_id, 'source': source, 'list_kind': list_kind})
        return True

    monkeypatch.setattr('app.services.newsletter_service._persist_subscriber', fake_persist)
    subscribe_newsletter(NewsletterSubscribeRequest(token=mint('public@firm.com', '')), settings=settings)
    subscribe_newsletter(
        NewsletterSubscribeRequest(token=mint('org@firm.com', 'org_abc')),
        settings=settings,
    )
    assert stored == [
        {
            'email': 'public@firm.com',
            'org_id': None,
            'source': 'public',
            'list_kind': NEWSLETTER_LIST_PUBLIC,
        },
        {
            'email': 'org@firm.com',
            'org_id': 'org_abc',
            'source': 'invite',
            'list_kind': NEWSLETTER_LIST_ORG,
        },
    ]


def test_publications_invite_requires_a_real_organization() -> None:
    from typing import cast

    from sqlalchemy.orm import Session

    from app.core.security import CurrentUser
    from app.schemas.newsletter import NewsletterInviteRequest

    with pytest.raises(HTTPException) as exc:
        invite_newsletter_subscriber(
            NewsletterInviteRequest(email='cco@example.com'),
            CurrentUser(user_id='user_1', org_id='platform', role='master_user'),
            cast(Session, object()),
        )
    assert exc.value.status_code == 400


def test_publications_invite_mints_org_list_token(monkeypatch) -> None:
    from typing import cast

    from sqlalchemy.orm import Session

    from app.core.security import CurrentUser
    from app.schemas.newsletter import NewsletterInviteRequest

    captured: dict[str, object] = {}

    def fake_send(**kwargs: object) -> str:
        captured.update(kwargs)
        return 'sent'

    monkeypatch.setattr(
        'app.services.newsletter_service._send_newsletter_invitation',
        fake_send,
    )
    monkeypatch.setattr(
        'app.services.newsletter_service._organization_name',
        lambda _db, _org_id: 'Acme',
    )
    response = invite_newsletter_subscriber(
        NewsletterInviteRequest(email='cco@example.com'),
        CurrentUser(user_id='user_1', org_id='org_abc', role='master_user'),
        cast(Session, object()),
        settings=Settings(NEWSLETTER_TOKEN_SECRET='newsletter-test-secret'),
    )
    assert response.status == 'sent'
    assert captured['org_id'] == 'org_abc'
    assert captured['list_kind'] == NEWSLETTER_LIST_ORG
    assert captured['invited_by'] == 'user_1'
    assert captured.get('public_signup') is not True


def test_newsletter_page_subscribe_mints_public_list_token(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_send(**kwargs: object) -> str:
        captured.update(kwargs)
        return 'sent'

    monkeypatch.setattr(
        'app.services.newsletter_service._send_newsletter_invitation',
        fake_send,
    )
    response = subscribe_newsletter(
        NewsletterSubscribeRequest(email='reader@firm.com'),
        settings=Settings(NEWSLETTER_TOKEN_SECRET='newsletter-test-secret'),
    )
    assert response.status == 'sent'
    assert captured['org_id'] == ''
    assert captured['list_kind'] == NEWSLETTER_LIST_PUBLIC
    assert captured['public_signup'] is True


def test_weekly_send_builds_once_and_dedupes_recipients(monkeypatch) -> None:
    sent: list[str] = []
    subjects: list[str] = []
    monkeypatch.setattr(
        'app.services.newsletter_service.collect_weekly_publication_items',
        lambda now=None: (
            datetime(2026, 3, 23),
            [
                {
                    'source': 'SEC',
                    'title': 'Example',
                    'summary': 'Shared feed summary',
                    'kind': 'Agency action',
                    'published_label': 'Mar 20, 2026',
                    'url': 'https://www.sec.gov/x',
                    'comment_period': '0',
                }
            ],
        ),
    )
    monkeypatch.setattr(
        'app.services.newsletter_service.list_all_subscriber_emails',
        lambda: (['one@firm.com', 'two@firm.com'], 3),
    )
    captured: list[dict[str, str]] = []
    monkeypatch.setattr(
        'app.services.newsletter_service.send_resend_email',
        lambda **kwargs: sent.append(kwargs['to_email'])
        or subjects.append(kwargs['subject'])
        or captured.append({'html': kwargs['html']})
        or {'status': 'sent'},
    )
    result = send_weekly_newsletter(
        settings=Settings(FRONTEND_URL='https://reg-ai.ipxenterprises.com'),
        now=datetime(2026, 3, 23),
    )
    assert result.status == 'completed'
    assert result.publication_count == 1
    assert result.recipient_count == 2
    assert result.sent_count == 2
    assert result.skipped_orgs == 3
    assert sent == ['one@firm.com', 'two@firm.com']
    assert sent.count('one@firm.com') == 1
    assert subjects == [
        'Reg-AI Newsletter: Weekly Regulatory Brief [Mar 23 2026]',
        'Reg-AI Newsletter: Weekly Regulatory Brief [Mar 23 2026]',
    ]
    assert captured
    assert 'https://reg-ai.ipxenterprises.com/sign-up' in captured[0]['html']
    assert 'https://reg-ai.ipxenterprises.com/newsletter' in captured[0]['html']
    assert 'localhost' not in captured[0]['html']


def test_persist_invite_uses_org_table(monkeypatch) -> None:
    from app.models.core import NewsletterSubscriber
    from app.services import newsletter_service as service

    added: list[object] = []

    class FakeQuery:
        def filter(self, *_args: object) -> 'FakeQuery':
            return self

        def first(self) -> None:
            return None

    class FakeDb:
        def query(self, model: object) -> FakeQuery:
            assert model is NewsletterSubscriber
            return FakeQuery()

        def add(self, row: object) -> None:
            added.append(row)

        def flush(self) -> None:
            return None

    class FakeScope:
        def __enter__(self) -> FakeDb:
            return FakeDb()

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(service, 'session_scope', lambda **_kwargs: FakeScope())
    created = service._persist_subscriber(
        email='cco@example.com',
        org_id='org_abc',
        source='invite',
        invited_by='user_1',
        list_kind=NEWSLETTER_LIST_ORG,
    )
    assert created is True
    assert len(added) == 1
    assert isinstance(added[0], NewsletterSubscriber)
    assert added[0].auth_org_id == 'org_abc'
    assert added[0].email == 'cco@example.com'


def test_persist_public_uses_general_table(monkeypatch) -> None:
    from app.models.core import GeneralNewsletterSubscriber
    from app.services import newsletter_service as service

    added: list[object] = []

    class FakeQuery:
        def filter(self, *_args: object) -> 'FakeQuery':
            return self

        def first(self) -> None:
            return None

    class FakeDb:
        def query(self, model: object) -> FakeQuery:
            assert model is GeneralNewsletterSubscriber
            return FakeQuery()

        def add(self, row: object) -> None:
            added.append(row)

        def flush(self) -> None:
            return None

    class FakeScope:
        def __enter__(self) -> FakeDb:
            return FakeDb()

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(service, 'platform_session_scope', lambda **_kwargs: FakeScope())
    created = service._persist_subscriber(
        email='reader@firm.com',
        org_id=None,
        source='public',
        list_kind=NEWSLETTER_LIST_PUBLIC,
    )
    assert created is True
    assert len(added) == 1
    assert isinstance(added[0], GeneralNewsletterSubscriber)
    assert added[0].email == 'reader@firm.com'


def test_persist_public_list_ignores_org_id(monkeypatch) -> None:
    from app.models.core import GeneralNewsletterSubscriber
    from app.services import newsletter_service as service

    added: list[object] = []

    class FakeQuery:
        def filter(self, *_args: object) -> 'FakeQuery':
            return self

        def first(self) -> None:
            return None

    class FakeDb:
        def query(self, model: object) -> FakeQuery:
            assert model is GeneralNewsletterSubscriber
            return FakeQuery()

        def add(self, row: object) -> None:
            added.append(row)

        def flush(self) -> None:
            return None

    class FakeScope:
        def __enter__(self) -> FakeDb:
            return FakeDb()

        def __exit__(self, *_args: object) -> None:
            return None

    def fail_org_scope(**_kwargs: object) -> None:
        raise AssertionError('public signups must not use the org newsletter table')

    monkeypatch.setattr(service, 'session_scope', fail_org_scope)
    monkeypatch.setattr(service, 'platform_session_scope', lambda **_kwargs: FakeScope())
    created = service._persist_subscriber(
        email='reader@firm.com',
        org_id='org_abc',
        source='public',
        list_kind=NEWSLETTER_LIST_PUBLIC,
    )
    assert created is True
    assert isinstance(added[0], GeneralNewsletterSubscriber)


def _isolated_frontend_settings(**kwargs: str) -> Settings:
    payload = {
        'FRONTEND_URL': 'http://localhost:3000',
        'CORS_ORIGINS': 'http://localhost:3000',
        'CLERK_AUTHORIZED_PARTIES': '',
        'ENVIRONMENT': 'local',
    }
    payload.update(kwargs)
    return Settings(**payload)


def test_public_frontend_url_ignores_localhost_and_follows_environment() -> None:
    assert _isolated_frontend_settings(
        ENVIRONMENT='production',
    ).public_frontend_url == PUBLIC_FRONTEND_URL_PRODUCTION
    assert _isolated_frontend_settings(
        ENVIRONMENT='staging',
    ).public_frontend_url == PUBLIC_FRONTEND_URL_STAGING
    assert _isolated_frontend_settings(
        ENVIRONMENT='dev',
    ).public_frontend_url == PUBLIC_FRONTEND_URL_DEV
    assert _isolated_frontend_settings(
        ENVIRONMENT='local',
    ).public_frontend_url == PUBLIC_FRONTEND_URL_DEV


def test_public_frontend_url_prefers_configured_https_origin() -> None:
    settings = _isolated_frontend_settings(
        ENVIRONMENT='production',
        FRONTEND_URL='https://reg-ai.ipxenterprises.com',
        CORS_ORIGINS='https://reg-ai.ipxenterprises.com',
    )
    assert settings.public_frontend_url == 'https://reg-ai.ipxenterprises.com'


def test_public_frontend_url_uses_cors_when_frontend_url_is_local() -> None:
    settings = _isolated_frontend_settings(
        ENVIRONMENT='production',
        FRONTEND_URL='http://localhost:3000',
        CORS_ORIGINS='http://localhost:3000,https://reg-ai-production.vercel.app',
    )
    assert settings.public_frontend_url == PUBLIC_FRONTEND_URL_PRODUCTION


def test_weekly_send_signup_cta_never_uses_localhost(monkeypatch) -> None:
    sent_html: list[str] = []
    monkeypatch.setattr(
        'app.services.newsletter_service.collect_weekly_publication_items',
        lambda now=None: (
            datetime(2026, 3, 23),
            [
                {
                    'source': 'SEC',
                    'title': 'Example',
                    'summary': 'Shared feed summary',
                    'kind': 'Agency action',
                    'published_label': 'Mar 20, 2026',
                    'url': 'https://www.sec.gov/x',
                    'comment_period': '0',
                }
            ],
        ),
    )
    monkeypatch.setattr(
        'app.services.newsletter_service.list_all_subscriber_emails',
        lambda: (['one@firm.com'], 0),
    )
    monkeypatch.setattr(
        'app.services.newsletter_service.send_resend_email',
        lambda **kwargs: sent_html.append(kwargs['html']) or {'status': 'sent'},
    )

    send_weekly_newsletter(
        settings=_isolated_frontend_settings(ENVIRONMENT='production'),
        now=datetime(2026, 3, 23),
    )
    assert sent_html
    assert 'http://localhost:3000' not in sent_html[0]
    assert f'{PUBLIC_FRONTEND_URL_PRODUCTION}/sign-up' in sent_html[0]
    assert f'{PUBLIC_FRONTEND_URL_PRODUCTION}/newsletter' in sent_html[0]

    sent_html.clear()
    send_weekly_newsletter(
        settings=_isolated_frontend_settings(ENVIRONMENT='staging'),
        now=datetime(2026, 3, 23),
    )
    assert f'{PUBLIC_FRONTEND_URL_STAGING}/sign-up' in sent_html[0]

    sent_html.clear()
    send_weekly_newsletter(
        settings=_isolated_frontend_settings(ENVIRONMENT='dev'),
        now=datetime(2026, 3, 23),
    )
    assert f'{PUBLIC_FRONTEND_URL_DEV}/sign-up' in sent_html[0]


def test_subscribe_invite_skips_localhost_frontend_url(monkeypatch) -> None:
    sent: list[dict[str, str]] = []
    monkeypatch.setattr(
        'app.services.newsletter_service._persist_subscriber',
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        'app.services.newsletter_service.send_resend_email',
        lambda **kwargs: sent.append(kwargs) or {'status': 'sent'},
    )
    subscribe_newsletter(
        NewsletterSubscribeRequest(email='reader@firm.com'),
        settings=_isolated_frontend_settings(
            NEWSLETTER_TOKEN_SECRET='newsletter-test-secret',
            ENVIRONMENT='production',
        ),
    )
    assert sent
    assert f'{PUBLIC_FRONTEND_URL_PRODUCTION}/newsletter?token=' in sent[0]['html']
    assert 'localhost' not in sent[0]['html']


def test_weekly_send_skips_when_all_lists_empty(monkeypatch) -> None:
    monkeypatch.setattr(
        'app.services.newsletter_service.collect_weekly_publication_items',
        lambda now=None: (datetime(2026, 3, 23), []),
    )
    monkeypatch.setattr(
        'app.services.newsletter_service.list_all_subscriber_emails',
        lambda: ([], 4),
    )
    result = send_weekly_newsletter(now=datetime(2026, 3, 23))
    assert result.status == 'skipped'
    assert result.recipient_count == 0
    assert result.skipped_orgs == 4


def test_newsletter_table_summary_prefers_feed_summary_over_title() -> None:
    from app.models.core import RegulatoryPublication
    from app.services.newsletter_service import _newsletter_table_summary

    title = 'Statement on Proposed Rescission of Rule 206(4)-5 under the Investment Advisers Act'
    body = (
        'The Commission proposed to rescind Rule 206(4)-5 and asked advisers to review '
        'how political-contribution restrictions would change if the rule is withdrawn. '
    ) * 8
    publication = RegulatoryPublication(
        id='pub-newsletter-summary',
        source='SEC',
        title=title,
        url='https://www.sec.gov/news/statement/example',
        raw_text=body,
        feed_summary=body.strip(),
        processed=False,
        embedding_chunk_ids=[],
    )
    summary = _newsletter_table_summary(publication)
    assert summary != title
    assert 'political-contribution' in summary


def test_newsletter_table_summary_falls_back_when_empty() -> None:
    from app.models.core import RegulatoryPublication
    from app.services.newsletter_service import _PENDING_FEED_SUMMARY, _newsletter_table_summary

    publication = RegulatoryPublication(
        id='pub-newsletter-empty',
        source='SEC',
        title='Untitled statement',
        url='https://www.sec.gov/news/empty',
        raw_text=None,
        feed_summary=None,
        processed=False,
        embedding_chunk_ids=[],
    )
    assert _newsletter_table_summary(publication) == _PENDING_FEED_SUMMARY
