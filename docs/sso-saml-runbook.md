# Supabase SAML SSO runbook

Reg AI uses **Supabase Auth** on the control-plane project.

## Layers

1. **Social IdP (Google + LinkedIn)** — enabled in Supabase Dashboard → Authentication → Providers. App buttons call `signInWithOAuth`.
2. **Enterprise SAML** — self-serve from Organization Settings (permission `org:sys_sso:manage`). Backend registers IdPs via Auth Admin API (`/auth/v1/admin/sso/providers`).
3. **JIT membership** — after SSO login, `POST /api/v1/auth/sso/jit-sync` maps email domain (+ optional IdP groups) to `organization_members`.
4. **SSO-only domains** — when enabled, password sign-in/sign-up is blocked for those domains (`POST /api/v1/auth/sso/login-policy`).

## Prerequisites

- Supabase project with Google and LinkedIn (OIDC) providers configured (client IDs/secrets)
- Redirect URL allowlist includes `{FRONTEND_URL}/auth/callback`
- For enterprise SAML: enable SAML 2.0 on the Auth Providers page (Team plan)
- Backend: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SSO_ENABLED=true`
- Frontend: `NEXT_PUBLIC_SSO_ENABLED=true`

## Self-serve SAML (app Settings)

1. Copy SP Entity ID / ACS URL from Settings → Single sign-on (or `GET /organizations/current/sso/sp-info`).
2. Configure the customer IdP (Entra, Okta, Google Workspace SAML app, etc.).
3. Paste IdP Metadata URL (preferred) or Metadata XML + email domains.
4. Optionally enable SSO-only, JIT, and group → role mappings.
5. Save. Status `active` means Supabase accepted the provider; `pending`/`error` means config is stored but SAML registration failed (often SAML not enabled yet).

## Sign-in paths

- Google / LinkedIn buttons → OAuth → `/auth/callback` → JIT sync
- Work email domain with active SAML → `signInWithSSO({ domain })` → callback → JIT sync
- Password path → login-policy check; if `sso_required`, redirect to corporate SSO

## Custom access token hook

Still recommended so FastAPI JWTs carry `org_id` + `role` after org switch. See `supabase/functions/custom-access-token/README.md`.
