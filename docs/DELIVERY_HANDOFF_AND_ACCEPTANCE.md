# Hand-offs, business acceptance and the outcome

Usability redesign, step 2 (the reference journey). All records are additive; nothing existing is changed or removed.

## Three outcomes, kept apart

| Outcome | Comes from | Who |
|---|---|---|
| Verified in DEV | the route's own verification: the functional test (orchestration or recorded), the technical package's verification, or the hand-off verification | JADE / Application Manager |
| Accepted by the business | `services/acceptance.py` (document kind `business_acceptance`) | Domain Owner of the story's domain, or the Application Manager on a named business person's behalf |
| Handed over for release | the final as-built record; promotion beyond DEV is done by CNC outside JADE | Application Manager (existing release roles) |

After DEV verification the lifecycle asks the Domain Owner to **accept the outcome** (`accept_outcome`) before the release (`finalise_asbuilt`). The as-built record has a new checkpoint, *Outcome accepted by the business*, so it cannot be finalised before. An acceptance is bound to exactly what was verified (route, approved change or package, verification record). A new verification makes it stale and it is asked for again. *Not accepted* needs a reason. It blocks the story with the Application Manager, who corrects it.

A story released before this existed shows the acceptance step as *Not recorded in JADE*; it is not reopened.

## Hand-offs to a named implementer (`delivery/handoff.py`, document kind `human_handoffs`)

Hand-off items come from the current design: for the *Human Implementation* route, the implementation steps; for every route, `human_actions_required`. Two kinds of step are left out:

- **Promotion and production steps.** These are the release hand-over, not work recorded before it.
- **Package milestones on technical routes** (check-in, build, CNC activation). These are already recorded on the package.

Items carry the design revision, so a new design asks for the work again; earlier records stay in the history.

- **Authorisation.** Human Implementation: the Application Manager authorises the hand-off of exactly the current design revision. This uses the same design-approval record and approval policy as a technical design (`authorise_handoff`). Other routes: the route's own approval covers the human items.
- **Specification.** `GET /changes/{id}/handoff/specification.pdf` is generated from the design: the need, the conclusion, the before → after changes, the work, the objects, the acceptance criteria, the validation, the rollback, the answers recorded on open points, and what could not be verified. A PDF made before authorisation says it is not authorised.
- **Recording.** Name an implementer, then record each item as completed with an evidence reference. Human Implementation: then record the verification against the acceptance criteria. Allowed roles: Application Manager, CNC operator, Administrator. Every record also goes to the story's hash-chained evidence log.
- **Lifecycle.** Human Implementation is no longer a dead end: authorise → record items → verify → business acceptance → release. On the other routes, open hand-off items must be recorded before the business is asked to accept.
- **As-built.** New checkpoints: *Every hand-off item recorded*, *Result verified* (Human Implementation), and *Work done outside JADE recorded* (other routes, when there are items).

## Work items and the outcome card (`services/work_items.py`)

On the lifecycle (read-only projections):

- `workItems`: who does what, in order. This covers agent and person configuration items, the test, technical package milestones, hand-off items, the verification, the acceptance and the release, each with an owner, its dependencies and its progress.
- `outcomeCard`: what changed, what was verified, the three outcomes, what the business does now, and the Jira link. The link is built from a Jira-shaped source reference and the customer's Jira site.
- `activity`: while JADE works, the latest read of the run in progress (from the discovery observations), for example "Reading the codes in 00/LT … 12 found".

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/changes/{id}/handoff` | items, authorisation, verification, specification |
| GET | `/changes/{id}/handoff/specification.pdf` | the implementer's specification |
| POST | `/changes/{id}/handoff/authorise` | `{note}` |
| POST | `/changes/{id}/handoff/items/{item}/implementer` | `{name}` |
| POST | `/changes/{id}/handoff/items/{item}/complete` | `{implementer, evidenceReference, note}` |
| POST | `/changes/{id}/handoff/verification` | `{passed, evidenceReference, note}` |
| GET / POST | `/changes/{id}/acceptance` | `{decision: accepted \| not_accepted, note, onBehalfOf}` |

Dependency added: `fpdf2` (pure Python, for the PDF).
