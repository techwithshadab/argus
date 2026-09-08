# ADR-0018: The watch floor signs in at the load balancer; tools sign with IAM

Date: 2026-09-07. Status: accepted.

## Context

The UI and Grafana were reachable on plain HTTP from `uiAllowedCidr` (default open),
the officer's identity was a free-text header (`X-Watch-Officer`) that every review,
approval and sweep recorded, and nothing rate-limited or filtered requests. Agents and
tool servers already proved identity to each other with STS-signed caller tokens
(`callerauth.py`, ADR-0001), so the gap was the human edge only.

## Decision

1. **Cognito on every public listener.** A user pool `argus-officers` (no self sign-up,
   officers created by an operator, MFA optional by default and `officerMfa=required`
   to enforce it) and the ALB's own authenticate action on the HTTPS listeners for the
   UI and Grafana. Sessions last one watch shift (8 h). HTTP redirects to HTTPS.
2. **The API trusts the balancer's signed token, nothing else.** With
   `OFFICER_AUTH=oidc` the API verifies `x-amzn-oidc-data` (ES256 against the key the
   balancer publishes for the token's `kid`, the `signer` must be this balancer, the
   issuer this pool) and records the email claim as the officer. Every route that is
   neither public (`/health`, `/metrics`, `/whoami`) nor agent-only needs that token or
   a verified IAM caller; agent-only routes stay IAM-only (`services/api/officerauth.py`).
   Locally (`OFFICER_AUTH=header`) the typed id still applies.
3. **Non-browser callers use IAM.** Requests on `/api/*` carrying a bearer token skip
   the sign-in at the balancer (a listener rule) and are checked by the API against
   `TOOL_ALLOWED_ROLES`, which includes `argus-operator`, a role any principal in
   the account may assume (`operatorPrincipalArn` narrows it). `make eval-aws` assumes
   it. Root and IAM users are not roles and are refused, on purpose.
4. **TLS without a domain.** A deploy without `uiCertificateArn` gets a self-signed
   certificate generated inside a Lambda at deploy time and imported into ACM (the key
   never enters a template). The same issuer gives the internal balancer's API listener
   its certificate; agents trust it through the SSM parameter `/argus/internal-ca`.
   A domain certificate replaces the public one without any other change.
5. **AWS WAF on the public balancer.** A per-IP rate limit and the managed rule groups
   for IP reputation, common exploits and known bad inputs; blocked requests alarm.

Considered and not taken: CloudFront in front of the balancer (it cannot connect over
TLS to a self-signed origin, so it needs a domain first; with one, it is a small
addition), Private CA for the internal listener (about $400 a month), the API doing the
OIDC exchange itself (a confidential client secret in the platform for no gain over
the balancer's action).

## Consequences

The officer id on every decision is an authenticated email. A browser sees one
certificate warning per profile until a domain certificate is supplied. Scripts against
AWS must assume the operator role, and the evals in CI need role credentials rather
than a bare URL. Cognito's Lite plan and WAF add roughly $9 a month plus request charges.
