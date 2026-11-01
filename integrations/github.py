"""GitHub: a board ticket's result comment + card move (`github_issue`) and an auto-review
submitted to a pull request (`github_pr`). Both are durable records a person reads later."""
from integrations import Integration, REPORT

_MAX_COMMENT = 50_000   # GitHub rejects comments over ~65KB; stay well under.


def _deliver_pr(reply_to, result):
    """Submit an auto-review to its pull request the moment the run finishes.

    All of the deciding — verdict, nit trim, header, idempotency marker, state stamp — lives in
    `pr_review.submit`, shared with the button and the poll sweep, so what lands cannot depend
    on which of the three routes got there first."""
    import pr_review
    return pr_review.submit_on_completion(reply_to, result)



def deliver_issue(reply_to, result, cap, run_id=None):
    """Comment the run's result on the source issue, then move its board card on. Destination:
    the **blocked** column when the run needs a human (verification/QA didn't pass — reply_to
    `blocked`), the Review column for a repo-edit ticket with a draft PR awaiting, else Done.
    Card-move ids are carried on `reply_to` (resolved by the poll activity).

    Comment-first (before the move) is deliberate: if the process dies between the two, a
    still-In-Progress card is visible to the reaper, whereas a moved-but-uncommented card would
    silently lose the result. Idempotent via a hidden `run_id` marker so a retried delivery
    (e.g. after an activity timeout) doesn't post a duplicate."""
    import board
    repo, number = reply_to.get("repo"), reply_to.get("number")
    body = result if len(result) <= _MAX_COMMENT else (
        result[:_MAX_COMMENT] + "\n\n_…truncated by Otto (result exceeded comment limit)._")
    marker = f"<!-- otto-run:{run_id} -->" if run_id else ""
    # Comments delivered before the Mosaic->Otto rename carry the old marker; check BOTH or a
    # retried delivery of a pre-rename run would post a duplicate.
    legacy = f"<!-- mosaic-run:{run_id} -->" if run_id else ""
    if marker and (board.has_comment_marker(repo, number, marker)
                   or board.has_comment_marker(repo, number, legacy)):
        posted = True   # already delivered on a prior (retried) attempt — don't duplicate
    else:
        posted = board.comment(repo, number, (marker + "\n" + body) if marker else body)

    if reply_to.get("blocked"):
        target = reply_to.get("blocked_col")
    elif reply_to.get("merged"):
        # Factory mode merged the PR itself: nothing is left to review.
        target = reply_to.get("done_col")
    elif reply_to.get("repo_edit"):
        target = reply_to.get("review_col")
    else:
        target = reply_to.get("done_col")
    option_id = (reply_to.get("status_options") or {}).get(target)
    moved = board.set_status_raw(reply_to.get("project_id"), reply_to.get("status_field_id"),
                                 reply_to.get("item_id"), option_id)
    bits = [f"commented on {repo}#{number}" if posted else f"comment FAILED on {repo}#{number}"]
    if target:
        bits.append(f"moved to {target}" if moved else f"could NOT move to {target}")
    return "; ".join(bits)


def deliver(reply_to, result, cap=None, run_id=None):
    if reply_to.get("kind") == "github_pr":
        return _deliver_pr(reply_to, result)
    return deliver_issue(reply_to, result, cap, run_id)


def source_line(reply_to):
    """A GitHub issue IS named (repo#number) — it's the actionable half of the notification, and
    an internal repo name is a far smaller exposure than a ticket body."""
    repo, number = (reply_to.get("repo") or "?"), reply_to.get("number")
    noun = "PR" if reply_to.get("kind") == "github_pr" else "issue"
    return f"source: GitHub {noun} {repo}#{number}" if number else f"source: GitHub {repo}"


def on_needs_human(reply_to):
    """A board-sourced run that parked: move the stuck card OUT of In Progress to the Blocked
    column and stamp a `needs-human` label (the label survives a board with no Blocked column).

    A `github_pr` is a COLLEAGUE's pull request under review: nothing of Otto's to move, and a
    label written there is a write into their repo (`IntegrationRegistryTests`)."""
    if reply_to.get("kind") != "github_issue":
        return {}
    import board
    repo, number = reply_to.get("repo"), reply_to.get("number")
    blocked_col = reply_to.get("blocked_col")
    option_id = (reply_to.get("status_options") or {}).get(blocked_col)
    moved = board.set_status_raw(reply_to.get("project_id"), reply_to.get("status_field_id"),
                                 reply_to.get("item_id"), option_id)
    labelled = board.add_label(repo, number, "needs-human")
    return {"moved": moved, "labelled": labelled}


INTEGRATION = Integration(
    name="github",
    kinds=("github_issue", "github_pr"),
    audiences={"github_issue": REPORT, "github_pr": REPORT},
    deliver=deliver,
    source_line=source_line,
    on_needs_human=on_needs_human,
)
