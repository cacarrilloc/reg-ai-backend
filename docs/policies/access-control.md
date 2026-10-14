# Access Control Policy

**Owner:** Security / Engineering  
**Applies to:** Reg-AI application, Supabase Auth, hosting consoles, and production secrets  
**Review cadence:** At least quarterly  

## 1. Purpose

Define how access to Reg-AI systems and customer data is granted, reviewed, and revoked in support of SOC 2 Trust Services Criteria for security.

## 2. Scope

- End-user access to the Reg-AI web application
- Privileged access to Supabase, Vercel, Railway, GitHub, Resend, Sentry, Stripe, and related vendors
- Service accounts and CI/CD credentials

## 3. Identity and authentication

1. **Human users** authenticate with Supabase Auth (email/password, Google/LinkedIn OAuth, or corporate SSO when enabled).
2. **Multi-factor authentication (MFA)** is required for all application users (`NEXT_PUBLIC_MFA_REQUIRED` / `MFA_REQUIRED_FOR_ALL_USERS`, default on). Users must enroll a TOTP authenticator before using the product and complete AAL2 on sign-in.
3. **Sensitive API actions** (billing mutations, support ticket writes) require an AAL2 session.
4. **Auth bypass** (`AUTH_BYPASS_ENABLED`) is forbidden in staging/production.

## 4. Authorization model

1. Access is organization-scoped via JWT `org_id` and membership records (`organization_members`).
2. Role-based and permission-based controls enforce least privilege for reviews, settings, invites, and internal ops roles.
3. Row-level security (RLS) remains enabled on tenant tables where configured; API uses service-role access with application-layer org scoping.

## 5. Privileged / admin access

1. Cloud console access (Supabase, Vercel, Railway, GitHub org, Stripe) is limited to named operators.
2. Privileged accounts must use provider MFA.
3. Secrets are stored in platform environment stores; secrets are never committed to git.

## 6. Joiner / mover / leaver

1. **Join:** Access granted via invite or SSO JIT into an organization with an explicit role.
2. **Move:** Role/permission changes are performed by org admins or internal operators and are auditable where wired.
3. **Leave:** Disable/remove membership promptly; revoke console access the same business day when employment ends.

## 7. Access reviews

1. Quarterly review of:
   - Application org admins / internal roles
   - GitHub repository collaborators
   - Supabase / Vercel / Railway / Stripe console users
2. Retain review evidence (checklist + date + reviewer) for audit.

## 8. Related evidence

- Product: MFA enrollment gate, RBAC guards, audit trail (`/audit`)
- Operator: `docs/security-hardening.md`, staging runbook, console MFA screenshots
