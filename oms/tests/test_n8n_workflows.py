"""Static checks for n8n workflow JSON (not externally verified — instance MCP disabled). Enforces the n8n-skills
rules adopted in the spec: credentials only via the credential store, Respond-to-Webhook on both paths, retries on
network nodes, explicit error workflow, and the AI reaching the business only through OMS /agent tools."""

import json
import re
from pathlib import Path

import pytest

from app.modules import agent

WF = Path(__file__).parents[2] / "n8n" / "workflows"
SECRET = re.compile(r"(Bearer\s+[A-Za-z0-9._-]{8,}|sk-[A-Za-z0-9]{8,}|EAA[A-Za-z0-9]{20,}|password\s*[:=])", re.I)


def load(name):
    return json.loads((WF / name).read_text())


@pytest.mark.parametrize("name", ["ai-sales-admin.json", "error-workflow.json"])
def test_wiring_and_no_inline_secrets(name):
    wf = load(name)
    names = {n["name"] for n in wf["nodes"]}
    for src, outs in wf["connections"].items():
        assert src in names
        for branches in outs.values():
            for branch in branches:
                for target in branch:
                    assert target["node"] in names, (src, target)
    assert not SECRET.search(json.dumps(wf))
    for n in wf["nodes"]:
        if n["type"].endswith("httpRequest") or n["type"].endswith("toolHttpRequest"):
            assert "credentials" in n and n["parameters"].get("authentication") == "genericCredentialType"
        if n["type"] == "n8n-nodes-base.httpRequest":
            assert n.get("retryOnFail") and n.get("maxTries", 0) >= 2


def test_ai_sales_admin_contract():
    wf = load("ai-sales-admin.json")
    assert wf["settings"]["errorWorkflow"] == load("error-workflow.json")["name"]
    nodes = {n["name"]: n for n in wf["nodes"]}
    hook = next(n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.webhook")
    assert hook["parameters"]["responseMode"] == "responseNode"
    # both If branches end in a Respond to Webhook with an explicit status
    valid, invalid = wf["connections"]["If Params Valid"]["main"]
    for branch, code in ((valid, 202), (invalid, 400)):
        node = nodes[branch[0]["node"]]
        assert node["type"] == "n8n-nodes-base.respondToWebhook" and node["parameters"]["options"]["responseCode"] == code
    tools = [n for n in wf["nodes"] if n["type"].endswith("toolHttpRequest")]
    used = {re.search(r"/tools/([a-z_.]+)", t["parameters"]["url"]).group(1) for t in tools}
    assert used == set(agent.SCHEMAS)  # exactly the OMS tool registry; nothing human-only
    assert not used & agent.HUMAN_ONLY
    for t in tools:
        assert "/agent/conversations/" in t["parameters"]["url"]
    assert "UNTRUSTED" in nodes["AI Sales Admin Agent"]["parameters"]["text"]
    assert "NOT externally verified" in wf["meta"]["verification"]
