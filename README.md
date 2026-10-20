# AgentCI 🔁

**You changed the prompt. Did the agent get better or worse?**

Every agent team has lived this: someone tweaks a system prompt, the demo
looks great, it ships — and a behavior that used to work quietly breaks.
There was no test run to compare against, so nobody finds out until a
user does.

AgentCI is a regression harness for AI agents. Keep a suite of test cases
— an input plus the checks a good output must pass — run it against saved
agent outputs, and save the report as a baseline. After the next prompt
change, run again and **compare**: regressions fail the build.

Offline-first: AgentCI evaluates saved outputs and never calls a live
model API, so it runs anywhere — including your CI pipeline.

```bash
python src/agentci.py init
python src/agentci.py run agentci.cases.json agentci.outputs.json --save baseline_report.json
```

## The checks

Each case carries a list of checks. An output passes the case only if it
passes **every** check.

| Check | Passes when |
|-------|-------------|
| `contains` | Output contains the value (case-insensitive) |
| `not_contains` | Output does **not** contain the value (case-insensitive) |
| `regex` | Output matches the pattern (`re.search`) |
| `min_length` | Output is at least N characters |

A case with no saved output is **missing** — reported loudly, and counted
as not passing. Unknown check types fail with a reason, never silently.

## Quick start

```bash
# 1. Scaffold a sample suite + passing outputs:
agentci init

# 2. Run it and save the report as your baseline:
agentci run agentci.cases.json agentci.outputs.json --save baseline_report.json

# 3. Change your agent's prompt. Save its new outputs. Run again:
agentci run agentci.cases.json agentci.outputs.v2.json --save new_report.json

# 4. Diff the two runs — exit code 1 if anything regressed:
agentci compare baseline_report.json new_report.json

# Machine-readable output for scripts:
agentci run cases.json outputs.json --json
agentci compare baseline_report.json new_report.json --json
```

Exit codes: `run` → `0` = all cases pass, `1` = any case fails or is
missing, `2` = input error. `compare` → `0` = no regression, `1` =
regression found, `2` = input error. Drop both in GitHub Actions.

## File formats

**Cases** (`agentci.cases.json`) — an object with the agent name and a
list of cases (a bare list of cases also works):

```json
{
  "agent": "support-agent",
  "cases": [
    {
      "id": "refund-flow",
      "input": "I was charged twice for March.",
      "checks": [
        {"type": "contains", "value": "refund"},
        {"type": "contains", "value": "confirm"},
        {"type": "not_contains", "value": "guaranteed"},
        {"type": "min_length", "value": 40}
      ]
    }
  ]
}
```

**Outputs** — a map of case id → output text (or a list of
`{"id": ..., "output": ...}` objects):

```json
{"refund-flow": "I can issue a refund — please confirm you'd like me to go ahead..."}
```

**Reports** — `run --save` writes the full run as JSON: per-case status,
per-check outcomes with reasons, and a timestamp. That file is what
`compare` consumes.

## Example

From `examples/` — a 4-case suite for a support agent. The v1 outputs
pass everything:

```
====================================================================
AgentCI run — support-agent
====================================================================

✅ refund-flow — PASS (4/4 checks)
✅ password-reset — PASS (3/3 checks)
✅ angry-user-tone — PASS (3/3 checks)
✅ data-privacy — PASS (2/2 checks)

Score: 4/4 cases passed (100%) — PASS
```

Then someone "improves" the prompt to sound more decisive, and the agent
starts issuing refunds *without asking for confirmation* — and promising
a guaranteed delivery date. The v2 run catches both:

```
❌ refund-flow — FAIL (2/4 checks)
     ✗ [contains] output does not contain 'confirm' (case-insensitive)
     ✗ [not_contains] output contains forbidden 'guaranteed' (case-insensitive)

Score: 3/4 cases passed (75%) — FAIL
```

And the compare against the baseline names the regression:

```
====================================================================
AgentCI compare — ❌ REGRESSION
====================================================================

🔻 Regressions — was PASS, now not (1):
  - refund-flow: pass → fail

1 regressions · 0 improvements · 3 unchanged · 4 cases compared
```

## Why

Prompt changes are code changes, but they ship without tests because
agent behavior isn't a unit test — it's a set of expectations about
outputs. AgentCI makes those expectations explicit, versioned, and
diffable, so "the new prompt feels better" becomes "the new prompt
passes everything the old one did, plus two cases it used to fail."

## Part of AgentForge

AgentCI is the third tool in the [AgentForge](https://github.com/maticcretic-commits)
trust layer for AI agents:

- **AgentProof** — verify agent work — [github.com/maticcretic-commits/agentproof](https://github.com/maticcretic-commits/agentproof)
- **PromptRE** — reverse-engineer agent prompts — [github.com/maticcretic-commits/promptre](https://github.com/maticcretic-commits/promptre)
- **AgentCI** — regression harness for agents (this repo)
- **SecretScan** — scan agent outputs for leaked secrets — [github.com/maticcretic-commits/secretscan](https://github.com/maticcretic-commits/secretscan)
- **CostLedger** — cost accounting for agent runs — [github.com/maticcretic-commits/costledger](https://github.com/maticcretic-commits/costledger)
- **SchemaGuard** — validate agent outputs against their contracts — [github.com/maticcretic-commits/schemaguard](https://github.com/maticcretic-commits/schemaguard)
- **SkillAudit** — audit agent skills for hidden instructions and quality — [github.com/maticcretic-commits/skillaudit](https://github.com/maticcretic-commits/skillaudit)
- **PromptDiff** — diff prompt versions — [github.com/maticcretic-commits/promptdiff](https://github.com/maticcretic-commits/promptdiff)
- **TraceTape** — the canonical run tape — [github.com/maticcretic-commits/tracetape](https://github.com/maticcretic-commits/tracetape)
- **PromptLint** — lint prompts for quality defects — [github.com/maticcretic-commits/promptlint](https://github.com/maticcretic-commits/promptlint)
- **ClaimCheck** — audit agent reports against their evidence — [github.com/maticcretic-commits/claimcheck](https://github.com/maticcretic-commits/claimcheck)
- **TrustBadge** — one trust score and badge for the whole family — [github.com/maticcretic-commits/trustbadge](https://github.com/maticcretic-commits/trustbadge)

AgentProof checks what an agent *did*; PromptRE checks what it was *told*; AgentCI checks it still *works* after you change it; SecretScan checks that nothing *leaked*; CostLedger checks what it *cost*; SchemaGuard checks the output still *fits the contract*; SkillAudit checks the skill was *safe to install*; PromptDiff checks what the prompt change *changed*; TraceTape keeps the whole run on *tape*; PromptLint keeps the prompt itself *linted clean*; ClaimCheck makes sure every claim is *backed*; TrustBadge puts the family's score *on one badge*.

## License

MIT
