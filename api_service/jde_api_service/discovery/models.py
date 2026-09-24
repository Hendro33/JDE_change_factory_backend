"""API models for Architect Environment Discovery (camelCase on the wire)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal, Optional
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator

from ..models.base import ApiModel
from . import capabilities

ConnectionMode = Literal["simulation", "live"]
DataSharingPolicy = Literal["metadata_only", "configuration_and_artifacts", "full"]
CheckState = Literal["ok", "failed", "unknown", "stale"]

_NAME = re.compile(r"^[A-Za-z0-9_]{1,40}$")
_TARGETS = {
    "table": re.compile(r"^F[0-9A-Z]{3,9}$"),
    "product_code/type": re.compile(r"^[0-9A-Z]{2,4}/[0-9A-Z]{2,3}$"),
    "application": re.compile(r"^[PRV][0-9A-Z]{3,9}$"),
    "object": re.compile(r"^[A-Z][0-9A-Z]{2,9}$"),
    "application|version": re.compile(r"^[PR][0-9A-Z]{3,9}\|[0-9A-Z_]{1,10}$"),
}


class ApprovedRead(ApiModel):
    """One approved read: a capability, the exact targets it may read,
    the columns it may return and the columns it may filter on."""

    capability_id: str
    targets: list[str] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)
    filter_fields: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> "ApprovedRead":
        cap = capabilities.get(self.capability_id)
        if cap is None:
            raise ValueError(f"unknown discovery capability {self.capability_id!r}")
        if cap.base_status == "unavailable":
            raise ValueError(f"{self.capability_id} is unavailable through AIS: {cap.unavailable_reason}")
        if cap.target_kind != "none":
            if not self.targets:
                raise ValueError(f"{self.capability_id}: at least one exact target is required")
            pattern = _TARGETS[cap.target_kind]
            bad = [t for t in self.targets if not pattern.match(t)]
            if bad:
                raise ValueError(f"{self.capability_id}: targets must be exact {cap.target_kind} names, got {bad}")
        for name in (*self.fields, *self.filter_fields):
            if not _NAME.match(name):
                raise ValueError(f"field names are column aliases only (letters, digits, _): {name!r}")
        if cap.fixed_fields:
            extra = set(self.fields) - set(cap.fixed_fields)
            if extra:
                raise ValueError(f"{self.capability_id} can only return {list(cap.fixed_fields)}; not {sorted(extra)}")
        if cap.capability_id == "table_browse" and not self.fields:
            raise ValueError("table_browse: list the approved columns -- there is no 'all columns'")
        return self


class DiscoveryWindow(ApiModel):
    starts_at: str
    ends_at: str

    @model_validator(mode="after")
    def _check(self) -> "DiscoveryWindow":
        try:
            start, end = datetime.fromisoformat(self.starts_at), datetime.fromisoformat(self.ends_at)
        except ValueError as exc:
            raise ValueError("discovery window times must be ISO-8601") from exc
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("discovery window times need a timezone")
        if end <= start:
            raise ValueError("discovery window must end after it starts")
        return self


class RequestLimits(ApiModel):
    max_records: int = Field(default=10, ge=1, le=capabilities.HARD_MAX_RECORDS)
    timeout_seconds: int = Field(default=15, ge=1, le=30)
    # Fixed for this increment; stored so the UI shows it.
    concurrent_requests: Literal[1] = 1


class JdeProfileConfig(ApiModel):
    """Everything about a company's discovery connection except the secret."""

    connection_mode: ConnectionMode = "simulation"
    ais_base_url: str
    environment: str
    environment_type: Literal["DEV"] = "DEV"
    role: str
    expected_application_release: str
    expected_tools_release: str
    path_code: str
    auth_method: Literal["ais_token_request"] = "ais_token_request"
    customer_contact: str = ""
    cnc_contact: str = ""
    network_route: str = ""
    isolation_evidence: str = ""
    routing_isolation_confirmed: bool = False
    privilege_statement: str = ""
    privilege_confirmed: bool = False
    # The documented AIS contract exposes neither the Tools release nor the
    # path code a session runs on; the customer's CNC attests them.
    runtime_attestation_confirmed: bool = False
    runtime_attestation_evidence: str = ""
    approved_reads: list[ApprovedRead] = Field(default_factory=list)
    discovery_window: Optional[DiscoveryWindow] = None
    limits: RequestLimits = Field(default_factory=RequestLimits)
    data_sharing_policy: DataSharingPolicy = "metadata_only"

    @field_validator("ais_base_url")
    @classmethod
    def _https(cls, v: str) -> str:
        parsed = urlparse(v.strip())
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("the AIS endpoint must be an https:// URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("the AIS endpoint must not carry credentials, a query or a fragment")
        return v.strip().rstrip("/")

    @field_validator("environment", "role", "path_code", "expected_application_release", "expected_tools_release")
    @classmethod
    def _explicit(cls, v: str) -> str:
        v = v.strip()
        if not v or v in {"*ALL", "*", "ALL"}:
            raise ValueError("must be named explicitly (no blank, *ALL or wildcard)")
        return v

    @model_validator(mode="after")
    def _one_read_per_capability(self) -> "JdeProfileConfig":
        ids = [r.capability_id for r in self.approved_reads]
        if len(ids) != len(set(ids)):
            raise ValueError("list each capability once in approved reads")
        return self


class JdeProfileUpdate(JdeProfileConfig):
    expected_revision: Optional[int] = None


class CredentialUpdate(ApiModel):
    username: str
    password: str
    expected_revision: Optional[int] = None


class CheckResult(ApiModel):
    state: CheckState = "unknown"
    checked_at: Optional[str] = None
    detail: str = ""
    profile_revision: Optional[int] = None
    # Environment verification only: expected / server defaults / session
    # context / attested, and each item's status with its source.
    facets: dict[str, Any] = {}


class CapabilityView(ApiModel):
    capability_id: str
    title: str
    description: str
    status: Literal["supported", "unverified", "unavailable"]
    status_detail: str = ""
    data_class: str
    target_kind: str
    approved: bool
    approved_targets: list[str] = []
    approved_fields: list[str] = []
    approved_filter_fields: list[str] = []
    alternative: str = ""


class JdeProfileView(ApiModel):
    """What the Admin screen (and ERP Landscape) sees. Never the secret."""

    company_id: str
    configured: bool
    revision: int = 0
    config: Optional[JdeProfileConfig] = None
    credential_configured: bool = False
    credential_username_masked: Optional[str] = None
    credential_storage: str = "none"
    health: dict[str, CheckResult] = {}
    capabilities: list[CapabilityView] = []
    discovery_enabled: bool = False
    enabled_by: Optional[str] = None
    enabled_at: Optional[str] = None
    disabled: bool = False
    disabled_by: Optional[str] = None
    disabled_at: Optional[str] = None
    enable_blockers: list[str] = []
    mode_label: str = ""
    live_allowed_by_deployment: bool = False
    updated_at: Optional[str] = None
    updated_by: Optional[str] = None


class SampleReadInput(ApiModel):
    capability_id: str


class ActionResult(ApiModel):
    outcome: str
    detail: str = ""
    profile: JdeProfileView
    evidence: Optional[dict[str, Any]] = None
    in_flight: list[dict[str, Any]] = []


class ActivityRow(ApiModel):
    id: int
    request_id: str
    company_id: str
    profile_revision: Optional[int] = None
    actor_user_id: Optional[str] = None
    actor_name: Optional[str] = None
    agent_run_id: Optional[str] = None
    story_id: Optional[str] = None
    operation: str
    target: str
    mode: Optional[str] = None
    started_at: str
    duration_ms: Optional[int] = None
    result_count: Optional[int] = None
    outcome: str
    reason: str = ""


# ---------------------------------------------------------------------
# Technical baseline artifacts and reference documents
# ---------------------------------------------------------------------
ArtifactKind = Literal["technical_export", "reference_document"]
ExportFormat = Literal["text", "c_source", "er_text", "omw_xml", "json", "markdown", "csv",
                       "par", "zip", "pdf", "docx", "other"]
RuntimeCorrespondence = Literal["matches_dev_runtime", "known_mismatch", "unknown"]


class ArtifactUpload(ApiModel):
    kind: ArtifactKind
    domain_id: Optional[str] = None
    object_name: str
    object_type: str
    export_format: ExportFormat
    customer_environment: str = ""
    path_code: str = ""
    release: str = ""
    source_location: str = ""
    repository: str = ""
    commit_ref: str = ""
    exported_at: str
    runtime_correspondence: RuntimeCorrespondence = "unknown"
    runtime_statement: str = ""
    runtime_stated_by: str = ""
    # reference documents
    doc_title: str = ""
    doc_revision: str = ""
    applies_to_releases: list[str] = []
    file_name: str
    content_base64: str

    @field_validator("object_name", "object_type", "file_name")
    @classmethod
    def _required(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("required")
        return v.strip()


class ArtifactView(ApiModel):
    artifact_id: str
    revision: int
    company_id: str
    domain_id: Optional[str] = None
    kind: ArtifactKind
    meta: dict[str, Any]
    sha256: str
    size_bytes: int
    extraction_status: Literal["supported", "unsupported"]
    extraction_note: str = ""
    uploaded_by: str
    uploaded_at: str
    compatibility: Optional[str] = None
    latest: bool = True


class DesignBaselineView(ApiModel):
    baseline_id: str
    story_id: str
    design_revision: int
    baseline_revision: int
    created_at: str
    trigger: str
    manifest: dict[str, Any]
    manifest_sha256: str
    status: str
    reassessment: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
