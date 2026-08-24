"""
Guardrails Stack — Bedrock Guardrail for pipeline safety.

Applies content filtering, PII detection, secret pattern blocking,
and denied topic policies across the pipeline:
  - Classifier input (prompt injection defense from ticket text)
  - KB retrieval output (PII filtering before Resolution Agent)
  - Resolution Agent output (secret/credential detection in PR descriptions)
"""

import aws_cdk as cdk
from aws_cdk import (
    aws_bedrock as bedrock,
    CfnOutput,
)
from constructs import Construct


class GuardrailStack(cdk.Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ── Bedrock Guardrail ──────────────────────────────────────────────────
        guardrail = bedrock.CfnGuardrail(
            self, "PipelineGuardrail",
            name="agentic-pipeline-guardrail",
            description="Safety guardrail for the agentic resolution pipeline — filters PII, secrets, harmful content, and prompt injection",
            blocked_input_messaging="Input blocked: content violates pipeline safety policy.",
            blocked_outputs_messaging="Output blocked: content violates pipeline safety policy.",

            # ── Content Filters (hate, sexual, violence, insults, misconduct) ──
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=[
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type="HATE",
                        input_strength="HIGH",
                        output_strength="HIGH",
                    ),
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type="SEXUAL",
                        input_strength="HIGH",
                        output_strength="HIGH",
                    ),
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type="VIOLENCE",
                        input_strength="MEDIUM",
                        output_strength="MEDIUM",
                    ),
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type="INSULTS",
                        input_strength="HIGH",
                        output_strength="HIGH",
                    ),
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type="MISCONDUCT",
                        input_strength="HIGH",
                        output_strength="HIGH",
                    ),
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        type="PROMPT_ATTACK",
                        input_strength="HIGH",
                        output_strength="NONE",
                    ),
                ],
            ),

            # ── Denied Topics ──────────────────────────────────────────────────
            topic_policy_config=bedrock.CfnGuardrail.TopicPolicyConfigProperty(
                topics_config=[
                    bedrock.CfnGuardrail.TopicConfigProperty(
                        name="malware-generation",
                        definition="Requests to generate malicious code, viruses, ransomware, exploit code, or backdoors",
                        type="DENY",
                    ),
                    bedrock.CfnGuardrail.TopicConfigProperty(
                        name="credential-exfiltration",
                        definition="Requests to extract, expose, or transmit credentials, API keys, passwords, or secrets to external systems",
                        type="DENY",
                    ),
                    bedrock.CfnGuardrail.TopicConfigProperty(
                        name="data-destruction",
                        definition="Requests to delete databases, drop tables, remove backups, or destroy production data without explicit safeguards",
                        type="DENY",
                    ),
                ],
            ),

            # ── Word Filters (secret patterns) ────────────────────────────────
            word_policy_config=bedrock.CfnGuardrail.WordPolicyConfigProperty(
                managed_word_lists_config=[
                    bedrock.CfnGuardrail.ManagedWordsConfigProperty(
                        type="PROFANITY",
                    ),
                ],
                words_config=[
                    bedrock.CfnGuardrail.WordConfigProperty(text="AKIA"),
                    bedrock.CfnGuardrail.WordConfigProperty(text="ASIA"),
                    bedrock.CfnGuardrail.WordConfigProperty(text="-----BEGIN RSA PRIVATE KEY-----"),
                    bedrock.CfnGuardrail.WordConfigProperty(text="-----BEGIN OPENSSH PRIVATE KEY-----"),
                    bedrock.CfnGuardrail.WordConfigProperty(text="-----BEGIN PGP PRIVATE KEY-----"),
                ],
            ),

            # ── PII / Sensitive Information ────────────────────────────────────
            sensitive_information_policy_config=bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
                pii_entities_config=[
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(
                        type="EMAIL", action="ANONYMIZE",
                    ),
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(
                        type="PHONE", action="ANONYMIZE",
                    ),
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(
                        type="US_SOCIAL_SECURITY_NUMBER", action="BLOCK",
                    ),
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(
                        type="CREDIT_DEBIT_CARD_NUMBER", action="BLOCK",
                    ),
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(
                        type="AWS_ACCESS_KEY", action="BLOCK",
                    ),
                    bedrock.CfnGuardrail.PiiEntityConfigProperty(
                        type="AWS_SECRET_KEY", action="BLOCK",
                    ),
                ],
                regexes_config=[
                    bedrock.CfnGuardrail.RegexConfigProperty(
                        name="aws-access-key-id",
                        description="AWS Access Key ID pattern (AKIA/ASIA prefix + 16 chars)",
                        pattern=r"(?:AKIA|ASIA)[A-Z0-9]{16}",
                        action="BLOCK",
                    ),
                    bedrock.CfnGuardrail.RegexConfigProperty(
                        name="generic-api-token",
                        description="Generic API token patterns (bearer tokens, long hex strings)",
                        pattern=r"(?:token|key|secret|password)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{20,}",
                        action="ANONYMIZE",
                    ),
                ],
            ),
        )

        # ── Guardrail Version (immutable snapshot) ─────────────────────────────
        guardrail_version = bedrock.CfnGuardrailVersion(
            self, "PipelineGuardrailVersion",
            guardrail_identifier=guardrail.attr_guardrail_id,
            description="Initial version with content, topic, word, and PII policies",
        )

        # ── Outputs ────────────────────────────────────────────────────────────
        CfnOutput(self, "GuardrailId",
                  value=guardrail.attr_guardrail_id,
                  description="Bedrock Guardrail ID — pass to InvokeModel and ApplyGuardrail calls")
        CfnOutput(self, "GuardrailVersion",
                  value=guardrail_version.attr_version,
                  description="Guardrail version number — use with GuardrailId")
        CfnOutput(self, "GuardrailArn",
                  value=guardrail.attr_guardrail_arn,
                  description="Guardrail ARN for IAM policies")

        # ── Exports ────────────────────────────────────────────────────────────
        self.guardrail_id = guardrail.attr_guardrail_id
        self.guardrail_version = guardrail_version.attr_version
        self.guardrail_arn = guardrail.attr_guardrail_arn
