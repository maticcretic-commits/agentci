"""Tests for AgentCI checks, run scoring, and compare."""
import argparse
import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from agentci import (VERSION, cmd_compare, cmd_init, cmd_run, compare_reports,
                     evaluate_case, evaluate_check, load_cases, load_outputs,
                     main, run_suite)


# ---------------------------------------------------------------------------
# Individual check types
# ---------------------------------------------------------------------------

def test_contains_pass_and_fail():
    out = "I can issue a refund for the duplicate charge."
    assert evaluate_check({"type": "contains", "value": "refund"}, out).status == "pass"
    assert evaluate_check({"type": "contains", "value": "invoice"}, out).status == "fail"


def test_contains_is_case_insensitive():
    out = "Refund processed."
    assert evaluate_check({"type": "contains", "value": "refund"}, out).status == "pass"
    assert evaluate_check({"type": "contains", "value": "REFUND"}, out).status == "pass"


def test_not_contains():
    out = "Refunds usually land in 3-5 days."
    assert evaluate_check({"type": "not_contains", "value": "guaranteed"}, out).status == "pass"
    bad = evaluate_check({"type": "not_contains", "value": "usually"}, out)
    assert bad.status == "fail"
    assert "forbidden" in bad.detail


def test_regex_pass_and_fail():
    out = "The link will expire in one hour."
    assert evaluate_check({"type": "regex", "pattern": r"expire|15 minutes"}, out).status == "pass"
    assert evaluate_check({"type": "regex", "pattern": r"\b\d{4}\b"}, out).status == "fail"


def test_regex_invalid_pattern_fails_cleanly():
    r = evaluate_check({"type": "regex", "pattern": "([unclosed"}, "anything")
    assert r.status == "fail"
    assert "invalid regex" in r.detail


def test_min_length():
    out = "x" * 50
    assert evaluate_check({"type": "min_length", "value": 40}, out).status == "pass"
    assert evaluate_check({"type": "min_length", "value": 60}, out).status == "fail"
    bad = evaluate_check({"type": "min_length", "value": "soon"}, out)
    assert bad.status == "fail"


def test_unknown_check_type_fails_with_reason():
    r = evaluate_check({"type": "vibes", "value": "good"}, "some output")
    assert r.status == "fail"
    assert "unknown check type" in r.detail


# ---------------------------------------------------------------------------
# Case evaluation + run scoring
# ---------------------------------------------------------------------------

CASES = [
    {"id": "a", "input": "q1",
     "checks": [{"type": "contains", "value": "refund"}]},
    {"id": "b", "input": "q2",
     "checks": [{"type": "min_length", "value": 10},
                {"type": "not_contains", "value": "sorry"}]},
    {"id": "c", "input": "q3",
     "checks": [{"type": "regex", "pattern": r"\d+"}]},
]


def test_case_pass_requires_all_checks():
    outputs = {"b": "here is a fairly long answer"}
    r = evaluate_case(CASES[1], outputs)
    assert r.status == "pass"
    outputs = {"b": "sorry, short"}  # trips not_contains AND is long enough
    r = evaluate_case(CASES[1], outputs)
    assert r.status == "fail"


def test_run_scoring():
    outputs = {"a": "refund on its way",
               "b": "a sufficiently long answer",
               "c": "no digits here"}
    report = run_suite("test-agent", CASES, outputs)
    assert report.total == 3
    assert report.passed == 2
    assert report.failed == 1
    assert report.missing == 0
    assert report.score == 67
    assert report.verdict == "FAIL"


def test_run_all_pass_verdict():
    outputs = {"a": "refund", "b": "a sufficiently long answer", "c": "order 42"}
    report = run_suite("test-agent", CASES, outputs)
    assert report.verdict == "PASS"
    assert report.score == 100


def test_missing_output_handling():
    outputs = {"a": "refund"}  # b and c have no saved output
    report = run_suite("test-agent", CASES, outputs)
    by_id = {r.id: r for r in report.results}
    assert by_id["b"].status == "missing"
    assert by_id["c"].status == "missing"
    assert "no output saved" in by_id["b"].reason
    assert report.missing == 2
    assert report.passed == 1
    assert report.verdict == "FAIL"  # missing counts as not passing


def test_extra_outputs_are_ignored():
    outputs = {"a": "refund", "b": "a sufficiently long answer",
               "c": "order 42", "zzz": "not in the suite"}
    report = run_suite("test-agent", CASES, outputs)
    assert report.total == 3
    assert report.verdict == "PASS"


# ---------------------------------------------------------------------------
# Loading / file formats
# ---------------------------------------------------------------------------

def write(tmp, name, obj):
    p = Path(tmp) / name
    p.write_text(json.dumps(obj), encoding="utf-8")
    return str(p)


def test_load_cases_object_and_list_forms():
    with tempfile.TemporaryDirectory() as tmp:
        p = write(tmp, "c.json", {"agent": "x", "cases": CASES})
        agent, cases = load_cases(p)
        assert agent == "x" and len(cases) == 3
        p2 = write(tmp, "c2.json", CASES)
        agent2, cases2 = load_cases(p2)
        assert agent2 == "" and len(cases2) == 3


def test_load_cases_rejects_malformed():
    with tempfile.TemporaryDirectory() as tmp:
        p = write(tmp, "bad.json", {"cases": [{"input": "no id here"}]})
        with pytest.raises(ValueError):
            load_cases(p)
        p2 = write(tmp, "bad2.json", {"cases": [{"id": "a", "checks": []}]})
        with pytest.raises(ValueError):
            load_cases(p2)


def test_load_outputs_dict_and_list_forms():
    with tempfile.TemporaryDirectory() as tmp:
        p = write(tmp, "o.json", {"a": "one"})
        assert load_outputs(p) == {"a": "one"}
        p2 = write(tmp, "o2.json", [{"id": "a", "output": "one"},
                                    {"id": "b", "output": "two"}])
        assert load_outputs(p2) == {"a": "one", "b": "two"}


# ---------------------------------------------------------------------------
# JSON validity of reports
# ---------------------------------------------------------------------------

def test_report_json_round_trip():
    outputs = {"a": "refund", "b": "a sufficiently long answer"}
    report = run_suite("test-agent", CASES, outputs)
    blob = json.dumps(asdict(report))
    back = json.loads(blob)
    assert back["agent"] == "test-agent"
    assert back["results"][0]["id"] == "a"
    assert back["results"][2]["status"] == "missing"


def test_cmd_run_json_output_parses(capsys):
    with tempfile.TemporaryDirectory() as tmp:
        cases = write(tmp, "cases.json", {"agent": "x", "cases": CASES})
        outputs = write(tmp, "outputs.json",
                        {"a": "refund", "b": "a sufficiently long answer",
                         "c": "order 42"})
        args = argparse.Namespace(cases=cases, outputs=outputs,
                                  json=True, save=None)
        with pytest.raises(SystemExit) as ei:
            cmd_run(args)
        assert ei.value.code == 0
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["agent"] == "x"
        assert len(parsed["results"]) == 3


def test_cmd_run_exit_1_on_failure(capsys):
    with tempfile.TemporaryDirectory() as tmp:
        cases = write(tmp, "cases.json", {"agent": "x", "cases": CASES})
        outputs = write(tmp, "outputs.json", {"a": "no match here"})
        args = argparse.Namespace(cases=cases, outputs=outputs,
                                  json=False, save=None)
        with pytest.raises(SystemExit) as ei:
            cmd_run(args)
        assert ei.value.code == 1


def test_cmd_run_missing_file_exit_2():
    args = argparse.Namespace(cases="/nope/cases.json", outputs="/nope/o.json",
                              json=False, save=None)
    with pytest.raises(SystemExit) as ei:
        cmd_run(args)
    assert ei.value.code == 2


# ---------------------------------------------------------------------------
# Compare
# ---------------------------------------------------------------------------

def make_report(statuses):
    return {"agent": "x", "timestamp": "t",
            "results": [{"id": cid, "status": st, "checks": []}
                        for cid, st in statuses.items()]}


def test_compare_detects_regression():
    base = make_report({"a": "pass", "b": "pass", "c": "fail"})
    new = make_report({"a": "pass", "b": "fail", "c": "fail"})
    cmp = compare_reports(base, new)
    assert [d.id for d in cmp.regressions] == ["b"]
    assert cmp.regressions[0].baseline_status == "pass"
    assert cmp.regressions[0].new_status == "fail"
    assert cmp.improvements == []
    assert sorted(d.id for d in cmp.unchanged) == ["a", "c"]
    assert cmp.verdict == "REGRESSION"


def test_compare_detects_improvement_and_missing_regression():
    base = make_report({"a": "fail", "b": "pass"})
    new = make_report({"a": "pass", "b": "missing"})
    cmp = compare_reports(base, new)
    assert [d.id for d in cmp.improvements] == ["a"]
    assert [d.id for d in cmp.regressions] == ["b"]  # pass → missing regresses


def test_compare_added_and_removed():
    base = make_report({"a": "pass", "old": "pass"})
    new = make_report({"a": "pass", "fresh": "fail"})
    cmp = compare_reports(base, new)
    assert [d.id for d in cmp.added] == ["fresh"]
    assert [d.id for d in cmp.removed] == ["old"]
    assert cmp.regressions == []
    assert cmp.verdict == "OK"


def test_compare_exit_codes(tmp_path):
    base = make_report({"a": "pass"})
    ok_new = make_report({"a": "pass"})
    bad_new = make_report({"a": "fail"})
    bp = write(tmp_path, "base.json", base)
    op = write(tmp_path, "ok.json", ok_new)
    np_ = write(tmp_path, "bad.json", bad_new)
    with pytest.raises(SystemExit) as ei:
        cmd_compare(argparse.Namespace(baseline=bp, new=op, json=True))
    assert ei.value.code == 0
    with pytest.raises(SystemExit) as ei:
        cmd_compare(argparse.Namespace(baseline=bp, new=np_, json=True))
    assert ei.value.code == 1


# ---------------------------------------------------------------------------
# init → run round trip
# ---------------------------------------------------------------------------

def test_init_then_run_round_trip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cmd_init(argparse.Namespace(force=False, dir=None))
    cases_path = tmp_path / "agentci.cases.json"
    outputs_path = tmp_path / "agentci.outputs.json"
    assert cases_path.exists() and outputs_path.exists()
    agent, cases = load_cases(str(cases_path))
    outputs = load_outputs(str(outputs_path))
    report = run_suite(agent, cases, outputs)
    assert report.verdict == "PASS", asdict(report)
    assert report.score == 100


def test_init_refuses_to_overwrite(tmp_path, capsys):
    cmd_init(argparse.Namespace(force=False, dir=str(tmp_path)))
    cmd_init(argparse.Namespace(force=False, dir=str(tmp_path)))
    assert "exists" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Envelope / version / error envelope (Phase 1 conventions)
# ---------------------------------------------------------------------------

def test_run_json_envelope_keys(capsys):
    with tempfile.TemporaryDirectory() as tmp:
        cases = write(tmp, "cases.json", {"agent": "x", "cases": CASES})
        outputs = write(tmp, "outputs.json",
                        {"a": "refund", "b": "a sufficiently long answer",
                         "c": "order 42"})
        args = argparse.Namespace(cases=cases, outputs=outputs,
                                  json=True, save=None)
        with pytest.raises(SystemExit) as ei:
            cmd_run(args)
        assert ei.value.code == 0
        parsed = json.loads(capsys.readouterr().out)
        assert parsed["tool"] == "agentci"
        assert parsed["version"] == VERSION == "1.0.0"
        assert parsed["schema_version"] == 1


def test_run_save_file_has_envelope_keys(capsys, tmp_path):
    with tempfile.TemporaryDirectory() as tmp:
        cases = write(tmp, "cases.json", {"agent": "x", "cases": CASES})
        outputs = write(tmp, "outputs.json",
                        {"a": "refund", "b": "a sufficiently long answer",
                         "c": "order 42"})
        save_path = str(tmp_path / "saved.json")
        args = argparse.Namespace(cases=cases, outputs=outputs,
                                  json=False, save=save_path)
        with pytest.raises(SystemExit) as ei:
            cmd_run(args)
        assert ei.value.code == 0
        capsys.readouterr()
        saved = json.loads(Path(save_path).read_text(encoding="utf-8"))
        assert saved["tool"] == "agentci"
        assert saved["version"] == "1.0.0"
        assert saved["schema_version"] == 1
        assert saved["agent"] == "x"
        assert len(saved["results"]) == 3


def test_version_flag_exits_0(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["agentci", "--version"])
    with pytest.raises(SystemExit) as ei:
        main()
    assert ei.value.code == 0
    assert capsys.readouterr().out.strip() == "agentci 1.0.0"


def test_missing_file_error_envelope(capsys):
    args = argparse.Namespace(cases="/nope/cases.json", outputs="/nope/o.json",
                              json=False, save=None)
    with pytest.raises(SystemExit) as ei:
        cmd_run(args)
    assert ei.value.code == 2
    err = capsys.readouterr().err
    lines = [ln for ln in err.strip().splitlines() if ln.strip()]
    assert lines[0].startswith("error: ")
    envelope = json.loads(lines[-1])
    assert envelope["error"]["code"] == "file_not_found"
    assert "cases file not found" in envelope["error"]["message"]


def test_compare_accepts_old_shape_baseline(capsys, tmp_path):
    # Old baselines have no envelope keys; they must still load/compare.
    base = make_report({"a": "pass", "b": "pass"})
    new = make_report({"a": "pass", "b": "fail"})
    # Strip envelope keys if present (make_report has none, but be explicit)
    for rep in (base, new):
        for k in ("tool", "version", "schema_version"):
            rep.pop(k, None)
    bp = write(tmp_path, "old_base.json", base)
    np_ = write(tmp_path, "old_new.json", new)
    with pytest.raises(SystemExit) as ei:
        cmd_compare(argparse.Namespace(baseline=bp, new=np_, json=True))
    assert ei.value.code == 1  # regression b: pass -> fail
    parsed = json.loads(capsys.readouterr().out)
    assert [d["id"] for d in parsed["regressions"]] == ["b"]


def test_compare_json_envelope_keys(capsys, tmp_path):
    base = make_report({"a": "pass"})
    new = make_report({"a": "pass"})
    bp = write(tmp_path, "base.json", base)
    np_ = write(tmp_path, "new.json", new)
    with pytest.raises(SystemExit) as ei:
        cmd_compare(argparse.Namespace(baseline=bp, new=np_, json=True))
    assert ei.value.code == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["tool"] == "agentci"
    assert parsed["version"] == "1.0.0"
    assert parsed["schema_version"] == 1


# ---------------------------------------------------------------------------
# Phase 2: stdin inputs (pipe composability)
# ---------------------------------------------------------------------------

import subprocess  # noqa: E402


def test_run_stdin_outputs():
    cli = Path(__file__).parent.parent / "src" / "agentci.py"
    ex = Path(__file__).parent.parent / "examples"
    proc = subprocess.run(
        [sys.executable, str(cli), "run", str(ex / "cases.json"), "-",
         "--json"],
        input=(ex / "outputs.v2.json").read_text(),
        capture_output=True, text=True)
    assert proc.returncode in (0, 1)
    data = json.loads(proc.stdout)  # stdout is pure JSON
    assert data["tool"] == "agentci"
    assert data["outputs_file"] == "<stdin>"


def test_run_stdin_both_sides_rejected():
    cli = Path(__file__).parent.parent / "src" / "agentci.py"
    proc = subprocess.run(
        [sys.executable, str(cli), "run", "-", "-"],
        input="{}", capture_output=True, text=True)
    assert proc.returncode == 2  # stdin can only be read once
