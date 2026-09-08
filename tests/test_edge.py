"""The watch floor's edge (ADR-0018): sign-in on every public listener, WAF, no plain HTTP."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EDGE = (ROOT / "infra/cdk/stacks/edge.py").read_text()
PLATFORM = (ROOT / "infra/cdk/stacks/platform_stack.py").read_text()
HANDLER = (ROOT / "infra/cdk/lambdas/selfsigned/handler.py").read_text()


def test_public_listeners_are_https_and_signed_in():
    assert "default_action=elbv2.ListenerAction.redirect(" in PLATFORM
    assert "add_redirect(" not in PLATFORM
    # the target groups keep their pre-hardening ids: no ECS service replacement
    assert '"UiGroup"' in PLATFORM and '"GrafanaGroup"' in PLATFORM
    assert "watch_host" in PLATFORM and "dns_name_lower" in PLATFORM
    assert PLATFORM.count("sign_in.authenticate(") == 2  # UI and Grafana
    assert 'bearer_bypass(https, "ApiBearer", ui_tg)' in PLATFORM
    assert "allow_oidc_egress(public_alb)" in PLATFORM
    assert 'public_web_acl(self, "WebAcl", alb=public_alb)' in PLATFORM


def test_api_is_told_to_verify_the_balancers_token():
    assert '"OFFICER_AUTH": "oidc"' in PLATFORM
    assert '"OIDC_SIGNER": public_alb.load_balancer_arn' in PLATFORM
    assert '"OIDC_ISSUER": sign_in.pool.user_pool_provider_url' in PLATFORM
    assert "argus-agent-watch,argus-agent-orchestrator,argus-operator" in PLATFORM
    assert 'role_name="argus-operator"' in PLATFORM


def test_web_acl_rules():
    for rule in (
        "RateLimitPerIp",
        "AWSManagedRulesAmazonIpReputationList",
        "AWSManagedRulesCommonRuleSet",
        "AWSManagedRulesKnownBadInputsRuleSet",
    ):
        assert rule in EDGE
    assert 'scope="REGIONAL"' in EDGE
    assert "CfnWebACLAssociation" in EDGE


def test_sign_in_is_admin_provisioned_and_confidential():
    assert "self_sign_up_enabled=False" in EDGE
    assert "generate_secret=True" in EDGE
    assert "authorization_code_grant=True" in EDGE
    assert "FeaturePlan.LITE" in EDGE
    assert 'path_patterns(["/api/*"])' in EDGE
    assert 'http_header("Authorization", ["Bearer *"])' in EDGE


def test_self_signed_certificate_is_generated_at_deploy_time():
    # The key is made inside the Lambda and imported; nothing is passed as a property.
    assert 'properties={"DnsName": dns_name}' in EDGE
    assert 'issuer.issue("PublicCert", public_dns)' in PLATFORM
    assert '"DnsNameLower": dns_name.lower()' in HANDLER
    assert 'issuer.issue("InternalCert", self.internal_alb_dns)' in PLATFORM
    assert 'parameter_name="/argus/internal-ca"' in PLATFORM
    assert "certificates=internal_certs" in PLATFORM
    assert "rsa.generate_private_key" in HANDLER
    assert "acm.import_certificate" in HANDLER
    assert "acm.delete_certificate" in HANDLER
