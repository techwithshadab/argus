**What this changes, and why**

**How you know it works**

Which test fails without this change? If the change is infrastructure, what did you see in
the synthesized template?

**Gates**

- [ ] `ruff check .` and `ruff format --check .`
- [ ] `pytest -q`
- [ ] `docker compose config -q`
- [ ] `make synth` (if `infra/` changed)
- [ ] `evals/node_evals.py --gate` (if a prompt or model changed)

**Documentation this invalidates**

Figures carry counts a test checks, the runbook lists every alarm, and the architecture
page names code paths. Tick nothing if nothing applies, but please look.

**Anything a reviewer should push back on**
