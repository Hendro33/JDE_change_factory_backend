"""Durable queue consumer with leases, environment locks and conservative recovery.

The web lifecycle starts a consumer; additional replicas are safe because claims
use the existing database write lock. Run `python -m jde_api_service.validation.worker`
to place a consumer on a network that can reach JDE, sharing the database/blobs.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time

from ..persistence.db import connection, ensure_schema
from ..services import membership_service, write_pause
from . import agents, runners, service as s

LOG = logging.getLogger(__name__)
LEASE = 45


def recover():
    with connection(immediate=True):
        for run in s.store("runs").list_all():
            if run["status"] not in ("running", "preflight", "stop_requested") or run["lease_until"] >= time.time():
                continue
            run.update(status="execution_error", reason="Execution lease expired. Review partial evidence before retesting",
                       finished_at=s.now(), reconciliation_required=bool(run.get("inflight_write")),
                       revision=run["revision"] + 1)
            for a in run["attempts"]:
                if a["selected"] and a["outcome"] == "not_run":
                    a["outcome"] = "needs_review" if run["reconciliation_required"] else "blocked"
            s.store("runs").put(run["id"], run)
        for job in s.store("agent_jobs").list_all():
            if job["status"] == "running" and job["lease_until"] < time.time():
                job.update(status="failed", error="Agent worker interrupted; request a new proposal", finished_at=s.now())
                s.store("agent_jobs").put(job["id"], job)


def claim(owner):
    if write_pause.status() is not None:
        return None
    recover()
    with connection(immediate=True):
        all_runs = s.store("runs").list_all()
        locked = {(r["company_id"], r["environment_id"]) for r in all_runs
                  if r["status"] in ("preflight", "running", "stop_requested", "awaiting_input") or r["reconciliation_required"]}
        for run in sorted(all_runs, key=lambda r: r["created_at"]):
            if run["status"] != "queued" or (run["company_id"], run["environment_id"]) in locked:
                continue
            run.update(status="preflight", lease_owner=owner, lease_until=time.time() + LEASE,
                       started_at=s.now(), revision=run["revision"] + 1)
            s.store("runs").put(run["id"], run)
            return "runs", run
        for job in sorted(s.store("agent_jobs").list_all(), key=lambda j: j["created_at"]):
            if job["status"] == "queued":
                job.update(status="running", lease_owner=owner, lease_until=time.time() + LEASE)
                s.store("agent_jobs").put(job["id"], job)
                return "agent_jobs", job
    return None


def renew(kind, job, owner):
    with connection(immediate=True):
        current = s.get(kind, job["id"], job["company_id"])
        if current["lease_owner"] == owner and current["status"] in s.ACTIVE:
            current["lease_until"] = time.time() + LEASE
            s.store(kind).put(job["id"], current)


def execute(run):
    company, key, owner = run["company_id"], run["id"], run["lease_owner"]
    env = s.get("environments", run["environment_id"], company)
    deadline = time.monotonic() + env["timeout_seconds"]

    def guard():
        current = s.get("runs", key, company)
        if current["lease_owner"] != owner or current["status"] not in ("preflight", "running"):
            raise runners.Blocked("Execution stopped or the worker lease changed")
        if write_pause.status() is not None:
            raise runners.Blocked("JADE is paused for maintenance")
        if "test_manager" not in membership_service.roles_for(run["initiated_by"], company):
            raise runners.Blocked("The initiating Test Manager no longer has execution authority")
        if time.monotonic() >= deadline:
            raise runners.Blocked("Run time budget exhausted")
        if not env["enabled"] or s.get("environments", env["id"], company)["revision"] != env["revision"]:
            raise runners.Blocked("Environment is disabled or its configuration changed")
        if env["stage"] == "PROD":
            approval = s.get("production_approvals", run["production_approval_id"], company)
            if approval["expires_at"] < s.now() or "product_manager" not in membership_service.roles_for(approval["actor"], company):
                raise runners.Blocked("Production authorisation expired or its approver lost release authority")
        if s.policy(company)["revision"] != run["policy_revision"]:
            raise runners.Blocked("Validation policy changed")
        plan = s.version(s.get("plans", run["plan_id"], company), run["version"])
        s.assert_current_plan(company, plan)
        dep = s.latest_deployment(company, env["id"], plan["body"]["story_ids"])
        if not dep or dep["id"] != run["deployment_id"]:
            raise runners.Blocked("The deployment changed during testing")

    def record(scenario_id, event):
        with connection(immediate=True):
            current = s.get("runs", key, company)
            if current["lease_owner"] != owner or current["status"] not in ("running", "stop_requested"):
                raise runners.Blocked("The execution lease is no longer owned by this worker")
            a = next(a for a in current["attempts"] if a["scenario_id"] == scenario_id)
            if event["event"] == "agent_call" and sum(e["event"] == "agent_call" for e in current["events"]) >= env.get("max_agent_calls", 5):
                raise runners.Blocked("This run's agent invocation budget is exhausted")
            if event["event"] == "dispatch" and sum(e["event"] == "dispatch" for e in current["events"]) >= env["max_actions"]:
                raise runners.Blocked("This run's action budget is exhausted")
            event = {**event, "scenario_id": scenario_id, "at": s.now(), "sequence": len(current["events"]) + 1}
            current["events"].append(event)
            if event["event"] == "dispatch":
                current["inflight_write"] = event["may_write"]
            if event["event"] == "step":
                a["steps"].append(event)
                a["evidence"] = list(dict.fromkeys([*a["evidence"], *event.get("evidence", [])]))
                current["inflight_write"] = False
            current["revision"] += 1
            s.store("runs").put(key, current)

    try:
        guard()
        if run["environment_revision"] != env["revision"]:
            raise runners.Blocked("Environment changed since this run was queued")
        selected = [a for a in run["attempts"] if a["selected"]]
        for a in selected:
            if a["body"]["route"] not in env["routes"]:
                raise runners.Blocked("A selected execution route is disabled for this environment")
            for step in a["body"]["steps"]:
                runners.check_step(env, runners.substitute(step, env["parameters"]))
        if any(a["body"]["route"] != "manual" for a in selected):
            identity = runners.test_connection(env)
        else:
            identity = {"status": "manual", "detail": "Assigned tester must verify environment and test data"}
        with connection(immediate=True):
            current = s.get("runs", key, company)
            if current["status"] != "preflight":
                raise runners.Blocked("Execution stopped during preflight")
            current.update(status="running", identity_check=identity, revision=current["revision"] + 1)
            s.store("runs").put(key, current)
        waiting = False
        for a in selected:
            guard()
            if a["outcome"] != "not_run":
                continue
            current = s.get("runs", key, company)
            prerequisites = [p for p in current["attempts"] if p["scenario_id"] in a["depends_on"]]
            prerequisite_outcomes = [p["outcome"] for p in prerequisites]
            if any(p["outcome"] == "not_run" or (p["outcome"] == "needs_review" and p["body"]["route"] != "manual") for p in prerequisites):
                waiting = True
                continue
            if any(o != "passed" for o in prerequisite_outcomes):
                outcome, reason = "blocked", "A prerequisite scenario has not passed"
            elif a["body"]["route"] == "manual":
                waiting = True
                outcome, reason = "not_run", "Awaiting assigned business tester"
            else:
                try:
                    callback = lambda event: record(a["scenario_id"], event)
                    if a["body"]["route"] == "ais":
                        runners.ais_scenario(env, run, a, guard, callback)
                    else:
                        asyncio.run(asyncio.wait_for(runners.browser_scenario(env, run, a, guard, callback),
                                                   timeout=max(1, deadline - time.monotonic())))
                    done = next(x for x in s.get("runs", key, company)["attempts"] if x["scenario_id"] == a["scenario_id"])
                    guard()
                    outcomes = {x["outcome"] for x in done["steps"]}
                    outcome = "failed" if "failed" in outcomes else "passed" if outcomes == {"passed"} and len(done["steps"]) == len(a["body"]["steps"]) and done["evidence"] else "needs_review"
                    reason = ""
                except Exception:
                    raise
            with connection(immediate=True):
                current = s.get("runs", key, company)
                if current["lease_owner"] != owner or current["status"] not in ("running", "stop_requested"):
                    return
                target = next(x for x in current["attempts"] if x["scenario_id"] == a["scenario_id"])
                target.update(outcome=outcome, reason=reason)
                current["revision"] += 1
                s.store("runs").put(key, current)
        with connection(immediate=True):
            current = s.get("runs", key, company)
            if current["lease_owner"] != owner or current["status"] not in ("running", "stop_requested"):
                return
            waiting = waiting or any(a["selected"] and a["outcome"] == "needs_review" and a["body"]["route"] != "manual" for a in current["attempts"])
            current.update(status="cancelled" if current["status"] == "stop_requested" else "awaiting_input" if waiting else "completed",
                           finished_at=s.now(), revision=current["revision"] + 1)
            s.store("runs").put(key, current)
    except Exception as exc:
        # Do not expose network, browser or provider errors containing credentials or payloads.
        known = isinstance(exc, (runners.Blocked, transport_error_types()))
        reason = str(exc)[:500] if known else f"Execution stopped ({type(exc).__name__}); inspect saved evidence and connection health"
        with connection(immediate=True):
            current = s.get("runs", key, company)
            if current["lease_owner"] != owner or current["status"] not in ("running", "preflight", "stop_requested"):
                return
            uncertain = bool(current.get("inflight_write"))
            stopped = current["status"] == "stop_requested"
            for a in current["attempts"]:
                if a["selected"] and a["outcome"] == "not_run":
                    a.update(outcome="needs_review" if uncertain else "blocked", reason=reason)
            current.update(status="cancelled" if stopped else "execution_error", reason=reason,
                           reconciliation_required=uncertain, finished_at=s.now(), revision=current["revision"] + 1)
            s.store("runs").put(key, current)


def transport_error_types():
    from ..discovery.transport import TransportError
    return TransportError


class Worker:
    def __init__(self):
        self.owner = s.uid("worker")
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.loop, name="jade-validation", daemon=True)

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.stop_event.set()
        with connection(immediate=True):
            for run in s.store("runs").list_all():
                if run.get("lease_owner") == self.owner and run["status"] in ("running", "preflight"):
                    run.update(status="stop_requested", revision=run["revision"] + 1)
                    s.store("runs").put(run["id"], run)
        self.thread.join(timeout=3)

    def loop(self):
        while not self.stop_event.is_set():
            try:
                s.store("workers").put(self.owner, {"id": self.owner, "at": s.now(), "heartbeat": time.time(),
                                                   "host": os.environ.get("HOSTNAME", "local")})
                claimed = claim(self.owner)
                if claimed:
                    kind, job = claimed
                    finished = threading.Event()
                    def beat():
                        while not finished.wait(10):
                            try:
                                renew(kind, job, self.owner)
                                s.store("workers").put(self.owner, {"id": self.owner, "heartbeat": time.time(), "at": s.now()})
                            except Exception:
                                LOG.exception("Validation lease renewal failed")
                    heartbeat = threading.Thread(target=beat, daemon=True)
                    heartbeat.start()
                    try:
                        if kind == "runs":
                            execute(job)
                        else:
                            asyncio.run(agents.execute(job))
                    except Exception as exc:
                        if kind == "agent_jobs":
                            with connection(immediate=True):
                                current = s.get(kind, job["id"], job["company_id"])
                                current.update(status="failed", error=f"Agent execution failed ({type(exc).__name__}). Check agent configuration and run history",
                                               finished_at=s.now())
                                s.store(kind).put(job["id"], current)
                    finally:
                        finished.set()
                        heartbeat.join(timeout=1)
                else:
                    self.stop_event.wait(1)
            except Exception:
                LOG.exception("Validation worker failed; queue remains durable")
                self.stop_event.wait(3)


if __name__ == "__main__":
    ensure_schema()
    worker = Worker().start()
    try:
        while worker.thread.is_alive():
            worker.thread.join(timeout=1)
    except KeyboardInterrupt:
        worker.close()
