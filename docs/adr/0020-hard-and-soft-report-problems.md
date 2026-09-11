# ADR-0020: Hard and soft policy problems, and what a degraded branch may claim

Date: 2026-09-09. Status: accepted.

## Context

The report node checks every draft against `agents/shared/policy.py`: recommended actions
must match allowed patterns, and every indicator must trace to evidence a tool actually
produced. The documentation said the node retries once and fails closed on a second
violation. The code had already grown two exceptions to that, and neither was written
down, so the architecture page, the use-case page and the README all described a loop
that does not exist.

The first exception is balance. A report that states neither counter-indicators nor
information gaps reads as certainty the evidence rarely supports, so the policy flags it.
But failing an otherwise sound investigation over it destroys a good report to punish a
missing sentence, and the retry usually does not fix it: the model has nothing to add.

The second is the guardrail. Bedrock's prompt-attack filter reads the whole prompt,
including the correction list, and a correction list quoting policy violations trips it
often enough to matter. Without a third attempt, a guardrail block on the retry loses the
investigation.

Separately, a failed Investigator branch degrades to an empty finding that names the gap,
and the case continues on the other branch's evidence. The report model saw fluent
material and set its own confidence from it: a run with a failed behaviour branch
returned high confidence and high priority on identity evidence alone, with nothing in
the report saying half of it was missing.

## Decision

1. **Two classes of policy problem.** `SOFT_PROBLEMS` holds exactly one member today, the
   balance problem. `hard_problems()` is everything else. A hard problem surviving the
   retry fails the investigation closed, as before. A soft problem surviving the retry is
   accepted, and the report's caveats say the report states no counter-indicators or
   information gaps, so the officer reads it knowing that.
2. **A third attempt only after a guardrail block.** The normal path is two model calls.
   A block on the retry buys one more with a plainer correction list. Two blocks end it.
3. **A degraded branch caps the report in code.** `cap_for_degraded()` holds confidence to
   the findings' own confidence and no higher than moderate, holds priority to no higher
   than medium, names the missing branch in `information_gaps`, and adds a caveat saying
   confidence and priority are capped. It runs after schema validation and before the
   policy check, so a retry cannot argue it away, and it is pure, so it is unit-tested.

## Consequences

The three documentation pages now describe the loop that runs. An officer can tell a
capped report from a confident one by reading it, rather than by knowing which branch
failed. The cap is deliberately blunt: it does not try to judge whether the missing
branch mattered for this vessel, because that judgement needs the evidence that is
missing. A report whose confidence was already low or moderate is unaffected.

Adding a second soft problem needs care. Soft means an officer can still act on the
report while seeing the flaw in it; anything that would mislead belongs in the hard set.
