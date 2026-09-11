# Security Policy

## Reporting A Vulnerability

Please report security issues privately, not as a public issue.

Use GitHub's private vulnerability reporting on this repository
(Security, then Report a vulnerability), which opens a channel visible only to the
maintainer. If that is unavailable, open an issue that says only that you have a security
report and asks for a contact address; do not include details.

Please include what you can: the version or commit, what an attacker gains, and the
smallest reproduction you have. A proof of concept is welcome but never required.

Expect an acknowledgement within a week. This is a demonstration project maintained by
one person, so there is no paid response commitment, and there is no bug bounty.

## Scope

This repository is a demonstration system. It is not a hosted service, and there is no
production deployment with third-party data to protect. The interesting reports are
therefore about the code and the infrastructure it creates:

- A way for an agent role to take an officer action (review, approve, sweep). The
  separation is the central guarantee of the design; see ADR-0019.
- A way to read a beneficial owner name through the API, the reports or the user
  interface. That column exists only as ciphertext and one tool decrypts it.
- Prompt injection through tool output that changes what an agent does, rather than what
  it says.
- A privilege escalation in the CDK stacks: a role that can do more than its description
  claims, a resource policy that admits more than the documented principal.
- Anything that lets an unauthenticated caller past the load balancer sign-in.

Out of scope: the self-signed certificate a deploy generates when no domain certificate is
supplied (documented, and a browser warning by design), missing rate limits on the local
compose stack, and findings that require credentials the attacker should not have.

## What The System Already Assumes

`docs/SECURITY.md` holds the threat model, the controls and the known gaps, including the
ones we have accepted deliberately. Reading it first will save you time.
