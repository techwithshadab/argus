"""The watch floor's edge: TLS, sign-in and a web ACL on the public load balancer (ADR-0018).

Cognito user pool (officers are created by an operator, no self sign-up) and the ALB's own
authenticate action on every HTTPS listener: the browser never reaches the UI or Grafana
without a session, and the ALB forwards the signed id token (`x-amzn-oidc-data`) that the
API turns into the officer's identity (services/api/officerauth.py). Requests that carry a
bearer token on `/api/*` skip the login and are checked by the API against IAM instead
(evals, operator tooling through the `argus-operator` role). AWS WAF in front: managed
rule groups plus a per-IP rate limit.

The HTTPS listener needs a certificate. `-c uiCertificateArn` (ACM, a domain the deployer
controls) when there is one; otherwise a self-signed certificate is generated at deploy
time by a small Lambda (lambdas/selfsigned) and imported into ACM. The same issuer gives
the internal load balancer its certificate for the API listener; agents trust it through
the SSM parameter that carries the public half."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aws_cdk import (
    BundlingOptions,
    CustomResource,
    DockerImage,
    Duration,
    RemovalPolicy,
)
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_elasticloadbalancingv2_actions as elbv2_actions
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_wafv2 as wafv2
from aws_cdk import custom_resources as cr
from constructs import Construct

LAMBDAS = Path(__file__).resolve().parents[1] / "lambdas"
SESSION = Duration.hours(8)  # one watch shift
RATE_LIMIT_PER_5_MIN = 2000


@dataclass(frozen=True)
class IssuedCertificate:
    certificate_arn: str
    certificate_pem: str  # the public half, for clients that must trust the listener
    dns_name_lower: str  # Cognito callback URLs must be lowercase; ALB names are not


class CertificateIssuer(Construct):
    """One Lambda-backed provider that generates self-signed certificates at deploy time
    and imports them into ACM (lambdas/selfsigned); `issue()` once per load balancer.
    The private key exists only inside the Lambda and ACM."""

    def __init__(self, scope: Construct, cid: str):
        super().__init__(scope, cid)

        def log_group(name: str) -> logs.LogGroup:
            return logs.LogGroup(
                self,
                name,
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            )

        fn = lambda_.Function(
            self,
            "Fn",
            runtime=lambda_.Runtime.PYTHON_3_14,
            architecture=lambda_.Architecture.X86_64,
            handler="handler.on_event",
            timeout=Duration.minutes(2),
            memory_size=512,
            log_group=log_group("Logs"),
            code=lambda_.Code.from_asset(
                str(LAMBDAS / "selfsigned"),
                bundling=BundlingOptions(
                    # Docker Hub's slim image instead of the SAM build image: same wheels,
                    # a tenth of the pull.
                    image=DockerImage.from_registry("python:3.14-slim"),
                    platform="linux/amd64",
                    command=[
                        "bash",
                        "-c",
                        "pip install --no-cache-dir -r requirements.txt -t /asset-output"
                        " && cp handler.py /asset-output/",
                    ],
                ),
            ),
        )
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "acm:ImportCertificate",
                    "acm:GetCertificate",
                    "acm:DeleteCertificate",
                    "acm:AddTagsToCertificate",
                ],
                resources=["*"],  # the ARN does not exist before ImportCertificate
            )
        )
        provider = cr.Provider(
            self, "Provider", on_event_handler=fn, log_group=log_group("ProviderLogs")
        )
        self.service_token = provider.service_token

    def issue(self, cid: str, dns_name: str) -> IssuedCertificate:
        resource = CustomResource(
            self,
            cid,
            service_token=self.service_token,
            properties={"DnsName": dns_name},
        )
        return IssuedCertificate(
            resource.get_att_string("CertificateArn"),
            resource.get_att_string("CertificatePem"),
            resource.get_att_string("DnsNameLower"),
        )


class OfficerSignIn(Construct):
    """Cognito user pool + hosted domain + the ALB's app client."""

    def __init__(
        self,
        scope: Construct,
        cid: str,
        *,
        account: str,
        callback_urls: list[str],
        logout_urls: list[str],
        initial_officer_email: str = "",
        mfa_required: bool = False,
    ):
        super().__init__(scope, cid)
        self.pool = cognito.UserPool(
            self,
            "Pool",
            user_pool_name="argus-officers",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=True)
            ),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
                temp_password_validity=Duration.days(3),
            ),
            mfa=cognito.Mfa.REQUIRED if mfa_required else cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(otp=True, sms=False),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            feature_plan=cognito.FeaturePlan.LITE,  # no per-MAU charge at this scale
            deletion_protection=False,  # `make destroy` must be able to remove it
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.domain = self.pool.add_domain(
            "Domain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=f"argus-{account}"
            ),
        )
        self.client = self.pool.add_client(
            "Alb",
            user_pool_client_name="argus-watch-floor",
            generate_secret=True,  # ALB authentication needs a confidential client
            auth_flows=cognito.AuthFlow(user_srp=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                ],
                callback_urls=callback_urls,
                logout_urls=logout_urls,
            ),
            supported_identity_providers=[
                cognito.UserPoolClientIdentityProvider.COGNITO
            ],
            prevent_user_existence_errors=True,
            access_token_validity=Duration.hours(1),
            id_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(1),
        )
        if initial_officer_email:
            # Cognito emails a temporary password; the first sign-in sets the real one.
            cognito.CfnUserPoolUser(
                self,
                "InitialOfficer",
                user_pool_id=self.pool.user_pool_id,
                username=initial_officer_email,
                desired_delivery_mediums=["EMAIL"],
                user_attributes=[
                    cognito.CfnUserPoolUser.AttributeTypeProperty(
                        name="email", value=initial_officer_email
                    ),
                    cognito.CfnUserPoolUser.AttributeTypeProperty(
                        name="email_verified", value="true"
                    ),
                ],
            )

    def authenticate(self, next_action: elbv2.ListenerAction) -> elbv2.ListenerAction:
        """The listener's default action: sign in, then `next_action`."""
        return elbv2_actions.AuthenticateCognitoAction(
            user_pool=self.pool,
            user_pool_client=self.client,
            user_pool_domain=self.domain,
            next=next_action,
            session_timeout=SESSION,
            on_unauthenticated_request=elbv2.UnauthenticatedAction.AUTHENTICATE,
        )


def bearer_bypass(
    listener: elbv2.ApplicationListener,
    cid: str,
    target: elbv2.IApplicationTargetGroup,
    priority: int = 10,
) -> None:
    """`/api/*` with an `Authorization: Bearer` header skips the browser sign-in; the API
    then verifies that token against IAM (OFFICER_AUTH=oidc) and admits only allowed roles."""
    listener.add_action(
        cid,
        priority=priority,
        conditions=[
            elbv2.ListenerCondition.path_patterns(["/api/*"]),
            elbv2.ListenerCondition.http_header("Authorization", ["Bearer *"]),
        ],
        action=elbv2.ListenerAction.forward([target]),
    )


def public_web_acl(
    scope: Construct, cid: str, *, alb: elbv2.ApplicationLoadBalancer
) -> wafv2.CfnWebACL:
    """Regional web ACL on the public ALB: AWS managed rule groups and a per-IP rate limit.
    Counted requests show up as AWS/WAFV2 metrics; blocked requests alarm in alarms.py."""

    def managed(name: str, priority: int, metric: str) -> wafv2.CfnWebACL.RuleProperty:
        return wafv2.CfnWebACL.RuleProperty(
            name=metric,
            priority=priority,
            override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
            statement=wafv2.CfnWebACL.StatementProperty(
                managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                    vendor_name="AWS", name=name
                )
            ),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                sampled_requests_enabled=True,
                cloud_watch_metrics_enabled=True,
                metric_name=metric,
            ),
        )

    rate = wafv2.CfnWebACL.RuleProperty(
        name="RateLimitPerIp",
        priority=0,
        action=wafv2.CfnWebACL.RuleActionProperty(block={}),
        statement=wafv2.CfnWebACL.StatementProperty(
            rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                limit=RATE_LIMIT_PER_5_MIN, aggregate_key_type="IP"
            )
        ),
        visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
            sampled_requests_enabled=True,
            cloud_watch_metrics_enabled=True,
            metric_name="RateLimitPerIp",
        ),
    )
    acl = wafv2.CfnWebACL(
        scope,
        cid,
        name="argus-watch-floor",
        scope="REGIONAL",
        default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
        rules=[
            rate,
            managed("AWSManagedRulesAmazonIpReputationList", 1, "IpReputation"),
            managed("AWSManagedRulesCommonRuleSet", 2, "CommonRuleSet"),
            managed("AWSManagedRulesKnownBadInputsRuleSet", 3, "KnownBadInputs"),
        ],
        visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
            sampled_requests_enabled=True,
            cloud_watch_metrics_enabled=True,
            metric_name="argus-watch-floor",
        ),
    )
    wafv2.CfnWebACLAssociation(
        scope,
        f"{cid}Association",
        resource_arn=alb.load_balancer_arn,
        web_acl_arn=acl.attr_arn,
    )
    return acl


def allow_oidc_egress(alb: elbv2.ApplicationLoadBalancer) -> None:
    """The ALB talks to Cognito's token and userinfo endpoints itself."""
    alb.connections.allow_to_any_ipv4(ec2.Port.tcp(443), "Cognito OIDC endpoints")
