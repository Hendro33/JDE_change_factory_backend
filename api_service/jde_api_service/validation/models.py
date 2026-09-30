from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Environment(Input):
    revision: int = 0
    name: str = Field(min_length=1, max_length=120)
    stage: Literal["DEV", "QA", "UAT", "PREPROD", "PROD"] = "DEV"
    application: str = "JD Edwards"
    ais_url: str = ""
    web_url: str = ""
    browser_environment_selector: str = ""
    browser_role_selector: str = ""
    jde_environment: str = Field(min_length=1, max_length=80)
    jde_role: str = "*ALL"
    username: str = ""
    password: str = Field(default="", exclude=True)
    ca_pem: str = Field(default="", exclude=True)
    certificate_sha: str = ""
    enabled: bool = False
    allow_writes: bool = False
    side_effects_isolated: bool = False
    routes: list[Literal["browser", "computer_use", "ais", "manual"]] = ["manual"]
    allowed_ais_paths: list[str] = []
    redaction_selectors: list[str] = []
    parameters: dict[str, str] = {}
    prerequisites: str = ""
    cleanup: str = ""
    timeout_seconds: int = Field(default=300, ge=30, le=1800)
    max_actions: int = Field(default=80, ge=1, le=200)
    max_agent_calls: int = Field(default=5, ge=1, le=50)

    @field_validator("ais_url", "web_url")
    @classmethod
    def https_only(cls, value):
        if not value:
            return value
        p = urlparse(value)
        if p.scheme != "https" or not p.hostname or p.username or p.password or p.query or p.fragment:
            raise ValueError("Use an HTTPS endpoint without credentials, query or fragment")
        return value.rstrip("/")

    @field_validator("allowed_ais_paths")
    @classmethod
    def bounded_paths(cls, values):
        for v in values:
            if not v.startswith("/") or any(c in v for c in ("..", ":", "?", "#", "%", "\\")) or v.startswith("//"):
                raise ValueError("AIS paths must be exact paths below the configured endpoint")
            if v in ("/tokenrequest", "/tokenrequest/logout"):
                raise ValueError("Authentication is managed by JADE")
        return list(dict.fromkeys(values))


class Policy(Input):
    revision: int = 0
    freshness_days: int = Field(default=30, ge=1, le=365)
    evidence_days: int = Field(default=365, ge=30, le=3650)
    require_independent_review: bool = False
    allow_release_exceptions: bool = False
    production_enabled: bool = False
    jira_enabled: bool = False
    jira_issue_type: str = "Bug"
    instructions: str = Field(default="", max_length=10000)


class Step(Input):
    id: str = Field(min_length=1, max_length=60, pattern=r"^[A-Za-z0-9_-]+$")
    action: str = Field(min_length=1, max_length=2000)
    expected: str = Field(min_length=1, max_length=2000)
    operation: Literal["manual", "open", "click", "fill", "select", "press", "observe", "ais"] = "manual"
    target: str = Field(default="", max_length=500)
    value: str = Field(default="", max_length=4000)
    path: str = ""
    method: Literal["GET", "POST"] = "POST"
    body: dict[str, Any] = {}
    assertion: Literal["contains", "equals", "exists", "human"] = "human"
    expected_value: Any = None
    result_path: str = ""
    mutates: bool = False
    wait_seconds: int = Field(default=0, ge=0, le=30)

    @model_validator(mode="after")
    def meaningful_assertion(self):
        if self.assertion == "contains" and (self.expected_value is None or not str(self.expected_value).strip()):
            raise ValueError("A contains assertion needs a non-empty expected value")
        if self.assertion == "equals" and self.expected_value is None:
            raise ValueError("An equality assertion needs an explicit expected value")
        return self


class Scenario(Input):
    revision: int = 0
    title: str = Field(min_length=1, max_length=200)
    purpose: str = Field(default="", max_length=4000)
    story_id: str = ""
    criteria: list[str] = []
    process_refs: list[str] = []
    domain_id: str = ""
    tags: list[str] = []
    prerequisites: str = ""
    cleanup: str = ""
    route: Literal["manual", "browser", "computer_use", "ais"] = "manual"
    risk: Literal["low", "medium", "high"] = "medium"
    mandatory: bool = True
    steps: list[Step] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_steps(self):
        if len({s.id for s in self.steps}) != len(self.steps):
            raise ValueError("Step IDs must be unique")
        if self.route != "manual" and any(s.operation == "manual" for s in self.steps):
            raise ValueError("Automated scenarios need an execution binding for every step")
        if self.route == "ais" and any(s.operation != "ais" for s in self.steps):
            raise ValueError("AIS scenarios require AIS steps")
        if self.route in ("browser", "computer_use") and any(s.operation == "ais" for s in self.steps):
            raise ValueError("Use a separate ordered AIS scenario for setup or verification")
        return self


class Selection(Input):
    scenario_id: str
    version: int = Field(ge=1)
    mandatory: bool = True
    rationale: str = ""
    depends_on: list[str] = []


class Plan(Input):
    revision: int = 0
    title: str = Field(min_length=1, max_length=200)
    story_ids: list[str] = Field(min_length=1, max_length=100)
    environment_ids: list[str] = Field(min_length=1, max_length=20)
    selections: list[Selection] = Field(min_length=1, max_length=200)
    exclusions: str = ""
    uncovered_criteria_reason: str = ""
    owner_id: str = ""


class Revision(Input):
    revision: int
    note: str = Field(default="", max_length=4000)


class Deployment(Input):
    environment_id: str
    story_ids: list[str] = Field(min_length=1, max_length=100)
    build: str = Field(min_length=1, max_length=200)
    evidence: str = Field(min_length=1, max_length=4000)


class Start(Input):
    plan_id: str
    version: int = Field(ge=1)
    environment_id: str
    deployment_id: str
    idempotency_key: str = Field(min_length=8, max_length=120)
    selected: list[str] = []
    retest_of: str = ""
    assignee_id: str = ""
    production_approval_id: str = ""


class ManualResult(Input):
    revision: int
    steps: dict[str, Literal["passed", "failed", "needs_review", "skipped"]]
    observations: dict[str, str]
    note: str = Field(default="", max_length=4000)


class Review(Input):
    revision: int
    note: str = Field(min_length=1, max_length=4000)


class Assessment(Review):
    steps: dict[str, Literal["passed", "failed"]]


class Defect(Input):
    run_id: str
    scenario_id: str
    severity: Literal["critical", "high", "medium", "low"] = "high"
    note: str = Field(default="", max_length=4000)


class EvidenceUpload(Input):
    name: str = Field(min_length=1, max_length=180)
    mime: Literal["image/png", "image/jpeg", "application/pdf", "text/plain"]
    data: str = Field(max_length=14000000)


class Signoff(Input):
    version: int
    coverage_hash: str
    note: str = Field(min_length=1, max_length=4000)
    exceptions: list[str] = []


class Release(Input):
    plan_id: str
    version: int
    coverage_hash: str
    decision: Literal["approve", "hold"]
    note: str = Field(min_length=1, max_length=4000)
    operational_readiness: str = Field(min_length=1, max_length=4000)
    exception_reason: str = Field(default="", max_length=4000)


class ProductionApproval(Input):
    plan_id: str
    version: int
    environment_id: str
    deployment_id: str
    note: str = Field(min_length=1, max_length=4000)


class AgentRequest(Input):
    role: Literal["test-designer", "regression-analyst", "result-assessor", "validation-summariser"]
    story_id: str = ""
    plan_id: str = ""
    run_id: str = ""
    note: str = Field(default="", max_length=4000)
