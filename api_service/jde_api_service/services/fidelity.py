"""Source fidelity of a user story (audit 23 Oct, F04).

The Check Agent judges meaning; this is the deterministic floor under it,
run when the Check Agent hands a story to review (propose_to_backlog):

* Every conditional the source states ("if the standard inquiry supports
  this", "whether", "unless", "als", "indien", ...) must be accounted for in
  the story's ``source_conditions`` -- quoted from the source, and kept in a
  story item that is itself still conditional (an open question, an
  assumption, or a criterion that keeps the condition). A criterion that
  turns the condition into a settled conclusion fails.
* Every acceptance criterion must trace to a source passage
  (``requirement_trace``: criterion -> verbatim source quote), or say why it
  is derived. A quote the source does not contain is invented scope.

It never rewrites the story. It returns the failures, named, so the Check
Agent sends the story back to the Improve Agent with them.
"""

from __future__ import annotations

import re
from typing import Any

# Markers that make a clause conditional (English and Dutch, the customers' languages).
_COND_START = re.compile(
    r"\b(if|whether|unless|provided that|in case|only when|only if|as long as|"
    # Dutch "als"/"wanneer" are left out: "Als orderverwerker wil ik" is the story form, not a condition.
    r"indien|tenzij|mits|voor zover)\b", re.IGNORECASE)
# Words that keep a story item open/conditional.
_OPEN_WORDS = re.compile(
    r"\b(if|whether|unless|provided|in case|only when|only if|as long as|when|where|assum\w*|"
    r"confirm\w*|verif\w*|determin\w*|check\w*|establish\w*|find out|investigat\w*|unknown|"
    r"otherwise|either|or not|als|indien|tenzij|mits|voor zover|wanneer|nagaan|controleren|of\b)",
    re.IGNORECASE)
_WORD = re.compile(r"[a-z0-9À-ɏ]+", re.IGNORECASE)
_STOP = set("""the a an and or of to in on for with by at from is are be been was were this that these those it its as
into than then so such can could should would will shall may might must not no yes do does did done have has had
de het een en of te in op voor met door bij van is zijn wordt worden dit dat deze die er als ook niet wel""".split())


def _words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text or "") if len(w) > 2 and w.lower() not in _STOP]


def _overlap(a: str, b: str) -> float:
    """Share of a's content words found in b."""
    wa, wb = _words(a), set(_words(b))
    return (sum(1 for w in wa if w in wb) / len(wa)) if wa else 0.0


def _in_source(quote: str, source: str) -> bool:
    q = " ".join(_words(quote))
    if not q:
        return False
    return q in " ".join(_words(source)) or _overlap(quote, source) >= 0.8


def conditional_clauses(source: str) -> list[str]:
    """The conditional clauses the source states, each from its marker to the
    end of the clause (punctuation or line end), with at least two content words."""
    out: list[str] = []
    for m in _COND_START.finditer(source or ""):
        before = (source or "")[max(0, m.start() - 25):m.start()].lower()
        if re.search(r"\b(ask|asks|asked|asking|wonder|wonders|wondering|vraagt|vragen)\s+(\w+\s+)?$", before):
            continue   # reported speech ("phone to ask whether ..."), not a condition on the requirement
        rest = source[m.start():]
        end = re.search(r"[.;:?!\n]|,\s", rest[len(m.group(0)):])
        clause = rest[: len(m.group(0)) + (end.start() if end else len(rest))].strip()
        if len(_words(clause)) >= 2 and clause not in out:
            out.append(clause)
    return out


def _items(story: dict) -> dict[str, list[str]]:
    def as_list(v: Any) -> list[str]:
        if isinstance(v, list):
            return [x if isinstance(x, str) else (x.get("text") or x.get("step") or str(x)) if isinstance(x, dict) else str(x)
                    for x in v]
        return [v] if isinstance(v, str) and v.strip() else []
    return {k: as_list(story.get(k)) for k in ("acceptance_criteria", "open_questions", "assumptions", "business_rules",
                                               "statement", "test_script")}


def check(source: str, story: dict) -> list[str]:
    failures: list[str] = []
    if not (source or "").strip():
        return ["The source request is not available, so its meaning cannot be checked."]
    items = _items(story or {})
    every_item = [(k, t) for k, ts in items.items() for t in ts]
    criteria = items["acceptance_criteria"]

    # 1. Conditionals stay conditional.
    accounted = story.get("source_conditions") if isinstance(story.get("source_conditions"), list) else []
    for clause in conditional_clauses(source):
        entry = next((e for e in accounted if isinstance(e, dict) and _overlap(clause, str(e.get("source", ""))) >= 0.6), None)
        if entry is None:
            failures.append(f'The source states a condition the story does not account for: "{clause}". '
                            "List it under source_conditions with the story item that keeps it open.")
            continue
        kept = str(entry.get("kept_as", ""))
        target = next((t for _, t in every_item if kept and (kept.strip() == t.strip() or _overlap(kept, t) >= 0.7)), None)
        if target is None:
            failures.append(f'The condition "{clause}" is said to be kept as "{kept}", which is not in the story.')
        elif not (target in items["open_questions"] or target in items["assumptions"] or _OPEN_WORDS.search(target)):
            failures.append(f'The source leaves this open: "{clause}". The story turns it into a settled statement: '
                            f'"{target}". Keep it conditional (an open question, an assumption, or a criterion '
                            "that keeps the condition).")

    # 2. Every acceptance criterion traces to the source (or says why it is derived).
    trace = story.get("requirement_trace") if isinstance(story.get("requirement_trace"), list) else []
    for crit in criteria:
        entry = next((e for e in trace if isinstance(e, dict) and _overlap(crit, str(e.get("criterion", ""))) >= 0.7), None)
        if entry is None:
            failures.append(f'Acceptance criterion without a source trace: "{crit}". Quote the source passage it comes '
                            "from in requirement_trace, or state why it is derived.")
            continue
        quote = str(entry.get("source_quote", "")).strip()
        derived = str(entry.get("derived_because", "")).strip()
        if quote and not _in_source(quote, source):
            failures.append(f'The source does not say "{quote}" (given as the basis of "{crit}"). Invented scope is '
                            "not allowed: quote the source exactly, or state why the criterion is derived.")
        elif not quote and len(derived) < 10:
            failures.append(f'Acceptance criterion "{crit}" has neither a source quote nor a reason it is derived.')
    return failures
