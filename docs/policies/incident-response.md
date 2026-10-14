# Incident Response Policy

**Owner:** Security / Engineering  
**Applies to:** Confirmed or suspected security incidents affecting Reg-AI systems or customer data  
**Review cadence:** At least annually, and after major incidents  

## 1. Purpose

Establish a consistent process to detect, contain, eradicate, recover from, and learn from security incidents.

## 2. Definitions

- **Event:** An observable anomaly (failed auth spike, error burst, dependency CVE).
- **Incident:** An event that compromises confidentiality, integrity, or availability, or a credible imminent risk of such compromise.
- **Severity:** Sev-1 (customer data exposure / auth bypass / sustained outage) through Sev-4 (low-risk, no customer impact).

## 3. Detection sources

- Sentry error and security alerts (`authentication_failed`, auth-rate limits)
- Application structured logs
- Supabase Auth / hosting provider alerts
- Vulnerability scan results from CI (npm audit / pip-audit)
- Customer reports via Support (`support_tickets`) or Crisp

## 4. Roles

| Role | Responsibility |
| --- | --- |
| Incident Commander (IC) | Owns timeline, decisions, and communications |
| Engineering lead | Technical containment and fix |
| Communications | Customer/vendor notices when required |
| Recorder | Maintains incident timeline and evidence |

## 5. Response phases

### 5.1 Identify

1. Open an incident channel / ticket with start time, suspected scope, and severity.
2. Preserve logs and screenshots; do not destroy evidence.

### 5.2 Contain

1. Revoke compromised sessions/keys; rotate secrets.
2. Disable abusive IPs via allowlist / platform firewall where applicable.
3. Temporarily disable risky features if needed (auth bypass is never an acceptable mitigation in prod).

### 5.3 Eradicate & recover

1. Deploy fix through normal change controls (PR + CI).
2. Verify health endpoints and critical user journeys.
3. Confirm MFA and RBAC controls remain enforced.

### 5.4 Communicate

1. Internal notice immediately for Sev-1/Sev-2.
2. Customer notice when personal data or service integrity is affected, consistent with contractual and legal duties.
3. Notify processors/vendors when their systems are implicated.

### 5.5 Post-incident

1. Within 5 business days for Sev-1/Sev-2, complete a blameless postmortem:
   - root cause
   - timeline
   - customer impact
   - corrective actions and owners
2. File corrective actions and track to closure.

## 6. Evidence to retain

- Incident ticket / timeline
- Relevant logs and Sentry issues
- Customer notification copies (if any)
- Postmortem and closed actions

## 7. Related runbooks

- `docs/security-hardening.md`
- `docs/staging-environment.md`
- Support ticket flow to `SUPPORT_NOTIFY_EMAIL`
