"""Slack: a thread someone asked in (`slack_thread`, a conversation) and a runbook's one-way
channel post (`slack_post`, a notice). The bodies still live in `delivery`/`privacy`/`activities`
until phase 2 moves them; these are pointers. (`import slack` below is the top-level module.)"""
from integrations import CONVERSATION, Integration, NOTICE


def _deliver(reply_to, result, cap=None, run_id=None):
    import delivery
    return delivery._slack(reply_to, result, run_id)


def _source_line(reply_to):
    import privacy
    return privacy.source_line(reply_to)


def _interim(reply_to, text):
    import delivery
    return delivery.interim(reply_to, text)


INTEGRATION = Integration(
    name="slack",
    kinds=("slack_thread", "slack_post"),
    audiences={"slack_thread": CONVERSATION, "slack_post": NOTICE},
    deliver=_deliver,
    source_line=_source_line,
    interim=_interim,
)
