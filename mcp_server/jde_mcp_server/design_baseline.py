"""
The Architect's design baseline, as handed to the Functional (and future
Technical) Agent: the Architect's instructions together with the SAME
immutable evidence manifest the design was based on.

Written by api_service (discovery/baseline.py) to JDE_DESIGN_BASELINE_DIR,
read here. The story must be approved, the baseline must belong to the
story's own company (from its intake link) and its manifest checksum must
verify. The baseline is evidence for a snapshot in time: it grants no
permission, and the execution gate still re-checks everything live.
"""

from __future__ import annotations

import hashlib
import json
import os

from .backlog import require_approved
from .scope import company_for_story


class DesignBaselineUnavailable(RuntimeError):
    pass


def _dir() -> str:
    return os.environ.get("JDE_DESIGN_BASELINE_DIR", "./design_baselines")


def get_design_baseline(story_id: str) -> dict:
    require_approved(story_id)
    company_id = company_for_story(story_id)
    path = os.path.join(_dir(), f"{story_id}.json")
    if not os.path.exists(path):
        raise DesignBaselineUnavailable(
            f"no design baseline for {story_id}: the Architect has not recorded an evidence manifest -- "
            "do not proceed as if the environment had been investigated")
    with open(path, encoding="utf-8") as f:
        package = json.load(f)
    if package.get("company_id") != company_id or package.get("story_id") != story_id:
        raise DesignBaselineUnavailable("the design baseline does not belong to this story's company -- refusing")
    blob = json.dumps(package.get("evidence_manifest"), sort_keys=True)
    if hashlib.sha256(blob.encode()).hexdigest() != package.get("manifest_sha256"):
        raise DesignBaselineUnavailable("the design baseline's checksum does not verify -- refusing")
    return package
