import time

from common.crypto import generate_keypair
from scenarios.attacks import RogueClient, replay

CUSTOMER = {"customer_id": "C001"}


def last_event(admin, type_: str) -> dict:
    return [e for e in admin.events() if e["type"] == type_][-1]


def test_valid_call_passes(make_agent):
    res = make_agent("retriever").call("get_customer", CUSTOMER, "r1")
    assert res.ok and res.body["result"]["customer_id"] == "C001"


def test_missing_token(admin, make_agent, gw_http):
    agent = make_agent("retriever")
    code = RogueClient("http://gateway", gw_http).send(agent.agent_id, "r1", "get_customer", CUSTOMER,
                                                       agent.private_key, token=None)
    assert code == 401
    assert last_event(admin, "identity_failed")["detail"]["reason"] == "missing_token"


def test_bad_signature(admin, make_agent, gw_http):
    agent = make_agent("retriever")
    agent.ensure_token()
    rogue, _ = generate_keypair()
    code = RogueClient("http://gateway", gw_http).send(agent.agent_id, "r1", "get_customer", CUSTOMER,
                                                       rogue, agent.token)
    assert code == 401
    assert last_event(admin, "identity_failed")["detail"]["reason"] == "bad_signature"


def test_stolen_token_wrong_key(admin, make_agent, gw_http):
    victim, other = make_agent("executor"), make_agent("retriever")
    rogue_client = RogueClient("http://gateway", gw_http)
    assert rogue_client.impersonate("r1", victim) == [401, 401, 401]
    victim.ensure_token()
    rogue, _ = generate_keypair()
    # the retriever's token presented for the executor's identity is a role mismatch
    other.ensure_token()
    assert rogue_client.send(victim.agent_id, "r1", "get_customer", CUSTOMER, rogue, other.token) == 401
    assert last_event(admin, "identity_failed")["detail"]["reason"] == "role_mismatch"
    assert admin.tool_state()["payments"] == []


def test_unknown_agent(admin, make_agent):
    agent = make_agent("retriever")
    agent.ensure_token()
    admin.reset(mode="on", scenario={"name": "t", "chaos": [], "seed": 1}, approval_mode="auto_reject",
                monitor_enabled=True)
    res = agent.call("get_customer", CUSTOMER, "r1")
    assert res.status_code == 401 and res.body["reason"] == "unknown_agent"


def test_replay_blocked(admin, make_agent):
    agent = make_agent("retriever")
    assert agent.call("get_customer", CUSTOMER, "r1").ok
    assert replay(agent, 2) == [401, 401]
    assert last_event(admin, "replay_blocked")["detail"]["reason"] == "replay_detected"


def test_clock_skew(admin, make_agent, gw_http):
    agent = make_agent("retriever")
    agent.ensure_token()
    code = RogueClient("http://gateway", gw_http).send(agent.agent_id, "r1", "get_customer", CUSTOMER,
                                                       agent.private_key, agent.token,
                                                       timestamp=time.time() - 100)
    assert code == 401
    assert last_event(admin, "identity_failed")["detail"]["reason"] == "clock_skew"


def test_revoked_token(gateway_app, make_agent):
    agent = make_agent("retriever")
    assert agent.call("get_customer", CUSTOMER, "r1").ok
    gateway_app.state.identity.revoke_agent(agent.agent_id)
    res = agent.call("get_customer", CUSTOMER, "r1")
    assert res.status_code == 401 and res.body["reason"] == "token_revoked"


def test_events_without_since_ts_returns_most_recent(admin, make_agent):
    agent = make_agent("retriever")
    for _ in range(3):
        agent.call("get_customer", CUSTOMER, "r1")
    everything = admin.events(limit=500)
    latest = admin.events(limit=4)
    assert latest == everything[-4:]
    assert [e["ts"] for e in latest] == sorted(e["ts"] for e in latest)
    # with since_ts the oldest events after that point come first
    after = admin.events(since_ts=everything[0]["ts"], limit=2)
    assert after == everything[1:3]
