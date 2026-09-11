# Responsible AI In Argus

Maritime domain awareness sits close to enforcement. A system that points a watch officer
at a vessel is a system that can point a state at a crew. This page states what Argus
does about that, and what it deliberately refuses to do.

`docs/MODEL_CARD.md` describes the system and its failure modes. This page is about the
choices behind it.

## The Principle

**A model may inform a decision. It may not make one, and it may not make one look
already made.**

Everything below follows from that sentence.

## What That Means In The Code

**The human gate is a control, not a convention.** Agents write `proposed` tasking rows
and `draft` findings. Approving and reviewing are separate endpoints admitting only the
operator role, on a different allowlist from the agent roles, enforced per route
(ADR-0019). An agent role calling a review endpoint is refused. Before that separation
existed, the guarantee the product made was true only because nothing had tried to break
it.

**Detection does not use a model.** Five SQL detectors produce candidates with fixed
vessel identifiers, windows and evidence. This is not only about accuracy. A model that
selects which vessels are suspicious is a model whose biases become an operational
targeting pattern, and one that cannot be audited by reading it. SQL can be read.

**Some things a model may not wave away.** One identity transmitting from two places at
once, and two vessels stopped together outside any anchorage, can only be raised, never
dismissed. These are definitional, so they are code.

**A report cannot outrun its evidence.** Every indicator must trace to a tool result;
recommended actions must match an allowed pattern; a report with no counter-indicators
and no stated gaps is flagged, and if it stays that way it ships with a caveat saying so
(ADR-0020). A failed line of enquiry caps confidence and priority in code and names the
gap. Confident prose is the easiest thing a language model produces and the most
dangerous thing to hand an officer.

**Uncertainty is stated, not smoothed.** Reports carry data caveats built from what the
deployment actually used: live feed or replay, OpenSanctions or a local list, and the note
that the registry may be incomplete. An AIS identity is self-reported and forgeable, which
is the thing several detectors exist to catch.

## Personal Data

The subject is a vessel, not a person. Beneficial owner names and person entity names
exist only as ciphertext under a key in Secrets Manager. One tool decrypts them. The API,
the reports and the user interface stay pseudonymous, and the evidence endpoint redacts
personal keys whatever wrote them.

Person nodes in the ownership graph carry a pseudonym as their display name. The graph
shows that two vessels share a beneficial owner without naming them.

## What This System Must Not Be Used For

Repeating the model card, because it belongs here too: no automated enforcement, no
tracking of a person, no treating an output as evidence, and no operation without a
trained officer reviewing.

If you deploy this against real traffic, the review step is what makes it acceptable.
Removing it does not make the system faster. It makes it a different system.

## Guardrails, And Why They Are Set Low

A Bedrock guardrail runs on every model call, with personally identifiable information
masking off and the misconduct and prompt-attack filters at low.

That is a deliberate calibration, not laxity. This system's whole job is to describe
suspected smuggling and sanctions evasion. At higher settings the guardrail blocked
roughly three per cent of ordinary calls, each one costing a whole line of enquiry or a
report retry, and a denied-topic entry for personal surveillance blocked about a fifth of
legitimate investigator prompts. A filter that blocks the work it is supposed to protect
teaches operators to route around it.

The controls that actually constrain output are the evidence traceability check, the
allowed-action patterns and the human gate. The guardrail is a backstop, and blocks are
recorded rather than hidden.

## Bias And Fairness

The honest position: this has not been formally evaluated for bias, and the synthetic
scenarios were written by the same people who wrote the detectors.

Three things are worth naming.

**Flag and registry effects.** A vessel's flag state and registry record feed the
assessment. Flags of convenience correlate with jurisdiction, and an assessment that
leans on flag alone reproduces that. Reports must cite behavioural evidence, not identity
alone, but this is not mechanically enforced.

**Coverage is not behaviour.** AIS reception is worse far from shore and in some regions
than others. A gap in a poorly covered area is not the same signal as a gap in a well
covered one, and the detectors do not currently distinguish them. This is the failure mode
most likely to produce unfair attention, and it is unaddressed.

**Sanctions matching.** Name matching against sanctions lists produces false positives on
common names, which fall unevenly across naming conventions. Matches are evidence for an
officer, never a conclusion.

## Transparency And Recourse

Every investigation stores a manifest: code revision, prompt hashes and versions, model
and tier per node, attempts, escalations and evidence snapshot identifiers. Evidence is
frozen when the finding is made, so a report can be re-read years later against what was
known then. Every state change is in an append-only audit log with an actor and a time.

This means a decision can be reconstructed and challenged. That is the minimum a system
in this domain owes the people it affects.

## Environmental Cost

Nova models on Bedrock, chosen by tier rather than by defaulting everything to the
largest. Trace indexing is sampled at a tenth. The deployment idles at roughly $400 a
month and can be paused or destroyed between demonstrations.
