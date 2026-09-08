---
status: accepted
---
# Model traffic stays inside AWS in production

Prompts carry vessel tracks, beneficial-owner names and sanctions text, and the model layer is provider-neutral (`MODEL_PROVIDER` = bedrock | anthropic | openai | gemini), so we needed a rule for where that data may go. Decision: production runs on Bedrock only. The prod deployment refuses any other provider at synth time (a CDK guard on `modelProvider`), and model traffic uses the Bedrock VPC endpoint with no NAT path. Anthropic, OpenAI and Gemini remain available in development and for evals, which is where cross-provider comparison of prompts and detectors happens before a Bedrock model is chosen for production.

## Considered options

- Any provider under a DPA with zero data retention, allowlisted and logged, with personal data redacted before egress. Rejected because the data the Investigator reasons over (ownership chains, sanctions listings) cannot be redacted without removing its purpose, and because "never leaves the account" is the answer defence and coastguard buyers expect. Revisit only if a customer explicitly requires a non-AWS model or Bedrock cannot serve a needed capability; if reopened, allow it per node (Watch and Tasking handle no personal data) rather than globally.
- No restriction: indefensible once real registry and sanctions data is ingested.

Bedrock models are further limited to vendors served on-demand by Bedrock itself (`anthropic` and `amazon` by default, `bedrockModelVendors` context to widen). Bedrock Marketplace and imported models need a SageMaker endpoint with hourly cost and are refused at synth time and by IAM.

## Consequences

Model tiers in production are Amazon Nova only (decision of 4 Sep 2026: no Claude models, lowest cost at acceptable quality): Nova Lite for the fast tier, Nova 2 Lite for standard, Nova Pro for strong, Nova Premier as the step-up. The IAM allowlist grants the `amazon` vendor alone; widen `bedrockModelVendors` deliberately if another first-party vendor (e.g. Meta Llama 4) is ever wanted. No redaction pipeline is needed. The provider switch is a development and evaluation feature, not a production control.
