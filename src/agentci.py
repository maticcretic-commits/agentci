#!/usr/bin/env python3
"""
AgentCI — "You changed the prompt. Did the agent get better or worse?"

An open-source, GitHub-native regression harness for AI agents. Teams
tweak a prompt or a config and ship on vibes, because there is no test
run to compare against. AgentCI fixes that: keep a suite of test cases
(input + expected checks), run it against saved agent outputs, save the
report as a baseline — then, after the next prompt change, run again and
diff. Regressions fail the build.

Offline-first: AgentCI evaluates saved outputs. It never calls a live
model API, so it runs anywhere pytest runs — including CI.

Usage:
    agentci init                                # scaffold a sample suite
    agentci run <cases.json> <outputs.json> [--json] [--save report.json]
    agentci compare <baseline.json> <new.json> [--json]
    agentci bundle <baseline.bundle.json> <new.bundle.json>
                   [--budget 1.50] [--json]

`bundle` is the regression proof over TraceTape proof bundles: it
fails when the new run's sealed evidence is weaker than the
baseline's — evidence lost, bundle schema broken, cost over budget,
or the TrustBadge score degraded.
"""

import argparse
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

VERSION = "1.1.0"

CHECK_TYPES = ("contains", "not_contains", "regex", "min_length")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class CheckOutcome:
    type: str
    status: str  # "pass" | "fail"
    detail: str = ""


@dataclass
class CaseResult:
    id: str
    status: str  # "pass" | "fail" | "missing"
    checks: list = field(default_factory=list)
    reason: str = ""  # set when status == "missing"


@dataclass
class RunReport:
    agent: str
    timestamp: str
    cases_file: str = ""
    outputs_file: str = ""
    results: list = field(default_factory=list)

    @property
    def total(self): return len(self.results)

    @property
    def passed(self): return sum(1 for r in self.results if r.status == "pass")

    @property
    def failed(self): return sum(1 for r in self.results if r.status == "fail")

    @property
    def missing(self): return sum(1 for r in self.results if r.status == "missing")

    @property
    def score(self):
        """Percent of cases passing, 0-100. Missing outputs count as not passing."""
        if not self.total:
            return 0
        return round(100 * self.passed / self.total)

    @property
    def verdict(self):
        if self.total and self.passed == self.total:
            return "PASS"
        return "FAIL"


@dataclass
class CaseDelta:
    id: str
    baseline_status: str
    new_status: str


@dataclass
class CompareReport:
    baseline_file: str = ""
    new_file: str = ""
    regressions: list = field(default_factory=list)   # was pass, now not pass
    improvements: list = field(default_factory=list)  # was not pass, now pass
    unchanged: list = field(default_factory=list)     # same status (pass or not)
    added: list = field(default_factory=list)         # only in the new report
    removed: list = field(default_factory=list)       # only in the baseline

    @property
    def verdict(self):
        return "REGRESSION" if self.regressions else "OK"


# ---------------------------------------------------------------------------
# Loading + validation
# ---------------------------------------------------------------------------

def load_cases(path):
    """Load a cases file. Accepts {"agent": ..., "cases": [...]} or a
    bare list of cases. Returns (agent_name, [case, ...]).
    Raises ValueError with a readable message on malformed input."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        agent, cases = "", data
    elif isinstance(data, dict):
        agent, cases = str(data.get("agent", "")), data.get("cases")
        if not isinstance(cases, list):
            raise ValueError("cases file: 'cases' must be a list")
    else:
        raise ValueError("cases file: top level must be an object or a list")
    for i, case in enumerate(cases):
        if not isinstance(case, dict) or "id" not in case:
            raise ValueError(f"cases file: case #{i} has no 'id'")
        checks = case.get("checks")
        if not isinstance(checks, list) or not checks:
            raise ValueError(
                f"cases file: case '{case['id']}' needs a non-empty 'checks' list")
        for chk in checks:
            if not isinstance(chk, dict) or "type" not in chk:
                raise ValueError(
                    f"cases file: case '{case['id']}' has a check with no 'type'")
    return agent, cases


def load_outputs(path):
    """Load an outputs file into {case_id: output_text}. Accepts a dict
    {"case-id": "output", ...} or a list [{"id": ..., "output": ...}]."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items()}
    if isinstance(data, list):
        out = {}
        for i, item in enumerate(data):
            if not isinstance(item, dict) or "id" not in item or "output" not in item:
                raise ValueError(
                    f"outputs file: entry #{i} needs 'id' and 'output'")
            out[str(item["id"])] = str(item["output"])
        return out
    raise ValueError("outputs file: top level must be an object or a list")


def load_report(path):
    """Load a saved run report (the JSON written by `run --save` / `--json`)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError(f"{path}: not an AgentCI run report (no 'results' list)")
    return data


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def evaluate_check(check, output):
    """Evaluate one check against one output string."""
    ctype = check.get("type", "")
    if ctype == "contains":
        needle = str(check.get("value", ""))
        ok = needle.lower() in output.lower()
        return CheckOutcome(ctype, "pass" if ok else "fail",
                            f"output {'contains' if ok else 'does not contain'} "
                            f"{needle!r} (case-insensitive)")
    if ctype == "not_contains":
        needle = str(check.get("value", ""))
        ok = needle.lower() not in output.lower()
        return CheckOutcome(ctype, "pass" if ok else "fail",
                            f"output {'correctly omits' if ok else 'contains forbidden'} "
                            f"{needle!r} (case-insensitive)")
    if ctype == "regex":
        pattern = str(check.get("pattern", ""))
        try:
            ok = re.search(pattern, output) is not None
        except re.error as e:
            return CheckOutcome(ctype, "fail", f"invalid regex {pattern!r}: {e}")
        return CheckOutcome(ctype, "pass" if ok else "fail",
                            f"pattern {pattern!r} {'matched' if ok else 'did not match'}")
    if ctype == "min_length":
        try:
            need = int(check.get("value", 0))
        except (TypeError, ValueError):
            return CheckOutcome(ctype, "fail",
                                f"min_length value is not a number: {check.get('value')!r}")
        ok = len(output) >= need
        return CheckOutcome(ctype, "pass" if ok else "fail",
                            f"output is {len(output)} chars, need >= {need}")
    return CheckOutcome(ctype, "fail",
                        f"unknown check type {ctype!r} "
                        f"(supported: {', '.join(CHECK_TYPES)})")


def evaluate_case(case, outputs):
    """Evaluate one case against the outputs map."""
    cid = str(case["id"])
    if cid not in outputs:
        return CaseResult(id=cid, status="missing",
                          reason=f"no output saved for case '{cid}'")
    output = outputs[cid]
    outcomes = [evaluate_check(chk, output) for chk in case["checks"]]
    status = "pass" if all(o.status == "pass" for o in outcomes) else "fail"
    return CaseResult(id=cid, status=status, checks=outcomes)


def run_suite(agent, cases, outputs, cases_file="", outputs_file=""):
    report = RunReport(
        agent=agent,
        timestamp=datetime.now(timezone.utc).isoformat(),
        cases_file=cases_file, outputs_file=outputs_file,
    )
    report.results = [evaluate_case(case, outputs) for case in cases]
    return report


# ---------------------------------------------------------------------------
# Compare
# ---------------------------------------------------------------------------

def _status_map(report):
    return {str(r["id"]): r["status"] for r in report["results"]}


def compare_reports(baseline, new, baseline_file="", new_file=""):
    base, cur = _status_map(baseline), _status_map(new)
    cmp = CompareReport(baseline_file=baseline_file, new_file=new_file)
    for cid in sorted(set(base) | set(cur)):
        if cid not in base:
            cmp.added.append(CaseDelta(cid, "(absent)", cur[cid]))
        elif cid not in cur:
            cmp.removed.append(CaseDelta(cid, base[cid], "(absent)"))
        elif base[cid] == "pass" and cur[cid] != "pass":
            cmp.regressions.append(CaseDelta(cid, base[cid], cur[cid]))
        elif base[cid] != "pass" and cur[cid] == "pass":
            cmp.improvements.append(CaseDelta(cid, base[cid], cur[cid]))
        else:
            cmp.unchanged.append(CaseDelta(cid, base[cid], cur[cid]))
    return cmp


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

STATUS_ICON = {"pass": "✅", "fail": "❌", "missing": "⚠️"}


def print_run_text(r: RunReport):
    print("=" * 68)
    print(f"AgentCI run — {r.agent or 'agent suite'}")
    print("=" * 68)
    print(f"Time:    {r.timestamp}")
    if r.cases_file:
        print(f"Cases:   {r.cases_file}")
    if r.outputs_file:
        print(f"Outputs: {r.outputs_file}")
    print()
    for res in r.results:
        icon = STATUS_ICON[res.status]
        if res.status == "missing":
            print(f"{icon} {res.id} — MISSING ({res.reason})")
            continue
        n_pass = sum(1 for c in res.checks if c.status == "pass")
        print(f"{icon} {res.id} — {res.status.upper()} "
              f"({n_pass}/{len(res.checks)} checks)")
        for c in res.checks:
            if c.status != "pass":
                print(f"     ✗ [{c.type}] {c.detail}")
    print()
    print(f"Score: {r.passed}/{r.total} cases passed ({r.score}%) — {r.verdict}")
    if r.missing:
        print(f"Note: {r.missing} case(s) had no saved output and count as not passing.")


def print_compare_text(c: CompareReport):
    icon = "❌" if c.regressions else "✅"
    print("=" * 68)
    print(f"AgentCI compare — {icon} {c.verdict}")
    print("=" * 68)
    if c.baseline_file:
        print(f"Baseline: {c.baseline_file}")
    if c.new_file:
        print(f"New:      {c.new_file}")
    print()
    def show(title, items, fmt):
        print(f"{title} ({len(items)}):")
        for d in items:
            print(f"  - {fmt(d)}")
        if not items:
            print("  (none)")
        print()
    show("🔻 Regressions — was PASS, now not",
         c.regressions, lambda d: f"{d.id}: {d.baseline_status} → {d.new_status}")
    show("🔺 Improvements — was not PASS, now PASS",
         c.improvements, lambda d: f"{d.id}: {d.baseline_status} → {d.new_status}")
    show("➖ Unchanged", c.unchanged,
         lambda d: f"{d.id}: {d.new_status}")
    show("➕ Added cases", c.added, lambda d: f"{d.id}: {d.new_status}")
    show("➗ Removed cases", c.removed, lambda d: f"{d.id}: was {d.baseline_status}")
    total = (len(c.regressions) + len(c.improvements) + len(c.unchanged)
             + len(c.added) + len(c.removed))
    print(f"{len(c.regressions)} regressions · {len(c.improvements)} improvements · "
          f"{len(c.unchanged)} unchanged · {total} cases compared")


# ---------------------------------------------------------------------------
# Bundle regression (TraceTape proof bundles)
#
# A proof bundle (written by `tracetape bundle`) seals a run's
# evidence: the tape, claims, a skill manifest, tool reports, a cost
# record, a schema contract, a TrustBadge score — each named by
# SHA-256. AgentCI treats a pair of bundles the way `compare` treats
# a pair of run reports: the new bundle must not be *weaker* than
# the baseline. Four regression classes:
#
#   schema   — the new bundle no longer parses as a proof bundle.
#   evidence — a sealed section / claim / skill file / tool report
#              that the baseline carried is gone, or the tape shrank.
#   cost     — the new cost total exceeds --budget (or the total
#              vanished while a budget is being enforced).
#   score    — the TrustBadge composite went down.
#
# Changed-but-present evidence (a skill file with a new hash, a
# bigger tape) is reported as a note / improvement, never silently
# passed over.
# ---------------------------------------------------------------------------

BUNDLE_SECTIONS = ("tape", "claims", "skill_manifest", "tool_outputs",
                   "cost_record", "schema_contract", "trustbadge")


@dataclass
class BundleFinding:
    category: str  # "schema" | "evidence" | "cost" | "score"
    detail: str


@dataclass
class BundleCompare:
    baseline_file: str = ""
    new_file: str = ""
    regressions: list = field(default_factory=list)
    improvements: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    @property
    def verdict(self):
        return "REGRESSION" if self.regressions else "OK"


def bundle_problems(doc):
    """Structural problems of a would-be proof bundle ([] = sound).
    This is AgentCI's own reading of the bundle format — the same
    mirror-the-format pattern TrustBadge uses for sibling reports."""
    if not isinstance(doc, dict):
        return ["top level is not a JSON object"]
    problems = []
    if doc.get("kind") != "proof-bundle":
        problems.append(f"kind is {doc.get('kind')!r}, "
                        f"not 'proof-bundle'")
    bv = doc.get("bundle_version")
    if not isinstance(bv, int) or isinstance(bv, bool) or bv < 1:
        problems.append(f"bundle_version is {bv!r}, not a positive int")
    sections = doc.get("sections")
    if not isinstance(sections, dict):
        problems.append("sections is missing or not an object")
        return problems
    tape = sections.get("tape")
    if not isinstance(tape, dict):
        problems.append("sections.tape is missing or not an object")
    else:
        for key in ("sha256", "events", "runs"):
            if key not in tape:
                problems.append(f"sections.tape has no '{key}'")
    return problems


def _decimal_or_none(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def compare_bundles(baseline, new, budget=None,
                    baseline_file="", new_file=""):
    """Diff two bundle dicts. `budget` is a Decimal or None.
    Assumes the baseline is structurally sound (the caller gates
    that as a caller error); problems in the NEW bundle are
    regressions, not caller errors."""
    cmp = BundleCompare(baseline_file=baseline_file, new_file=new_file)

    def reg(category, detail):
        cmp.regressions.append(BundleFinding(category, detail))

    for problem in bundle_problems(new):
        reg("schema", f"new bundle: {problem}")
    if cmp.regressions:
        return cmp  # nothing else in it can be trusted

    bsec = baseline.get("sections", {})
    nsec = new.get("sections", {})

    for name in BUNDLE_SECTIONS:
        if name in bsec and name not in nsec:
            reg("evidence", f"section '{name}' was sealed in the "
                            f"baseline and is missing now")
        elif name in nsec and name not in bsec:
            cmp.improvements.append(f"section '{name}' gained")

    btape, ntape = bsec.get("tape", {}), nsec.get("tape", {})
    if isinstance(btape.get("events"), int) \
            and isinstance(ntape.get("events"), int):
        if ntape["events"] < btape["events"]:
            reg("evidence", f"tape shrank: {btape['events']} → "
                            f"{ntape['events']} events")
        elif ntape["events"] > btape["events"]:
            cmp.improvements.append(
                f"tape grew: {btape['events']} → {ntape['events']} "
                f"events")
    bruns, nruns = btape.get("runs"), ntape.get("runs")
    if isinstance(bruns, list) and isinstance(nruns, list) \
            and len(nruns) < len(bruns):
        reg("evidence", f"tape lost runs: {len(bruns)} → {len(nruns)}")

    if "claims" in bsec and "claims" in nsec:
        b_hashes = {i.get("sha256") for i in
                    bsec["claims"].get("items", [])}
        n_hashes = {i.get("sha256") for i in
                    nsec["claims"].get("items", [])}
        lost = b_hashes - n_hashes
        if lost:
            reg("evidence", f"{len(lost)} sealed claim(s) lost "
                            f"(claim hashes no longer present)")
        elif len(n_hashes) > len(b_hashes):
            cmp.improvements.append(
                f"claims grew: {len(b_hashes)} → {len(n_hashes)}")

    if "skill_manifest" in bsec and "skill_manifest" in nsec:
        b_files = {f.get("path"): f.get("sha256") for f in
                   bsec["skill_manifest"].get("files", [])}
        n_files = {f.get("path"): f.get("sha256") for f in
                   nsec["skill_manifest"].get("files", [])}
        for path in sorted(set(b_files) - set(n_files)):
            reg("evidence", f"skill file lost: {path}")
        for path in sorted(set(b_files) & set(n_files)):
            if b_files[path] != n_files[path]:
                cmp.notes.append(f"skill file changed: {path}")

    if "tool_outputs" in bsec and "tool_outputs" in nsec:
        b_tools = {e.get("tool") for e in bsec["tool_outputs"]}
        n_tools = {e.get("tool") for e in nsec["tool_outputs"]}
        for tool in sorted(t for t in b_tools - n_tools if t):
            reg("evidence", f"tool report lost: {tool}")

    bcost = bsec.get("cost_record", {}).get("total") \
        if "cost_record" in bsec else None
    ncost = nsec.get("cost_record", {}).get("total") \
        if "cost_record" in nsec else None
    b_total, n_total = _decimal_or_none(bcost), _decimal_or_none(ncost)
    if budget is not None:
        if n_total is None:
            reg("cost", "new bundle has no readable cost total — "
                        "the budget cannot be proven")
        elif n_total > budget:
            reg("cost", f"cost {n_total} exceeds budget {budget}")
    if b_total is not None and n_total is not None \
            and n_total != b_total:
        cmp.notes.append(f"cost moved: {b_total} → {n_total}")

    b_score = bsec.get("trustbadge", {}).get("composite") \
        if "trustbadge" in bsec else None
    n_score = nsec.get("trustbadge", {}).get("composite") \
        if "trustbadge" in nsec else None
    if isinstance(b_score, (int, float)) \
            and isinstance(n_score, (int, float)):
        if n_score < b_score:
            reg("score", f"TrustBadge composite degraded: "
                         f"{b_score} → {n_score}")
        elif n_score > b_score:
            cmp.improvements.append(
                f"TrustBadge composite improved: {b_score} → "
                f"{n_score}")
    return cmp


def print_bundle_text(c: BundleCompare):
    icon = "❌" if c.regressions else "✅"
    print("=" * 68)
    print(f"AgentCI bundle — {icon} {c.verdict}")
    print("=" * 68)
    if c.baseline_file:
        print(f"Baseline: {c.baseline_file}")
    if c.new_file:
        print(f"New:      {c.new_file}")
    print()
    print(f"🔻 Regressions ({len(c.regressions)}):")
    for f in c.regressions:
        print(f"  - [{f.category}] {f.detail}")
    if not c.regressions:
        print("  (none)")
    print()
    print(f"🔺 Improvements ({len(c.improvements)}):")
    for d in c.improvements:
        print(f"  - {d}")
    if not c.improvements:
        print("  (none)")
    if c.notes:
        print()
        print("Notes:")
        for d in c.notes:
            print(f"  - {d}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _load_or_exit(loader, path, what):
    try:
        return loader(path)
    except FileNotFoundError:
        msg = f"{what} file not found: {path}"
        print(f"error: {msg}", file=sys.stderr)
        print(json.dumps({"error": {"code": "file_not_found", "message": msg}}),
              file=sys.stderr)
        sys.exit(2)
    except json.JSONDecodeError as e:
        msg = f"{what} file is not valid JSON: {path} ({e})"
        print(f"error: {msg}", file=sys.stderr)
        print(json.dumps({"error": {"code": "invalid_json", "message": msg}}),
              file=sys.stderr)
        sys.exit(2)
    except ValueError as e:
        msg = f"{e}"
        print(f"error: {msg}", file=sys.stderr)
        print(json.dumps({"error": {"code": "invalid_input", "message": msg}}),
              file=sys.stderr)
        sys.exit(2)


def _resolve_stdin(path):
    """Materialize stdin to a temp file when path is '-' (the loaders
    are path-based). Returns (real_path, tmp_path_or_None)."""
    if path != "-":
        return path, None
    fd, name = tempfile.mkstemp(prefix="agentci-stdin-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(sys.stdin.read())
    return name, name


def _stdin_guard(a, b):
    """stdin can be read once: at most one input may be '-'."""
    if a == "-" and b == "-":
        msg = "only one input may come from stdin"
        print(f"error: {msg}", file=sys.stderr)
        print(json.dumps({"error": {"code": "invalid_args",
                                    "message": msg}}),
              file=sys.stderr)
        sys.exit(2)


def _label(path):
    return "<stdin>" if path == "-" else path


def cmd_run(args):
    _stdin_guard(args.cases, args.outputs)
    cases_path, tmp1 = _resolve_stdin(args.cases)
    outputs_path, tmp2 = _resolve_stdin(args.outputs)
    try:
        agent, cases = _load_or_exit(load_cases, cases_path, "cases")
        outputs = _load_or_exit(load_outputs, outputs_path, "outputs")
    finally:
        for tmp in (tmp1, tmp2):
            if tmp:
                Path(tmp).unlink(missing_ok=True)
    report = run_suite(agent, cases, outputs,
                       cases_file=_label(args.cases),
                       outputs_file=_label(args.outputs))
    payload = asdict(report)
    payload["tool"] = "agentci"
    payload["version"] = VERSION
    payload["schema_version"] = 1
    if args.save:
        Path(args.save).write_text(
            json.dumps(payload, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print_run_text(report)
        if args.save:
            print(f"\nReport saved to {args.save} — use it as a compare baseline.")
    # CI-friendly exit codes: 0 = all pass, 1 = any fail/missing, 2 = input error
    sys.exit(0 if report.verdict == "PASS" else 1)


def cmd_compare(args):
    _stdin_guard(args.baseline, args.new)
    base_path, tmp1 = _resolve_stdin(args.baseline)
    new_path, tmp2 = _resolve_stdin(args.new)
    try:
        baseline = _load_or_exit(load_report, base_path,
                                 "baseline report")
        new = _load_or_exit(load_report, new_path, "new report")
    finally:
        for tmp in (tmp1, tmp2):
            if tmp:
                Path(tmp).unlink(missing_ok=True)
    cmp = compare_reports(baseline, new,
                          baseline_file=_label(args.baseline),
                          new_file=_label(args.new))
    payload = asdict(cmp)
    payload["tool"] = "agentci"
    payload["version"] = VERSION
    payload["schema_version"] = 1
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print_compare_text(cmp)
    # 0 = no regression, 1 = regression found, 2 = input error
    sys.exit(1 if cmp.regressions else 0)


def load_bundle(path):
    with open(path, "rb") as fh:
        raw = fh.read()
    doc = json.loads(raw.decode("utf-8"))  # JSONDecodeError propagates
    if not isinstance(doc, dict):
        raise ValueError(f"bundle file is not a JSON object: {path}")
    return doc


def _caller_error(msg):
    print(f"error: {msg}", file=sys.stderr)
    print(json.dumps({"error": {"code": "invalid_input",
                                "message": msg}}), file=sys.stderr)
    sys.exit(2)


def cmd_bundle(args):
    _stdin_guard(args.baseline, args.new)
    base_path, tmp1 = _resolve_stdin(args.baseline)
    new_path, tmp2 = _resolve_stdin(args.new)
    try:
        baseline = _load_or_exit(load_bundle, base_path, "bundle")
        new = _load_or_exit(load_bundle, new_path, "bundle")
    finally:
        for tmp in (tmp1, tmp2):
            if tmp:
                Path(tmp).unlink(missing_ok=True)
    problems = bundle_problems(baseline)
    if problems:
        _caller_error("baseline is not a sound proof bundle: "
                      + "; ".join(problems))
    budget = None
    if args.budget is not None:
        budget = _decimal_or_none(args.budget)
        if budget is None:
            _caller_error(f"--budget is not a number: {args.budget!r}")
    c = compare_bundles(baseline, new, budget=budget,
                        baseline_file=_label(args.baseline),
                        new_file=_label(args.new))
    payload = asdict(c)
    payload["tool"] = "agentci"
    payload["version"] = VERSION
    payload["schema_version"] = 1
    payload["verdict"] = c.verdict
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print_bundle_text(c)
    # 0 = no regression, 1 = regression found, 2 = input error
    sys.exit(1 if c.regressions else 0)


CASES_TEMPLATE = {
    "agent": "sample-support-agent",
    "description": "Sample AgentCI suite. Edit the cases to match your agent, "
                   "save fresh outputs after each prompt change, and re-run.",
    "cases": [
        {
            "id": "refund-flow",
            "input": "I was charged twice for March.",
            "checks": [
                {"type": "contains", "value": "refund"},
                {"type": "contains", "value": "confirm"},
                {"type": "not_contains", "value": "guaranteed"},
                {"type": "min_length", "value": 40},
            ],
        },
        {
            "id": "password-reset",
            "input": "I forgot my password, how do I get back in?",
            "checks": [
                {"type": "contains", "value": "reset link"},
                {"type": "regex", "pattern": "expire|15 minutes|one hour"},
                {"type": "not_contains", "value": "tell me your password"},
            ],
        },
        {
            "id": "polite-tone",
            "input": "Your app is broken again, this is ridiculous.",
            "checks": [
                {"type": "contains", "value": "sorry"},
                {"type": "not_contains", "value": "calm down"},
                {"type": "min_length", "value": 30},
            ],
        },
    ],
}

OUTPUTS_TEMPLATE = {
    "refund-flow": (
        "I'm sorry about the duplicate charge. I can issue a refund for the "
        "second March charge — please confirm you'd like me to go ahead, and "
        "I'll process it right away. Refunds usually land in 3-5 days."
    ),
    "password-reset": (
        "No problem — I've sent a password reset link to your email. The link "
        "will expire in one hour. Never share your password with anyone, "
        "including us."
    ),
    "polite-tone": (
        "I'm really sorry you're hitting this again — that's frustrating. "
        "Tell me what you were doing when it broke and I'll dig in right now."
    ),
}


def cmd_init(args):
    base = Path(args.dir) if args.dir else Path.cwd()
    base.mkdir(parents=True, exist_ok=True)
    cases_path = base / "agentci.cases.json"
    outputs_path = base / "agentci.outputs.json"
    for p in (cases_path, outputs_path):
        if p.exists() and not args.force:
            print(f"{p} exists — use --force to overwrite")
            return
    cases_path.write_text(json.dumps(CASES_TEMPLATE, indent=2), encoding="utf-8")
    outputs_path.write_text(json.dumps(OUTPUTS_TEMPLATE, indent=2), encoding="utf-8")
    print(f"created {cases_path} and {outputs_path}")
    print("try it:  agentci run agentci.cases.json agentci.outputs.json "
          "--save baseline_report.json")


def main():
    ap = argparse.ArgumentParser(
        prog="agentci",
        description="Regression harness for AI agents. "
                    "You changed the prompt — did the agent get better or worse?")
    ap.add_argument("--version", action="version",
                    version=f"agentci {VERSION}")
    ap.add_argument("--no-color", action="store_true",
                    help="disable color output (accepted harmlessly; no color emitted)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="evaluate saved outputs against a case suite")
    r.add_argument("cases", help="path to the cases JSON file "
                               "('-' reads stdin; one input only)")
    r.add_argument("outputs", help="path to the saved outputs JSON file")
    r.add_argument("--json", action="store_true",
                   help="machine-readable JSON report")
    r.add_argument("--save", metavar="PATH",
                   help="also save the JSON report to PATH (a compare baseline)")
    r.set_defaults(fn=cmd_run)

    c = sub.add_parser("compare", help="diff two run reports; exit 1 on regression")
    c.add_argument("baseline", help="path to the baseline run report JSON "
                                    "('-' reads stdin; one input only)")
    c.add_argument("new", help="path to the new run report JSON")
    c.add_argument("--json", action="store_true",
                   help="machine-readable JSON output")
    c.set_defaults(fn=cmd_compare)

    b = sub.add_parser(
        "bundle",
        help="regression-check a new TraceTape proof bundle against "
             "a baseline bundle")
    b.add_argument("baseline")
    b.add_argument("new")
    b.add_argument("--budget", default=None,
                   help="fail if the new bundle's cost total exceeds "
                        "this amount (e.g. 1.50)")
    b.add_argument("--json", action="store_true",
                   help="machine-readable JSON output")
    b.set_defaults(fn=cmd_bundle)

    i = sub.add_parser("init", help="scaffold a sample suite + passing outputs")
    i.add_argument("--force", action="store_true")
    i.add_argument("--dir", help="directory to write the sample files into")
    i.set_defaults(fn=cmd_init)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
