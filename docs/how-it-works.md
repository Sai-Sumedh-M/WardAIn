# How WardAIn works

Every section: what the component does, where it lives, which scenario proves it, and likely judge
questions. Run scenarios with `python -m scenarios.runner --scenario <name> --mode both`.

## The big picture

Agents never talk to tools directly. They send every action (tool call or LLM call) as a signed
envelope to the gateway's `POST /call`. The gateway runs a fixed chain of checks, forwards allowed
calls to the tools service, records everything and watches for misbehaviour.

**Q: What stops an agent from skipping the gateway?** Network isolation (see Docker below): the agents
container is on an internal network that contains only the gateway.
**Q: Is it tied to one agent framework?** No. The gateway only sees HTTP envelopes. The workflow
step functions are plain Python and can also run as LangGraph nodes (Phase 5b).

## Identity and replay protection

Each agent has an Ed25519 key pair. It registers its public key, proves possession by signing a
challenge to get a short-lived (5 min) JWT, and signs every call envelope. The gateway checks the
token, the agent's role, the envelope signature, the clock (30 s) and a one-time nonce.
File: `gateway/identity.py`, `common/crypto.py`.
Proven by: `impersonation` (forged and stolen-token calls all blocked) and `replay_attack` (a captured
call resent 5 times is blocked by the nonce).

**Q: A stolen token?** The token alone is useless: calls must also carry a signature from the agent's
private key (`stolen_token_wrong_key`).
**Q: What about replaying a valid signed request?** Each request has a nonce stored for 120 s; a repeat
is rejected as `replay_detected`.
**Q: What happens when an agent is halted?** Its tokens are revoked.

## Kill-switch

Humans can pause, resume, quarantine or halt one agent, or halt everything. Quarantined agents may
only call the LLM. The gateway re-reads agent status from the database right before a side-effecting
call is forwarded, so even in-flight work is stopped.
File: `gateway/killswitch.py`. Proven by: `runaway_loop` (after HALT ALL, zero payments land;
`post_kill_leakage` 0, `kill_latency_ms` measured).

**Q: How fast is the kill?** The scorecard reports `kill_latency_ms`; it is one DB write plus a
check on every call.
**Q: Can the agent ignore the kill?** No, enforcement is in the gateway, not in the agent.

## Policy and approvals

Role permissions come from `gateway/policies.yaml`: the Retriever may read customers, the Executor may
refund and email, and so on. Refunds must be for the run's customer, emails may only go to the company
domain or the customer's own address, and refunds over 500 need approval (auto-approve, auto-reject
or a human click).
File: `gateway/policy.py`. Proven by: `role_violation` (a Retriever trying `issue_refund` is blocked).

**Q: Why not an engine like OPA?** One small YAML file keeps the demo auditable; OPA is on the roadmap.
**Q: What if no human answers an approval?** It is rejected after 30 s (`approval_timeout`).

## Guards (input, output, data-flow)

Arguments are schema-checked; identical repeated calls are stopped (loop detection); outbound emails
containing PII are blocked; prompt-injection text in tool results or LLM input is replaced with
`[REMOVED: untrusted instruction]`.
File: `gateway/guards.py`. Proven by: `prompt_injection` (planted instruction is stripped, no
rogue refund), `pii_exfiltration` (email to an outside address with PII is blocked).

**Q: Can regexes catch every injection?** No. They are a cheap first layer; the real defences are
role policy and customer scoping, which hold even if an injection gets through.
**Q: Does it block legitimate traffic?** `false_block_rate` on `normal_run` is 0.

## Idempotency and chaos

Side-effecting calls carry an idempotency key. If a tool commits but the response times out, the
agent retries with the same key and the gateway replays the stored answer instead of executing again.
The chaos engine injects latency, errors, timeouts-after-commit, corrupted output, prompt injection
and wrong values for testing.
Files: `gateway/pipeline.py`, `gateway/chaos.py`. Proven by: `duplicate_on_retry` (OFF: duplicate
refund; ON: exactly one), `corrupted_output` (ON rejects the bad output and retries).

**Q: Is the chaos engine active in production?** It is only configured by the admin reset; with no
scenario it does nothing.

## Circuit breaker

Tracks recent tool outcomes; if more than half fail it opens for 2 s so agents stop hammering a sick
service, then lets one probe through (half-open) and closes on success.
File: `gateway/monitor.py`. Proven by: `tool_outage` (`mttd_ms` and `mttr_ms`).

**Q: What do agents see while it is open?** A 503 `circuit_open`; the client retries with backoff.

## Run ledger, plan deviation, monitor

The gateway records every step and event. A Planner submits a plan; each real tool call is matched to a
plan entry, and unmatched calls are flagged `plan_deviation`. The monitor quarantines an agent on
repeated blocks, two deviations, or more than three refunds in a run.
Files: `gateway/recovery.py`, `gateway/monitor.py`, `gateway/store.py`. Proven by:
`compromised_agent`.

**Q: Why plan-based detection?** It catches harmful-but-permitted actions (the role may refund, but
this refund was never in the plan).
**Q: Could a quarantine be false?** `false_block_rate` is 0 on normal runs, and a human can resume.

## Rollback and recovery

When an agent is quarantined, its steps from the first deviation onward (and anything derived from
them) are marked tainted, and the recorded undo actions (reverse refund, recall email, restore field)
run newest-first. The run is left in `rolled_back` with the last good checkpoint, and a fresh
Executor resumes from the next plan step.
File: `gateway/recovery.py`, `agents/workflow.py`. Proven by: `compromised_agent`
(`residual_bad_side_effects` 0, `rollback_fidelity`, `blast_radius_agents` 1).

**Q: What can't be rolled back?** Truly external effects. The attacker email is recalled in the mock
outbox; a real SMTP send could not be, which is why the policy and PII guards block it first.
**Q: How is the state of the good work kept?** The refund made before the compromise is not tainted, so
the replacement agent continues instead of restarting.

## Network isolation

`agents` sits only on the internal `agentnet`; `tools` only on the internal `toolnet`; the gateway is
on both. Bypass is impossible by construction, not by convention.
File: `docker-compose.yml`. Proven by the demo's bypass check (DNS lookup of `tools` from the agents
container fails).

**Q: Is local mode isolated?** No; `dev_local.sh` is for development. The isolation claim applies to Docker.

## LLM backend

Mock mode is deterministic (used for all official numbers). NVIDIA mode calls an OpenAI-compatible
endpoint through httpx. All LLM traffic also goes through the gateway so it is authenticated,
sanitised and logged.
Files: `gateway/llm.py`, `gateway/mock_llm.py`.

**Q: Why mock?** Reproducible, offline results; the mock deliberately simulates a model that obeys
injected instructions.

## Scenario harness and scorecard

The runner resets state, registers agents, runs a workflow with WardAIn OFF and ON, checks ground
truth against the tools' real state, and records events. The scorecard turns this into metrics.
Files: `scenarios/runner.py`, `scenarios/attacks.py`, `scenarios/scorecard.py`,
`scenarios/definitions/*.yaml`.

**Q: How do you know the numbers are honest?** Success is judged from the tools database (payments,
outbox, balances), not from what the agents or gateway claim.
**Q: Why OFF mode?** The same code path with the protections skipped gives a true baseline.

## NVIDIA guard layers (Phase 7)

**What it does:** after the regex sanitizer, tool results of `get_customer` / `get_payments` can also be classified by NVIDIA models: `nemotron-3.5-content-safety` (`safety`) and the Nemotron judge (`judge`), selected by the gateway config `nvidia_guard` (`off|safety|judge|both`). A flagged string field is replaced with `[REMOVED: untrusted instruction]` and emits `injection_sanitized` / `anomaly_detected` with the `layer` (`regex`, `nvidia_safety`, `nvidia_judge`). If the NVIDIA call fails, `guard_unavailable` is emitted and `NVIDIA_GUARD_FAIL` decides open or closed.
**Files:** `gateway/nvidia_guard.py`, `gateway/pipeline.py` (step 12). **Scenario:** `prompt_injection_semantic`.
**Official scorecard:** uses `GUARD_BACKEND=mock` (offline test double), so it stays deterministic. Real NVIDIA rates come from the red-team campaign.

**Red-team campaign:** `scenarios/redteam.py` generates attacks with the LLM, adds 15 seed attacks, human-reviews them (4 ambiguous items excluded) and classifies 41 attacks and 10 benign notes with each layer combination (`results/redteam/redteam_report.md`):

| Layers | Detection (all) | Seed | Generated | False positives |
|---|---|---|---|---|
| regex | 12.2% | 33.3% | 0.0% | 0.0% |
| regex+safety | 63.4% | 86.7% | 50.0% | 0.0% |
| regex+judge | 51.2% | 86.7% | 30.8% | 0.0% |
| regex+safety+judge | 73.2% | 100.0% | 57.7% | 0.0% |

**Q: Why not just use the LLM judge?** Each layer misses different attacks; safety+judge together beats either alone, and neither is relied on alone (see the campaign).
**Q: What if the NVIDIA API is down or rate-limited?** Calls are cached and throttled (`NVIDIA_GUARD_MIN_INTERVAL`), 429s are retried, and a failure emits `guard_unavailable`; `NVIDIA_GUARD_FAIL=closed` removes the field instead of passing it.
**Q: 26% of attacks evade all layers. Is that acceptable?** It is why the guard is defence in depth; the next section measures what happens to those.

## End-to-end attack campaign (Phase 8)

**What it does:** `scenarios/campaign.py` plants each of the 41 reviewed attacks in customer C001's record and runs the whole workflow with real LLM agents (Nemotron 3.5 Lightning, self-hosted with vLLM on an NVIDIA L40S via Brev; guards on the hosted NVIDIA API). The executor (`executor_mode: campaign`) reads the record with a deliberately compliant prompt and may act on it. Ground truth is the tool state, once with WardAIn OFF and once ON. **Files:** `scenarios/campaign.py`, `agents/workflow.py`. Report: `results/campaign/campaign_report.md`.

| Mode | Runs | Hijack rate | Damage rate | Task success |
|---|---|---|---|---|
| OFF | 41 | 68.3% | 61.0% | 39.0% |
| ON | 41 | 9.8% | 0.0% | 100.0% |

In the 32 ON runs where the attack was active, detection stopped 28 (nvidia_safety 20, regex 5, nvidia_judge 3), policy blocked 2 (`customer_out_of_scope`, `approval_rejected`), quarantine plus compensating rollback contained 2, and 0 damage got through. Quarantine events carry the run's `run_id`, which is how containment is attributed to a run.
Caveats: one run per attack and mode, one model, a small sample.

**Q: Why does ON show damage 0 when detection is only 73%?** The 4 hijacked ON runs were stopped behind detection by policy (recipient/customer scope, approval gate) or by the monitor quarantining the agent and rolling back its side effects.
**Q: Is the compliant prompt cheating?** It is the worst case for the gateway: an agent that obeys any instruction in data. A model that resists would make OFF look better; the ON result does not depend on it.
**Q: How do you know a quarantine belongs to a given run?** `agent_quarantined` events written by the monitor carry the run's `run_id`; manual console actions leave it null.

## Human-on-the-loop review (Phase 9)

**What it does:** when the two NVIDIA layers disagree about a field (exactly one flags it), the gateway does not guess. With config `hitl=ambiguous` it parks the tool result in a `data_review` approval (`approvals.kind`, `payload_json` holds the original and sanitized results and the flagged fields) and emits `review_requested`. A human decides in the console: Block releases the sanitized record, Allow original releases the raw one. No answer within `review_timeout_seconds` (60) releases the sanitized record. Both layers flagging still removes the field automatically (`layer: nvidia_both`), and neither flagging keeps it. The step is recorded once with the released result; `review_decided` carries `decision` and `by` (`human|auto|timeout`).
**Files:** `gateway/pipeline.py` (`_nvidia_review`, `_request_review`, `_decide_review`), `gateway/nvidia_guard.py` (`check_both`), `gateway/static/dashboard.html`, `scenarios/redteam.py` (`escalation`). **Scenario:** `prompt_injection_ambiguous`. **Tests:** `tests/test_hitl.py`.
**Numbers:** from the cached red-team verdicts, 25.5% of items (13 of 51, all attacks) would be escalated; automatic coverage of attacks is 41.5%, rising to 73.2% if the human blocks each escalation (`results/redteam/escalation.md`).

**Q: Why not let the LLM judge decide ambiguous cases?** The two layers are the decision makers and they disagree; breaking the tie with a third model call adds latency and another thing that can be fooled. A person sees the text and the two verdicts.
**Q: What if no human is watching?** The default is safe: after 60 s the sanitized record is released and the run continues. A wrong block costs one missing note field, not a leaked action.
**Q: Doesn't that make the agent slow?** Only for the 25% of items that are escalated, and only while a human is expected to be present; clear attacks and clear benign data never wait.
**Q: Does a human approval bypass the other controls?** No. Allowing the original only releases the data; policy, scope, recipient and guard checks still apply to whatever the agent then tries to do.

## Dashboard

Streamlit app over the admin API: live agent status and controls, event stream, approvals, tool state,
a scenario launcher and the scorecard. File: `dashboard/app.py`.

**Q: Can I stop things from the UI?** Yes, per-agent Pause/Resume/Quarantine/Halt and HALT ALL.
