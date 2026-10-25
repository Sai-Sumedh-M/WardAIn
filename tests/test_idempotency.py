CHAOS = [{"tool": "issue_refund", "fault": "timeout_after_commit", "times": 1}]


def _run(admin, make_agent, mode):
    admin.reset(mode=mode, scenario={"name": "t", "chaos": CHAOS, "seed": 1},
                approval_mode="auto_reject", monitor_enabled=True)
    executor = make_agent("executor")
    res = executor.call("issue_refund", {"customer_id": "C001", "amount": 49.99}, "r1")
    return res, [p for p in admin.tool_state()["payments"] if p["status"] == "completed"]


def test_timeout_after_commit_retry_no_duplicate(admin, make_agent):
    res, payments = _run(admin, make_agent, "on")
    assert res.ok and len(payments) == 1
    assert [e for e in admin.events(run_id="r1") if e["type"] == "idempotent_replay"]


def test_timeout_after_commit_off_mode_duplicates(admin, make_agent):
    res, payments = _run(admin, make_agent, "off")
    assert res.ok and len(payments) == 2
