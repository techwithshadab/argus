"""Records survive a destroy unless someone deliberately deletes them (I1).

The runbook and the security page promise seven-year retention, while `retainData`
and `retainArchive` both defaulted to false: `make destroy` removed the cluster and
the archive bucket with no snapshot and no prompt, taking the evidence behind every
past investigation with them. Retention is now the default, deleting is the explicit
act, and the destroy path snapshots first.
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESTROY = (ROOT / "scripts/destroy.sh").read_text()
CDK_JSON = json.loads((ROOT / "infra/cdk/cdk.json").read_text())


def flag():
    """`_flag` alone, compiled from source: the stack module imports aws_cdk, which
    unit tests do not install."""
    src = (ROOT / "infra/cdk/stacks/data_stack.py").read_text()
    start = src.index("def _flag(")
    end = src.index("class DataStack(", start)
    ns: dict = {}
    exec(compile(src[start:end], "data-stack-flag", "exec"), ns)  # noqa: S102
    return ns["_flag"]


def test_an_unset_flag_means_keep_the_data():
    _flag = flag()
    assert _flag(None, default=True) is True
    assert _flag("", default=True) is True
    assert _flag("   ", default=True) is True


def test_deleting_requires_saying_so():
    _flag = flag()
    for value in ("false", "False", "no", "0", "off"):
        assert _flag(value, default=True) is False, value


def test_the_usual_spellings_of_true_are_accepted():
    _flag = flag()
    for value in ("1", "true", "TRUE", "yes", " Yes "):
        assert _flag(value, default=True) is True, value


def test_the_committed_context_keeps_the_records():
    assert CDK_JSON["context"]["retainData"] is True
    assert CDK_JSON["context"]["retainArchive"] is True


def test_the_stack_reads_both_flags_with_a_retaining_default():
    src = (ROOT / "infra/cdk/stacks/data_stack.py").read_text()
    assert '_flag(self.node.try_get_context("retainData"), default=True)' in src
    assert '_flag(self.node.try_get_context("retainArchive"), default=True)' in src
    # Deletion protection and the final snapshot follow that flag.
    assert "RemovalPolicy.SNAPSHOT if retain_data else RemovalPolicy.DESTROY" in src
    assert "deletion_protection=retain_data" in src


def test_destroy_snapshots_the_cluster_before_deleting_it():
    assert "create-db-cluster-snapshot" in DESTROY
    assert "wait db-cluster-snapshot-available" in DESTROY
    snapshot_at = DESTROY.index("create-db-cluster-snapshot")
    destroy_at = DESTROY.index("cdk destroy --all")
    assert snapshot_at < destroy_at, "the snapshot must be taken before the delete"


def test_destroy_states_the_deletion_rather_than_relying_on_a_default():
    assert "retainData=false" in DESTROY and "retainArchive=false" in DESTROY


def test_there_is_a_way_to_remove_the_compute_and_keep_the_records():
    assert "KEEP_DATA" in DESTROY
    assert "retainData=true" in DESTROY
    assert "destroy-keep-data:" in (ROOT / "Makefile").read_text()


def test_the_runbook_documents_a_restore():
    runbook = (ROOT / "docs/RUNBOOK.md").read_text()
    assert "### Restoring the database" in runbook
    assert "restore-db-cluster-from-snapshot" in runbook
    assert "argus-predestroy-" in runbook


def test_the_nag_evidence_matches_what_the_code_now_does():
    nag = (ROOT / "infra/cdk/stacks/nag.py").read_text()
    evidence = nag.split("AwsSolutions-RDS10", 1)[1][:400]
    assert "defaults to true" in evidence
