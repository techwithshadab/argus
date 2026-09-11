"""No application container runs as root (I20).

The task roles are the real boundary on AWS; this is the layer beneath them. A container
compromised through an application bug should not also be root inside its own
filesystem. The vendor images for Grafana, Loki, Tempo and Prometheus set their own
non-root users upstream, so only the images this project writes are checked here.
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Images built from this project's own Dockerfiles.
OWN = (
    "services/api/Dockerfile",
    "services/ais-replay/Dockerfile",
    "services/archiver/Dockerfile",
    "mcp-servers/Dockerfile",
    "agents/Dockerfile",
    "services/ui/Dockerfile",
)


def test_every_own_dockerfile_is_tracked_here():
    tracked = subprocess.run(
        ["git", "ls-files", "*Dockerfile*"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    own = [f for f in tracked if "observability" not in f]
    assert sorted(own) == sorted(OWN), "a new image needs a non-root user and this list"


def test_no_application_image_runs_as_root():
    for rel in OWN:
        src = (ROOT / rel).read_text()
        assert "USER " in src or "nginx-unprivileged" in src, rel


def test_the_user_is_set_before_the_command():
    """A USER after CMD does nothing."""
    for rel in OWN:
        lines = (ROOT / rel).read_text().splitlines()
        user = next((i for i, ln in enumerate(lines) if ln.startswith("USER ")), None)
        cmd = next((i for i, ln in enumerate(lines) if ln.startswith("CMD")), None)
        if user is not None and cmd is not None:
            assert user < cmd, rel


def test_the_ui_listens_on_an_unprivileged_port():
    """A non-root process cannot bind port 80."""
    assert "listen 8080;" in (ROOT / "services/ui/nginx.conf").read_text()
    assert "EXPOSE 8080" in (ROOT / "services/ui/Dockerfile").read_text()


def test_the_stack_and_compose_agree_with_that_port():
    stack = (ROOT / "infra/cdk/stacks/platform_stack.py").read_text()
    ui = stack.split("_, ui_svc = service(", 1)[1].split(")", 1)[0]
    assert "8080" in ui
    assert 'ec2.Port.tcp(8080),\n            "public ALB to UI"' in stack
    # The host port the docs name is unchanged.
    assert '"8088:8080"' in (ROOT / "docker-compose.yml").read_text()


def test_the_documented_local_url_still_works():
    for doc in ("README.md", "docs/RUNBOOK.md"):
        assert "localhost:8088" in (ROOT / doc).read_text(), doc
