"""Webhook: an inbound event rule's reply URL (`webhook`). A machine reads it, so the report
shape is the sane default. Pointer until phase 2 moves the body here."""
from integrations import Integration, REPORT


def _deliver(reply_to, result, cap=None, run_id=None):
    import delivery
    return delivery._webhook(reply_to, result, cap)


def _source_line(reply_to):
    import privacy
    return privacy.source_line(reply_to)


INTEGRATION = Integration(
    name="webhook",
    kinds=("webhook",),
    audiences={"webhook": REPORT},
    deliver=_deliver,
    source_line=_source_line,
)
