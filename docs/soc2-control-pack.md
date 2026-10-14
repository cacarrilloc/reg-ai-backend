# SOC 2 Control Pack (Basic)

This pack maps the four basic SOC 2 preparation requirements to Reg-AI product and process controls. It is **not** a formal SOC 2 attestation.

## 1. Multi-factor authentication across all systems

| Control | Implementation |
| --- | --- |
| App MFA enrollment | Forced TOTP enrollment via `/mfa` when `NEXT_PUBLIC_MFA_REQUIRED` is not `false` |
| App MFA challenge | AAL2 challenge after enrollment before app shell access |
| Sensitive API MFA | `require_aal2` on billing mutations and support ticket writes |
| Operator MFA | Required on Supabase/Vercel/Railway/GitHub/Stripe consoles (policy) |
| Evidence | Access Control Policy; health flags `mfa_required_*`; Settings MFA card |

## 2. Encryption of data at rest and in transit

| Control | Implementation |
| --- | --- |
| In transit | HTTPS on Vercel/Railway; Supabase TLS; HSTS in non-local envs |
| At rest | Supabase managed disk encryption; private buckets |
| Verification | Encryption Policy; `/health` `database_tls_requested` / `transport_https_expected` |
| Evidence | Vendor trust reports + annual checklist in Encryption Policy |

## 3. Regular vulnerability scanning

| Control | Implementation |
| --- | --- |
| Application dependencies | GitHub Actions `vulnerability-scan` workflows on PR/push + weekly schedule |
| Frontend | `npm audit --audit-level=high` |
| Backend | `pip-audit -r requirements.txt` |
| Dependency updates | Dependabot (`npm`, `pip`, `github-actions`) |
| Evidence | CI run history; Dependabot PRs; Incident Response for exploitability triage |

## 4. Documented policies

| Policy | Location |
| --- | --- |
| Access control | `docs/policies/access-control.md` |
| Incident response | `docs/policies/incident-response.md` |
| Vendor management | `docs/policies/vendor-management.md` |
| Encryption | `docs/policies/encryption.md` |

## Operator cadence

| Cadence | Activity |
| --- | --- |
| Continuous | CI vulnerability scans on every PR |
| Weekly | Scheduled vulnerability workflows |
| Quarterly | Access reviews (app + vendor consoles) |
| Annually | Policy review; vendor SOC report refresh; encryption checklist |

## Related docs

- `docs/compliance-foundations.md`
- `docs/security-hardening.md`
- `docs/phase_8_audit_hardening.md`
