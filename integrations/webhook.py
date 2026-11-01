"""Webhook: an inbound event rule's reply URL (`webhook`). A machine reads it, so the report
shape is the sane default."""
import json
import urllib.request

from integrations import Integration, REPORT


def deliver(reply_to, result, cap=None, run_id=None, timeout=15):
    url = reply_to.get("url")
    if not url:
        return "webhook reply_to missing 'url'"
    body = json.dumps({"result": result, "capability": cap}).encode()
    headers = {"Content-Type": "application/json", **(reply_to.get("headers") or {})}
    req = urllib.request.Request(url, method="POST", data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return f"posted to webhook ({r.status})"



def source_line(reply_to):
    """Never the URL: it routinely carries a token in the path or query."""
    return "source: webhook"


INTEGRATION = Integration(
    name="webhook",
    kinds=("webhook",),
    audiences={"webhook": REPORT},
    deliver=deliver,
    source_line=source_line,
)
