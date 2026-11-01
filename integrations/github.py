"""GitHub: a board ticket's result comment + card move (`github_issue`) and an auto-review
submitted to a pull request (`github_pr`). Both are durable records a person reads later.
Pointers until phase 2 moves the bodies here."""
from integrations import Integration, REPORT


def _deliver(reply_to, result, cap=None, run_id=None):
    import delivery
    if reply_to.get("kind") == "github_pr":
        return delivery._github_pr(reply_to, result)
    return delivery._github_issue(reply_to, result, cap, run_id)


def _source_line(reply_to):
    import privacy
    return privacy.source_line(reply_to)


INTEGRATION = Integration(
    name="github",
    kinds=("github_issue", "github_pr"),
    audiences={"github_issue": REPORT, "github_pr": REPORT},
    deliver=_deliver,
    source_line=_source_line,
)
