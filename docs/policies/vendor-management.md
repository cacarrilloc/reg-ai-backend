# Vendor Management Policy

**Owner:** Security / Engineering / Operations  
**Applies to:** Third parties that process Reg-AI or customer data, or that can affect service security  
**Review cadence:** At least annually per material vendor  

## 1. Purpose

Ensure third-party vendors meet security and privacy expectations aligned with SOC 2 shared-responsibility obligations.

## 2. Material vendors (current)

| Vendor | Function | Data types (typical) |
| --- | --- | --- |
| Supabase | Auth, Postgres, Storage | Account, org, policy content |
| Vercel | Frontend hosting | Request metadata |
| Railway | API hosting | Application logs/secrets (config) |
| Resend | Transactional email | Email addresses, notification content |
| Stripe | Billing | Billing contact / payment metadata |
| Sentry | Error monitoring | Error telemetry (minimize PII) |
| PostHog | Product analytics | Pseudonymous product events |
| Langfuse | LLM observability | Prompt/trace metadata |
| Anthropic / OpenAI | Model inference / embeddings | Policy text excerpts as required for features |
| GitHub | Source control / CI | Source code, workflow logs |
| Crisp (optional) | Support chat | Support conversations |

## 3. Onboarding criteria

Before production use of a material vendor:

1. Document business purpose and data categories shared.
2. Confirm security posture via SOC 2 / ISO report, trust center, or equivalent questionnaire.
3. Execute DPA / BAA where legally required (especially EU personal data or PHI scenarios).
4. Prefer vendors with encryption in transit and at rest by default.
5. Configure least-privilege access and MFA on the vendor console.

## 4. Ongoing oversight

1. **Annual review** of SOC 2 / trust reports for material vendors.
2. **Access review** of console users each quarter with broader access reviews.
3. Track critical/high CVEs affecting libraries pulled from vendor ecosystems via CI vulnerability scanning.
4. Re-evaluate vendors on ownership change, breach notification, or material scope expansion.

## 5. Offboarding

1. Revoke API keys and console seats.
2. Request deletion/export of retained customer data where contractually required.
3. Update inventory in this policy and dependency docs.

## 6. Evidence

- Vendor inventory (this document)
- Executed DPAs / BAAs (secured store)
- Latest SOC 2 / trust center downloads with review date
- Console MFA and seat review notes

## 7. Related documents

- `docs/compliance-foundations.md`
- `docs/policies/access-control.md`
- `docs/policies/encryption.md`
