"""
Technical objects in the simulated DEV estate, and the simulation adapter
that applies, builds, activates and tests them.

THE FORMAT IS SYNTHETIC. "jade_sim_er" (Jade Simulated Event Rules) is a
small text language invented for this simulation. It is NOT a JD Edwards
export or specification format: real Event Rules live in JDE specifications
and are edited in the Fat Client; an ER print export is a report of them and
editing it changes nothing in JDE. This format exists so that the whole
Technical workflow -- inspect source, prepare a change, apply, build,
CNC activation, test -- can be exercised against something the simulation
GENUINELY supports: it is parsed, type-checked, built and executed here, and
every result is labelled SIMULATION.

    // comments start with //
    OBJECT P554210 FORM W554210A SYSTEM 55
    INPUT BC OrderTotal NUMBER
    OUTPUT VA HoldCode STRING
    EVENT OK_Button_Clicked
    IF BC OrderTotal > BC CreditLimit AND BC OrderType = "SO"
        VA HoldCode = "C1"
    ELSE
        VA HoldCode = ""
    END IF
    END EVENT

Conditions: comparisons (= != < > <= >=) joined by AND / OR (AND binds
tighter, no parentheses). Assignments set an OUTPUT variable to a literal
or another variable. The OBJECT / INPUT / OUTPUT lines are the object's
interface: changing them would be a data-structure change, which this
capability does not cover, so the simulated build refuses it.

An estate object records its ACTIVE runtime source (what runs in DEV), what
has been applied but not activated (checked_in), the last build, and the
customer's build rules. Nothing here has network code.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Optional

from . import sim_estate

FORMAT = "jade_sim_er"
FORMAT_LABEL = "jade_sim_er -- SYNTHETIC simulation format, not a JD Edwards export or specification format"
GRAMMAR = ("jade_sim_er grammar: '// comment'; header 'OBJECT <name> FORM <form> SYSTEM <nn>'; declarations "
           "'INPUT|OUTPUT <BC|VA|PO|GC> <Name> <NUMBER|STRING>' (the interface -- do not change it); "
           "'EVENT <Name>' ... 'END EVENT'; 'IF <cond>' ... ['ELSE'] ... 'END IF' (IFs may nest); "
           "assignment '<scope> <Name> = <value>'. A condition is comparisons joined by AND / OR (AND binds "
           "tighter); operators are = != < > <= >= (no <>, no parentheses); values are \"strings\", numbers or "
           "<scope> <Name>. Only OUTPUT variables can be assigned.")
ADAPTER = "jade_simulation_adapter"
ADAPTER_VERSION = "sim-1"


class ErSyntaxError(ValueError):
    def __init__(self, line: int, message: str) -> None:
        super().__init__(f"line {line}: {message}")
        self.line = line
        self.message = message


class SimObjectError(RuntimeError):
    pass


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def object_key(object_name: str, object_type: str) -> str:
    return f"{object_name.strip().upper()}|{object_type.strip().upper()}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------
_TOKEN = re.compile(r'\s*(?:(?P<str>"(?:[^"\\]|\\.)*")|(?P<num>-?\d+(?:\.\d+)?)|(?P<op><=|>=|!=|=|<|>)|(?P<word>[A-Za-z_][A-Za-z0-9_]*))')
SCOPES = {"BC", "VA", "PO", "GC"}
TYPES = {"NUMBER", "STRING"}
OPS = {"=", "!=", "<", ">", "<=", ">="}


def _strip_comment(line: str) -> str:
    out, in_str, i = [], False, 0
    while i < len(line):
        ch = line[i]
        if ch == '"' and (i == 0 or line[i - 1] != "\\"):
            in_str = not in_str
        if not in_str and line.startswith("//", i):
            break
        out.append(ch)
        i += 1
    return "".join(out).strip()


def _tokens(text: str, line: int) -> list[tuple[str, str]]:
    pos, out = 0, []
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise ErSyntaxError(line, f"unexpected text {text[pos:pos + 20]!r}")
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
        pos = m.end()
    return out


def _operand(tokens: list, i: int, line: int) -> tuple[tuple, int]:
    if i >= len(tokens):
        raise ErSyntaxError(line, "missing operand")
    kind, value = tokens[i]
    if kind == "str":
        return ("lit", "STRING", bytes(value[1:-1], "utf-8").decode("unicode_escape")), i + 1
    if kind == "num":
        return ("lit", "NUMBER", float(value)), i + 1
    if kind == "word" and value in SCOPES:
        if i + 1 >= len(tokens) or tokens[i + 1][0] != "word":
            raise ErSyntaxError(line, f"{value} must be followed by a variable name")
        return ("var", value, tokens[i + 1][1]), i + 2
    raise ErSyntaxError(line, f"expected a variable, number or string, found {value!r}")


def _condition(tokens: list, line: int) -> tuple:
    ors, ands, i = [], [], 0
    while True:
        left, i = _operand(tokens, i, line)
        if i >= len(tokens) or tokens[i][0] != "op":
            raise ErSyntaxError(line, "expected a comparison operator (= != < > <= >=)")
        op = tokens[i][1]
        right, i = _operand(tokens, i + 1, line)
        ands.append(("cmp", op, left, right))
        if i >= len(tokens):
            break
        joiner = tokens[i][1].upper()
        if joiner == "AND":
            i += 1
        elif joiner == "OR":
            ors.append(ands)
            ands, i = [], i + 1
        else:
            raise ErSyntaxError(line, f"expected AND or OR, found {tokens[i][1]!r}")
        if i >= len(tokens):
            raise ErSyntaxError(line, "condition ends with AND/OR")
    ors.append(ands)
    return ("or", ors)


def parse(text: str) -> dict[str, Any]:
    """The program, or ErSyntaxError with a line number."""
    header: Optional[dict] = None
    inputs: dict[str, dict] = {}
    outputs: dict[str, dict] = {}
    events: dict[str, list] = {}
    stack: list[list] = []  # statement lists being filled
    frames: list[tuple] = []  # ("event", name, line) or ("if", node, line)
    for n, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw)
        if not line:
            continue
        toks = _tokens(line, n)
        words = [v.upper() if k == "word" else v for k, v in toks]
        head = words[0]
        if header is None:
            if len(words) != 6 or words[0] != "OBJECT" or words[2] != "FORM" or words[4] != "SYSTEM":
                raise ErSyntaxError(n, "the first line must be: OBJECT <name> FORM <form> SYSTEM <nn>")
            header = {"object": toks[1][1].upper(), "form": toks[3][1].upper(), "system_code": toks[5][1]}
            continue
        if head in ("INPUT", "OUTPUT") and not frames:
            if len(words) != 4 or words[1] not in SCOPES or words[3] not in TYPES:
                raise ErSyntaxError(n, f"{head} <BC|VA|PO|GC> <Name> <NUMBER|STRING>")
            target = inputs if head == "INPUT" else outputs
            name = toks[2][1]
            if name in inputs or name in outputs:
                raise ErSyntaxError(n, f"{name} is declared twice")
            target[name] = {"scope": words[1], "type": words[3]}
            continue
        if head == "EVENT" and len(words) == 2 and not frames:
            name = toks[1][1]
            if name in events:
                raise ErSyntaxError(n, f"event {name} is defined twice")
            events[name] = []
            stack.append(events[name])
            frames.append(("event", name, n))
            continue
        if not frames:
            raise ErSyntaxError(n, f"{toks[0][1]!r} outside an EVENT block")
        if words[:2] == ["END", "EVENT"] and len(words) == 2:
            if frames[-1][0] != "event":
                raise ErSyntaxError(n, "END EVENT inside an open IF")
            frames.pop()
            stack.pop()
            continue
        if head == "IF":
            node = ("if", _condition(toks[1:], n), [], [], n)
            stack[-1].append(node)
            stack.append(node[2])
            frames.append(("if", node, n))
            continue
        if head == "ELSE" and len(words) == 1:
            if not frames or frames[-1][0] != "if" or frames[-1][1][3]:
                raise ErSyntaxError(n, "ELSE without a matching IF")
            stack.pop()
            stack.append(frames[-1][1][3])
            frames[-1] = ("if_else", frames[-1][1], frames[-1][2])
            continue
        if words[:2] == ["END", "IF"] and len(words) == 2:
            if frames[-1][0] not in ("if", "if_else"):
                raise ErSyntaxError(n, "END IF without a matching IF")
            frames.pop()
            stack.pop()
            continue
        if len(toks) >= 4 and toks[0][1].upper() in SCOPES and toks[2] == ("op", "="):
            value, i = _operand(toks, 3, n)
            if i != len(toks):
                raise ErSyntaxError(n, "an assignment sets one variable to one value")
            stack[-1].append(("assign", (toks[0][1].upper(), toks[1][1]), value, n))
            continue
        raise ErSyntaxError(n, f"cannot read {line!r}")
    if header is None:
        raise ErSyntaxError(1, "empty source: no OBJECT line")
    if frames:
        kind, _node, n = frames[-1]
        raise ErSyntaxError(n, "EVENT block is not closed with END EVENT" if kind == "event" else "IF is not closed with END IF")
    return {"header": header, "inputs": inputs, "outputs": outputs, "events": events}


def interface(program: dict) -> dict:
    return {"header": program["header"], "inputs": program["inputs"], "outputs": program["outputs"]}


# ---------------------------------------------------------------------
# Static checks (the simulated compiler) and execution
# ---------------------------------------------------------------------
def _type_of(operand: tuple, decls: dict) -> Optional[str]:
    if operand[0] == "lit":
        return operand[1]
    decl = decls.get(operand[2])
    return decl["type"] if decl else None


def check(program: dict) -> list[str]:
    """Type and declaration errors, as build log lines."""
    decls = {**program["inputs"], **program["outputs"]}
    errors: list[str] = []

    def var_ok(scope: str, name: str, line: int) -> bool:
        decl = decls.get(name)
        if decl is None:
            errors.append(f"line {line}: {scope} {name} is not declared")
            return False
        if decl["scope"] != scope:
            errors.append(f"line {line}: {name} is declared as {decl['scope']}, used as {scope}")
            return False
        return True

    def walk(stmts: list) -> None:
        for s in stmts:
            if s[0] == "assign":
                (scope, name), value, line = s[1], s[2], s[3]
                if var_ok(scope, name, line):
                    if name not in program["outputs"]:
                        errors.append(f"line {line}: {name} is an INPUT and cannot be assigned")
                    elif value[0] == "var" and not var_ok(value[1], value[2], line):
                        pass
                    elif _type_of(value, decls) != decls[name]["type"]:
                        errors.append(f"line {line}: {name} is {decls[name]['type']}, assigned a {_type_of(value, decls)}")
            else:
                line = s[4]
                for conj in s[1][1]:
                    for _, op, left, right in conj:
                        ok = all(var_ok(o[1], o[2], line) for o in (left, right) if o[0] == "var")
                        if ok and _type_of(left, decls) != _type_of(right, decls):
                            errors.append(f"line {line}: cannot compare {_type_of(left, decls)} with {_type_of(right, decls)}")
                        if ok and op in ("<", ">", "<=", ">=") and _type_of(left, decls) != "NUMBER":
                            errors.append(f"line {line}: {op} needs NUMBER operands")
                walk(s[2])
                walk(s[3])

    for stmts in program["events"].values():
        walk(stmts)
    return errors


def _value(operand: tuple, env: dict) -> Any:
    return operand[2] if operand[0] == "lit" else env[operand[2]]


_CMP = {"=": lambda a, b: a == b, "!=": lambda a, b: a != b, "<": lambda a, b: a < b, ">": lambda a, b: a > b,
        "<=": lambda a, b: a <= b, ">=": lambda a, b: a >= b}


def run_event(program: dict, event: str, inputs: dict[str, Any]) -> dict[str, Any]:
    """Execute one event with the given inputs; returns the outputs."""
    if event not in program["events"]:
        raise SimObjectError(f"no event {event!r} in {program['header']['object']}")
    env: dict[str, Any] = {}
    for name, decl in program["inputs"].items():
        if name not in inputs:
            raise SimObjectError(f"input {name} was not given")
        raw = inputs[name]
        env[name] = float(raw) if decl["type"] == "NUMBER" else str(raw)
    for name, decl in program["outputs"].items():
        env[name] = 0.0 if decl["type"] == "NUMBER" else ""

    def execute(stmts: list) -> None:
        for s in stmts:
            if s[0] == "assign":
                env[s[1][1]] = _value(s[2], env)
            else:
                taken = any(all(_CMP[op](_value(l, env), _value(r, env)) for _, op, l, r in conj) for conj in s[1][1])
                execute(s[2] if taken else s[3])

    execute(program["events"][event])
    return {name: env[name] for name in program["outputs"]}


# ---------------------------------------------------------------------
# Customer build rules (per object, part of the simulated estate)
# ---------------------------------------------------------------------
def changed_lines(before: str, after: str) -> list[tuple[int, str]]:
    """(line number in `after`, text) of every added or changed line."""
    a, b = before.splitlines(), after.splitlines()
    out = []
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag in ("insert", "replace"):
            out.extend((j + 1, b[j]) for j in range(j1, j2))
    return out


def apply_build_rules(rules: list[dict], before: str, after: str, context: dict) -> list[str]:
    errors = []
    for rule in rules or []:
        if rule.get("kind") == "modification_marker":
            marker = rule["marker"].format(**context)
            for n, text in changed_lines(before, after):
                if text.strip() and marker not in text:
                    errors.append(f"{rule['id']} line {n}: added or changed line has no modification marker "
                                  f"'{marker}' ({rule.get('description', '')})")
    return errors


# ---------------------------------------------------------------------
# The estate object and the simulation adapter
# ---------------------------------------------------------------------
def seed_object(company_id: str, environment: str, *, source: str, object_type: str = "ER", description: str = "",
                build_rules: Optional[list[dict]] = None, actor: str, reason: str) -> str:
    """Place a technical object's ACTIVE runtime source in the estate
    (test harness / demonstration setup)."""
    program = parse(source)
    key = object_key(program["header"]["object"], object_type)
    with sim_estate.edit(company_id, environment, actor=actor, reason=reason) as estate:
        estate.setdefault("objects", {})[key] = {
            "object_name": program["header"]["object"], "object_type": object_type.upper(),
            "form": program["header"]["form"], "system_code": program["header"]["system_code"], "format": FORMAT,
            "description": description, "interface": interface(program),
            "active": {"source": source, "sha256": sha256_text(source), "since": _now(), "package": "INITIAL"},
            "checked_in": None, "build": None, "activations": [], "build_rules": build_rules or [],
        }
    return key


def get_object(company_id: str, environment: str, key: str) -> Optional[dict]:
    return (sim_estate.load(company_id, environment).get("objects") or {}).get(key)


def runtime_state(company_id: str, environment: str, keys: list[str]) -> dict[str, Optional[str]]:
    """object key -> sha256 of its ACTIVE runtime source (None if absent)."""
    objects = sim_estate.load(company_id, environment).get("objects") or {}
    return {k: (objects.get(k) or {}).get("active", {}).get("sha256") for k in keys}


class AdapterOutcome(RuntimeError):
    """A simulated call ended without a clean answer. `applied` says what the
    simulation actually did, which the caller must NOT know -- it records the
    attempt as unknown or not sent according to `sent`."""

    def __init__(self, message: str, *, sent: bool) -> None:
        super().__init__(message)
        self.sent = sent


def _fault(estate: dict, operation: str, key: str) -> Optional[dict]:
    return sim_estate.take_fault(estate, operation, key)


def _raise_for(fault: dict) -> None:
    if fault["mode"] == "fail_before_send":
        raise AdapterOutcome(f"simulated: the request never left (TEST CONDITION) {fault['message']}", sent=False)
    if fault["mode"] == "fail":
        raise AdapterOutcome(f"simulated error answer (TEST CONDITION) {fault['message']}", sent=False)
    raise AdapterOutcome(f"simulated {fault['mode']} (TEST CONDITION) {fault['message']}", sent=True)


def adapter_apply(company_id: str, environment: str, key: str, candidate: str, *, change_id: str,
                  package_ref: str) -> dict:
    """Check the candidate in to DEV (not active until built and activated)."""
    fault = None
    with sim_estate.edit(company_id, environment, actor="simulation adapter",
                         reason=f"apply {package_ref} to {key} for change {change_id}") as estate:
        obj = (estate.get("objects") or {}).get(key)
        if obj is None:
            raise SimObjectError(f"{key} does not exist in the simulated DEV estate")
        fault = _fault(estate, "apply", key)
        if fault is None or fault["mode"] == "timeout_after_apply":
            obj["checked_in"] = {"source": candidate, "sha256": sha256_text(candidate), "change_id": change_id,
                                 "package_ref": package_ref, "at": _now()}
            obj["build"] = None
    if fault is not None:
        _raise_for(fault)
    return {"object_key": key, "checked_in_sha256": sha256_text(candidate)}


def adapter_build(company_id: str, environment: str, key: str, *, change_id: str, context: dict) -> dict:
    """Compile the checked-in source: syntax, declarations and types, the
    interface (a data-structure change is refused), and the customer's build
    rules. A known failure is an outcome with a log, not an exception.

    Build test conditions: fail_before_send (never reached the build server),
    fail (the build server reports an error: a KNOWN failure with that log),
    timeout_before_apply (no build ran, no answer), timeout_after_apply (the
    build ran and was recorded, but no answer came back)."""
    with sim_estate.edit(company_id, environment, actor="simulation adapter",
                         reason=f"build {key} for change {change_id}") as estate:
        obj = (estate.get("objects") or {}).get(key)
        if obj is None or not obj.get("checked_in"):
            raise SimObjectError(f"{key} has nothing checked in to build")
        fault = _fault(estate, "build", key)
        result: dict = {}
        if fault is None or fault["mode"] in ("fail", "timeout_after_apply"):
            source = obj["checked_in"]["source"]
            log: list[str] = []
            if fault is not None and fault["mode"] == "fail":
                log.append(f"TEST CONDITION: build server error -- {fault['message'] or 'no detail'}")
            else:
                try:
                    program = parse(source)
                    log += check(program)
                    if interface(program) != obj["interface"]:
                        log.append("SIM-BLD-0: the OBJECT/INPUT/OUTPUT interface differs from the registered data "
                                   "structure -- a data-structure change is not part of this capability")
                except ErSyntaxError as exc:
                    log.append(f"syntax error {exc}")
                log += apply_build_rules(obj.get("build_rules") or [], obj["active"]["source"], source, context)
            result = {"sha256": obj["checked_in"]["sha256"], "status": "failed" if log else "built", "log": log,
                      "at": _now(), "change_id": change_id}
            obj["build"] = result
    if fault is not None and fault["mode"] in ("fail_before_send", "timeout_before_apply", "timeout_after_apply"):
        _raise_for(fault)
    return result


def adapter_activate(company_id: str, environment: str, key: str, *, change_id: str, package_name: str,
                     actor: str) -> dict:
    """The CNC's package deployment, simulated as a recorded human action:
    the built, checked-in source becomes the active DEV runtime."""
    with sim_estate.edit(company_id, environment, actor=f"simulated CNC activation by {actor}",
                         reason=f"activate {key} (package {package_name}) for change {change_id}") as estate:
        obj = (estate.get("objects") or {}).get(key)
        if obj is None or not obj.get("checked_in"):
            raise SimObjectError(f"{key} has nothing checked in")
        build = obj.get("build") or {}
        if build.get("status") != "built" or build.get("sha256") != obj["checked_in"]["sha256"]:
            raise SimObjectError(f"{key}: the checked-in source has not been built successfully")
        previous = obj["active"]
        obj["active"] = {"source": obj["checked_in"]["source"], "sha256": obj["checked_in"]["sha256"],
                         "since": _now(), "package": package_name}
        obj["activations"].append({"previous_sha256": previous["sha256"], "sha256": obj["active"]["sha256"],
                                   "package": package_name, "change_id": change_id, "by": actor, "at": _now()})
        obj["checked_in"] = None
        # What discovery can observe afterwards: the object librarian row
        # names the package that last deployed it (F9860.SIPKGNAME).
        for row in estate["tables"].get("F9860", []):
            if row.get("SIOBNM") == obj["object_name"]:
                row["SIPKGNAME"] = package_name
                row["SIUPMJ"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return {"object_key": key, "active_sha256": obj["active"]["sha256"], "package": package_name}


def adapter_run_tests(company_id: str, environment: str, key: str, tests: list[dict], *, change_id: str) -> list[dict]:
    """Execute the test plan against the ACTIVE runtime source."""
    fault = None
    with sim_estate.edit(company_id, environment, actor="simulation adapter",
                         reason=f"verification tests on {key} for change {change_id}") as estate:
        fault = _fault(estate, "verify", key)
    if fault is not None:
        _raise_for(fault)
    obj = get_object(company_id, environment, key)
    if obj is None:
        raise SimObjectError(f"{key} does not exist in the simulated DEV estate")
    program = parse(obj["active"]["source"])
    results = []
    for t in tests:
        try:
            actual = run_event(program, t["event"], t.get("inputs") or {})
            expected = t.get("expected") or {}
            passed = all(str(actual.get(k)) == str(v) if not isinstance(v, (int, float)) else float(actual.get(k, 0)) == float(v)
                         for k, v in expected.items())
            results.append({"name": t["name"], "kind": t.get("kind"), "passed": passed, "expected": expected,
                            "actual": {k: actual.get(k) for k in expected}, "runtime_sha256": obj["active"]["sha256"]})
        except (SimObjectError, KeyError, ValueError) as exc:
            results.append({"name": t.get("name"), "kind": t.get("kind"), "passed": False, "error": str(exc),
                            "runtime_sha256": obj["active"]["sha256"]})
    return results
