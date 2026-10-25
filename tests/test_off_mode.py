from common.crypto import generate_keypair
from scenarios.attacks import RogueClient


def test_off_mode_skips_identity(admin, make_agent, gw_http):
    agent = make_agent("retriever")
    admin.set_config({"mode": "off"})
    rogue, _ = generate_keypair()
    code = RogueClient("http://gateway", gw_http).send(agent.agent_id, "r1", "get_customer",
                                                       {"customer_id": "C001"}, rogue, token=None)
    assert code == 200


def test_off_mode_allows_role_violation(admin, make_agent):
    agent = make_agent("retriever")
    admin.set_config({"mode": "off"})
    res = agent.call("issue_refund", {"customer_id": "C001", "amount": 300.0}, "r1")
    assert res.ok
    assert len(admin.tool_state()["payments"]) == 1
