---
name: ci-local
description: Run the exact CI gates locally (ruff check, ruff format --check, pytest, docker compose config, cdk synth) and report pass/fail per gate. Use before declaring any change done, or when asked to "run CI", "check the build", or "verify".
---

Run every gate from the repo root, in this order, and do not stop at the first failure. Collect all results, then report a table of gate → pass/fail with the failing output for any red gate.

```bash
uvx ruff@0.16.5 check .
uvx ruff@0.16.5 format --check .
uv run --python 3.12 --with pytest==9.1.1 --with pyyaml==6.0.3 --with pydantic==2.13.5 python -m pytest -q
docker compose config -q
```

Then, only if `infra/` or `scripts/deploy.sh` changed (or the user asks), run the synth gate. It needs `cdk` on PATH and the CDK venv:

```bash
cd infra/cdk && ( [ -d .venv ] || python3 -m venv .venv ) && . .venv/bin/activate && pip install -q -r requirements.txt && CDK_DEFAULT_ACCOUNT=123456789012 CDK_DEFAULT_REGION=us-east-1 cdk synth --all -q
```

Rules:
- The pytest gate deliberately installs only the four packages CI installs. If a test fails on import, the fix is to move it behind `@pytest.mark.integration`, not to add the dependency.
- If `ruff format --check` fails, run `uvx ruff@0.16.5 format .` and re-check, then mention which files were reformatted.
- Do not skip `docker compose config -q`; a YAML or interpolation error there fails CI.
- CI treats `cdk synth` as non-blocking (`|| true`), but a local synth failure is still a real bug. Report it as a failure.
