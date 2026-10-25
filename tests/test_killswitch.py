CUSTOMER = {"customer_id": "C001"}


def test_pause_and_resume(admin, make_agent):
    agent = make_agent("retriever")
    admin.control(agent.agent_id, "pause")
    res = agent.call("get_customer", CUSTOMER, "r1")
    assert res.status_code == 403 and res.body["reason"] == "agent_paused"
    admin.control(agent.agent_id, "resume")
    assert agent.call("get_customer", CUSTOMER, "r1").ok


def test_quarantine_allows_llm_blocks_tools(admin, make_agent):
    agent = make_agent("executor")
    admin.control(agent.agent_id, "quarantine")
    llm = agent.call("llm", {"task": "x", "messages": [{"role": "user", "content": "hi"}]}, "r1")
    assert llm.ok
    res = agent.call("issue_refund", {"customer_id": "C001", "amount": 10.0}, "r1")
    assert res.status_code == 403 and res.body["reason"] == "agent_quarantined"


def test_halt_revokes_token(admin, make_agent, gw_http):
    agent = make_agent("retriever")
    assert agent.call("get_customer", CUSTOMER, "r1").ok
    admin.control(agent.agent_id, "halt")
    res = agent.call("get_customer", CUSTOMER, "r1")
    assert res.status_code == 401 and res.body["reason"] == "token_revoked"
    agent.token = None
    agent.ensure_token()
    assert agent.token is None  # a halted agent cannot obtain a new token


def test_halt_all_blocks_everyone(admin, make_agent):
    retriever, auditor = make_agent("retriever"), make_agent("auditor")
    admin.halt_all()
    for agent in (retriever, auditor):
        res = agent.call("get_customer", CUSTOMER, "r1")
        assert res.status_code == 403 and res.body["reason"] == "global_halt"
    admin.resume_all()
    assert retriever.call("get_customer", CUSTOMER, "r1").ok
