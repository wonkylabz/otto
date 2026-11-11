"""Routes that start, continue, signal and recover a run.

A mixin of `server.Handler` (split out of server.py by route group). Module-level names
that live in server.py are read through `_srv()` at call time, never bound at import:
tests replace `server._wf_start`, `server.CAPS` and others, and a bound copy would not
see it. Guard tests read this file through `test_support.server_src()`.
"""
import hashlib
import json
import uuid

import attachments
import board
import chats
import config
import delivery
import engine
import factory
import gateway
import local_runtime
import temporal_client as tc
import workspace

from facade import srv as _srv


class RunRoutes:
    def _post_submit(self, body):
        """POST /api/submit"""
        if not _srv().TEMPORAL_OK:
            self._send(503, json.dumps({"error": "Temporal not available — run the server with ./.venv/bin/python server.py and start worker.py"})); return
        req = (body.get("request") or "").strip()
        name = (body.get("cap") or "").strip()
        # Explicit slash-command pick — resolve from the trusted registry (risk never
        # comes from the client) and pin it, skipping Router #1.
        cap = next((c for c in _srv().CAPS if c.name == name), None) if name else None
        if name and cap is None:
            self._send(400, json.dumps({"error": f"unknown capability '{name}'"})); return
        if not req:
            # A bare pinned capability (e.g. a self-describing skill like
            # /slack-qna-harvest) is a valid run with no free-text args — synthesize a
            # request so verify/audit/memory have coherent text. An UNPINNED empty request
            # has nothing to route, so it stays an error.
            if cap is None:
                self._send(400, json.dumps({"error": "missing 'request'"})); return
            req = f"Run the {cap.name} {cap.kind}."
        # The operator at their own keyboard — the fast lane's trusted asker (issue #193). Only
        # with a browser Origin (which `_csrf_ok` has already matched): an Origin-less POST is a
        # script — curl, a test, a run's own Bash — and must not skip the gate on our credit.
        params = {"request": req, "trusted_asker": bool(self.headers.get("Origin"))}
        if cap:
            params["cap"] = cap.ref()
        atts, err = self._attachments_param(body)
        if err:
            self._send(400, json.dumps({"error": err})); return
        if atts:
            params["attachments"] = atts
        # Per-chat memory opt-out (default on) and model override (default: admin config).
        # Explicit-key check, not truthiness — `body.get("memory_enabled")` would silently
        # collapse an intentional `False` back to the True default.
        if "memory_enabled" in body:
            params["memory_enabled"] = bool(body["memory_enabled"])
        # Composer's "Auto approve" toggle — the human at the keyboard pre-authorizes
        # this chat's writes, so the gate and its plan preview are bypassed. The client
        # sends a BOOLEAN and the server maps it; `approval` itself is never taken from a
        # body, so a browser still cannot reach the unattended "ask"/"skip" modes. What
        # keeps a page the operator merely VISITS from setting it on their behalf is
        # `_csrf_ok` above — this is the toggle that makes that check load-bearing.
        if body.get("auto_approve"):
            params["approval"] = "auto"
        model_override = (body.get("model_override") or "").strip()
        if model_override:
            if not gateway.resolve_model(model_override):
                self._send(400, json.dumps({"error": f"unknown model '{model_override}'"})); return
            params["model_override"] = model_override
        # How hard the model thinks (config.EFFORT_LEVELS). Validated here rather than passed
        # through, for the same reason model_override is: the CLI only WARNS on an unknown
        # --effort value, so an unchecked string yields a run at the default effort that every
        # layer above reports as having honoured the pick.
        effort = config.effort_level(body.get("effort"))
        if effort:
            params["effort"] = effort
        elif (body.get("effort") or "").strip().lower() not in ("", "default"):
            self._send(400, json.dumps({"error": f"unknown effort '{body.get('effort')}'"})); return
        repo = (body.get("repo") or "").strip()
        if repo:
            # Validate against the allowlist HERE (trusted), so the workflow can trust it.
            if not workspace.resolve(repo):
                self._send(400, json.dumps({"error": f"unknown or non-git repo '{repo}'"})); return
            params["repo"] = repo
            # Opt-in post-PR QA loop only makes sense in repo-mode (it validates the PR).
            # Setting it pre-authorizes the QA cap to run through the loop.
            if body.get("qa"):
                params["qa"] = True
            # Opt-in post-PR code-review loop (default-on for the worker cap; this flag
            # enables it for other caps too). Repo-mode only — it reviews the opened PR.
            if body.get("review"):
                params["review"] = True
        # Opt-in plan-then-execute: a strong model decomposes the task into atomic steps a
        # local executor runs one at a time. Only takes effect when OTTO_PLAN_MODE != off
        # (the workflow gates on it) and, in v1, for non-repo runs (repo-mode plans are a
        # follow-up — the workflow ignores it under repo-mode).
        if body.get("plan_mode"):
            params["plan_mode"] = True
        _srv()._front_web_chat(params, resubmit=bool(body.get("frontman")))
        wid = "web-" + uuid.uuid4().hex
        tc.run(_srv()._wf_start(wid, params))
        self._send(200, json.dumps({"id": wid}))

    def _post_continue(self, body):
        """POST /api/continue"""
        name = (body.get("cap") or {}).get("name")
        cap = next((c for c in _srv().CAPS if c.name == name), None)
        if cap is None:
            self._send(400, json.dumps({"error": "unknown capability"})); return
        # Follow-up handoff: a follow-up that DELEGATES a new task (e.g. accepting
        # a ticket the cap OFFERED) must start a FRESH routed run — resume keeps
        # the session's cap for life and never engages repo-mode/verify/review (the
        # PM-implements-the-code failure, PR #194). Return the extracted standalone
        # task; the client re-submits it through the normal fresh path. Needs the
        # previous reply (`prev`) for reference resolution — a client that doesn't
        # send it keeps plain resume semantics.
        prev = (body.get("prev") or "").strip()
        # Brainstorm never hands off: a blocking classifier call ahead of every turn of a
        # conversation is the latency the mode exists to avoid, and its replies offer
        # directions, which is exactly what reads as "delegate this" to the classifier.
        # A fronted chat never hands off either: the frontman delegates by declaring it.
        # Keyed on the SESSION alone: its system prompt carries the DELEGATE rule either way, so
        # switching the setting off mid-chat would leave a hand-off line shown verbatim.
        fronted = (cap.name == config.WEB_FRONTMAN_CAP
                   and bool((body.get("cap") or {}).get("fronted")))
        if (prev and not body.get("no_handoff") and cap.name != config.BRAINSTORM_CAP
                and not fronted):
            task = engine.followup_handoff(body["message"], prev, cap)
            if task:
                self._send(200, json.dumps({"handoff": {"request": task}}))
                return
        trusted_cap = cap.ref()
        params = {"request": body["message"], "resume": body["session_id"],
                  "cap": trusted_cap}
        # Same composer toggle as /api/submit: a follow-up can be re-classified from
        # read to write (classify_followup) and hit the gate, so pre-authorization has
        # to travel with the follow-up too or the toggle reads as broken on turn 2.
        if body.get("auto_approve"):
            params["approval"] = "auto"
        # The composer's model picker applies to a follow-up too — same validation as
        # /api/submit.
        model_override = (body.get("model_override") or "").strip()
        if model_override:
            entry = gateway.resolve_model(model_override)
            if not entry:
                self._send(400, json.dumps({"error": f"unknown model '{model_override}'"})); return
            params["model_override"] = model_override
            # A session is BOUND to the backend that minted its id — `claude -p --resume`
            # cannot read a local session's history, nor the local runtime a Claude one —
            # so an override on the other backend is unhonourable in-session. The explicit
            # pick wins over the binding: hand the follow-up back for a fresh run on the
            # chosen model rather than silently resuming on the OLD one (user-reported:
            # "it carries on using that model rather than using the override"). The client
            # re-submits with the conversation carried as context, and says so on screen.
            if gateway.backend_of(entry) != local_runtime.session_backend(
                    body["session_id"]):
                self._send(200, json.dumps({"rebind": {"request": body["message"],
                                                       "model": model_override}}))
                return
        # Effort applies to a follow-up too — same validation as /api/submit. A resume is one
        # more turn of the same conversation, so the composer's current pick governs THIS turn
        # rather than whatever the first turn ran at.
        effort = config.effort_level(body.get("effort"))
        if effort:
            params["effort"] = effort
        elif (body.get("effort") or "").strip().lower() not in ("", "default"):
            self._send(400, json.dumps({"error": f"unknown effort '{body.get('effort')}'"})); return
        if fronted:
            params["frontman"], params["cap"] = True, {**trusted_cap, "fronted": True}
            if self.headers.get("Origin"):
                params["trusted_asker"], params["asker_text"] = True, body["message"]
        atts, err = self._attachments_param(body)
        if err:
            self._send(400, json.dumps({"error": err})); return
        if atts:
            params["attachments"] = atts
        # Readable, not re-announced: the session already knows these paths from earlier turns.
        new = {a["id"] for a in atts}
        prior, _ = attachments.resolve([i for i in chats.session_attachment_ids(body["session_id"])
                                        if i not in new])
        if prior:
            params["prior_attachments"] = prior
        # A repo-mode session's isolated clone was torn down after the original run —
        # re-provisioning it needs the repo + the ORIGINAL run's id (its git identity)
        # so the workflow can rebuild the exact same workspace/branch. Re-validate the
        # repo against the allowlist here (trusted), same as /api/submit.
        repo = (body.get("repo") or "").strip()
        git_run_id = body.get("git_run_id")
        # Fall back to the CHAT's stored identity when the client didn't send one. The
        # browser's copy lives in memory from when the chat was opened, so a tab held
        # open across a change to that row keeps sending `undefined` — and an omitted
        # repo doesn't degrade, it makes the follow-up un-resumable (no workspace, so
        # the resume runs from the wrong cwd and returns `(no output)`). The store is
        # Otto's own state and the better authority; it's still allowlist-checked below.
        if not repo:
            ident = chats.git_identity(body["session_id"])
            repo, git_run_id = ident.get("repo", ""), ident.get("git_run_id")
        if repo and workspace.resolve(repo):
            params["repo"] = repo
            params["git_run_id"] = git_run_id
            # Validate here too (trusted boundary) — this reaches a git argv inside
            # the workflow, and the client body isn't trusted input.
            branch = (body.get("git_branch") or "").strip()
            params["git_branch"] = branch if workspace.valid_branch(branch) else None
        wid = "web-" + uuid.uuid4().hex
        tc.run(_srv()._wf_start(wid, params))
        self._send(200, json.dumps({"id": wid}))

    def _post_wf_signal(self, body):
        """POST /api/wf/signal. An allowlist (#3): `_wf_signal` maps anything it doesn't know to
        a clarification, so an unchecked name silently became an answer to a question."""
        wid, sig = (body.get("id") or "").strip(), body.get("signal")
        if not wid or sig not in _srv()._SIGNALS:
            self._send(400, json.dumps({"error": f"need 'id' and a 'signal' in {sorted(_srv()._SIGNALS)}"}))
            return
        try:
            tc.run(_srv()._wf_signal(wid, sig, body.get("value"), body.get("revision")))
        except _srv().GateMoved as e:
            self._send(409, json.dumps({"error": str(e)[:200], "gate_moved": True})); return
        except TimeoutError:
            self._send(503, json.dumps({"error": "no worker answered — the decision may still "
                                                 "land; check the run before retrying"})); return
        self._send(200, json.dumps({"ok": True}))

    def _post_wf_terminate(self, body):
        """POST /api/wf/terminate"""
        if not _srv().TEMPORAL_OK:
            self._send(503, json.dumps({"error": "Temporal not available"})); return
        wid = (body.get("id") or "").strip()
        if not wid:
            self._send(400, json.dumps({"error": "missing 'id'"})); return
        try:
            tc.run(_srv()._wf_terminate(wid))
        except Exception as e:  # noqa: BLE001 - already-closed workflow, unknown id
            # 409, not 200 — the run was not terminated, and the card must not disappear as if
            # it had been (`board.boardTerminate` removes it on a resolved promise).
            self._send(409, json.dumps({"error": str(e)[:200]})); return
        _srv()._dismiss(wid)
        self._send(200, json.dumps({"ok": True}))

    def _post_needs_you_retry(self, body):
        """POST /api/needs-you/retry"""
        if not _srv().TEMPORAL_OK:
            self._send(503, json.dumps({"error": "Temporal not available"})); return
        wid = (body.get("id") or "").strip()
        if factory.is_sprint(wid):
            self._send(409, json.dumps({"error": "a factory sprint is never retried by hand — "
                                                 "the factory runs its own next sprint; dismiss "
                                                 "this card"}))
            return
        request, capname, repo, reached_run = _srv()._run_origin(wid)
        if not request:
            self._send(404, json.dumps({"error": "no audit history found for that run"})); return
        params = {"request": request}
        if capname and ":" in capname:
            cap = next((c for c in _srv().CAPS if c.name == capname.split(":", 1)[1]), None)
            if cap:
                params["cap"] = cap.ref()
        if repo and workspace.resolve(repo):
            params["repo"] = repo
        # Carry the ORIGINAL run's reply target across, so a retry returns to the thread
        # that asked (a Slack DM, a GitHub issue, a webhook) instead of only landing on the
        # board. The audit trail never stored `reply_to`, so recover it from the dead run's
        # own Temporal history; if that's aged out, rebuild a Slack target from the run id.
        origin = tc.workflow_input(wid) or {}
        reply_to = origin.get("reply_to") or _srv()._slack_reply_target(wid)
        if reply_to:
            params["reply_to"] = reply_to
        # Re-resolved from ids, never the recorded paths: an expired upload is dropped, and the
        # retry says nothing of it rather than pointing the run at a file that is gone.
        atts, _ = attachments.resolve([a.get("id") for a in origin.get("attachments") or []])
        if atts:
            params["attachments"] = atts
        # An unattended run stays unattended on retry — nobody is watching to approve, so
        # it must keep its original approval mode instead of blocking on a screen. Recovered
        # INDEPENDENTLY of reply_to: a SCHEDULE-origin run delivers to the chat sidebar and
        # has no reply target, so folding this into the `if reply_to:` block above silently
        # downgraded every scheduled retry to interactive — its `auto_approve` was dropped
        # and a write cap hit the gate the schedule exists to skip.
        unattended = bool(reply_to or origin.get("unattended") or origin.get("scheduled"))
        if unattended:
            params["unattended"] = True
            # `auto_approve=True` is the scheduler's spelling of approval "auto"; mirror
            # the workflow's own mapping so a schedule retry stays pre-authorized.
            params["approval"] = (origin.get("approval")
                                  or ("auto" if origin.get("auto_approve") else "skip"))
        # Overrides whatever the block above decided (including a plain interactive
        # origin, where `unattended` above is False and nothing sets `approval` at all):
        # a run that already reached RUN passed its gate once already (or never needed
        # one), and re-authorizing that same write is exactly what clicking retry means.
        # `unattended=True` here also makes the workflow skip CLARIFY (gated on `not
        # unattended`) — pinning `cap` already skips DECOMPOSE/ROUTER — so this lands
        # straight back in the run/verify ladder instead of redoing everything upstream
        # of the failure.
        if reached_run:
            params["unattended"] = True
            params["approval"] = "auto"
        # A fast-lane run (never `reached_run`, see run_origin) is retried from the top, and may
        # take the lane again only if its own asker could: the flag is the ORIGIN's, never new.
        if origin.get("trusted_asker"):
            params["trusted_asker"] = True
        # The quoted post stays DATA on retry (#223), or the connector guard reads it as the ask.
        if origin.get("external_text"):
            params["external_text"] = origin["external_text"]
        if origin.get("untrusted_author"):     # a person-fired trigger stays write-intent checked
            params["untrusted_author"] = True
        new_id = "web-" + uuid.uuid4().hex
        if (reply_to or {}).get("kind") == "github_issue":
            # A board card's retry is the card's NEXT GENERATION, not a side run: as a `web-*`
            # run it raced the factory's own re-ready of the same card (zero-marketing#4: two
            # runs, two duplicate PRs), and its PR never superseded the earlier one's.
            n, slug = reply_to.get("number"), reply_to.get("repo")
            new_id = board.next_run_id(n, slug)
            if new_id is None:
                self._send(409, json.dumps({
                    "error": f"issue #{n} is already running again — wait for that run"})); return
            for k in ("qa", "fresh_branch"):
                if origin.get(k):
                    params[k] = origin[k]
            fac = factory.load()
            if factory._owned(wid, fac.get("wids") or []):
                # Else it escapes the factory's budget and MCP allowlist. A hold-label ticket stays
                # a human's across the retry, or its next failure is hidden for a PM that never
                # touches held cards.
                factory.claim(new_id, human=factory._owned(wid, fac.get("human_wids") or [])
                              or params.get("approval") == "ask")
        # Record the retry into a Chat thread so its result lands in a conversation, not
        # just on the board: an interactive run records CLIENT-side, so retrying it from
        # the board previously left the result board-only. Three tiers, most precise
        # first: (1) the ORIGINAL run's own chat_key straight from Temporal (exact, no
        # ambiguity); (2) the sticky origin_run_id backstop — catches an interactive
        # web-* run that never had a chat_key at all but did get a browser-tracked
        # run_id; (3) the text-similarity `find_reattach` guess, last resort. Getting all
        # three wrong forks a brand-new chat that holds the real result while the old,
        # familiar thread the Chats tab shows stays frozen (user-reported). chat_key also
        # gives the new card an "Open conversation" link + makes delete-in-flight able to
        # terminate it.
        try:
            reattach = tc.run(_srv()._wf_origin_chat_key(wid))
        except Exception:  # noqa: BLE001 - background loop/client unreachable
            reattach = None
        reattach = reattach or chats.find_by_run_origin(wid) or chats.find_reattach(request)
        params["chat_key"] = reattach or new_id
        if not reattach:
            params["chat_labels"] = ["retry"]
        tc.run(_srv()._wf_start(new_id, params))
        _srv()._record_retry(wid, new_id)
        # Retrying IS acknowledging the card: dismiss it so it leaves "Needs a look"
        # immediately — the retry shows up under Running as its own card. Leaving the
        # old card in place read as "did my click even work?" (user-reported).
        _srv()._dismiss(wid)
        self._send(200, json.dumps({"ok": True, "id": new_id}))

    def _post_needs_you_accept(self, body):
        """POST /api/needs-you/accept"""
        wid = (body.get("id") or "").strip()
        if not wid:
            self._send(400, json.dumps({"error": "missing 'id'"})); return
        request, capname, repo, _reached_run = _srv()._run_origin(wid)
        if not request:
            self._send(404, json.dumps({"error": "no audit history found for that run"})); return
        engine.accept_run(wid, request, capname, _srv()._run_result(wid), repo=repo)
        _srv()._dismiss(wid)
        self._send(200, json.dumps({"ok": True}))

    def _post_needs_you_dismiss(self, body):
        """POST /api/needs-you/dismiss"""
        wid = (body.get("id") or "").strip()
        if not wid:
            self._send(400, json.dumps({"error": "missing 'id'"})); return
        _srv()._dismiss(wid)
        self._send(200, json.dumps({"ok": True}))

    def _post_prefix_gate(self, body):
        """POST /api/gate/<token> — approve or deny a parked run from a notification action
        button. `{"approve": bool}`.

        The token IS the authorization: everything else needs the install's API token (cookie or
        header, api_auth), which a phone on the far side of a tunnel does not hold. So the grant
        is deliberately the narrowest thing that still works — one run, one use, expiring with the gate (delivery.mint_action_token) —
        and the run id is never in the URL, so a leaked topic cannot be replayed against any other
        run or any other endpoint. Unknown/expired/spent token: 403 and nothing happens."""
        token = self.path.rsplit("/", 1)[-1]
        # Peeked, then spent only on a FINAL outcome: a tap during a worker restart times out,
        # and burning the token there left the phone with no button for a gate still standing.
        wid, revision = delivery.peek_action_token(token)
        if not wid:
            self._send(403, json.dumps({"error": "expired or already used"})); return
        approve = bool(body.get("approve"))
        try:
            # Keyed on the token (hashed — the token is a live credential until spent) AND the
            # choice: both buttons share one token, and Temporal dedupes by id alone, so a Deny
            # after a timed-out Approve would otherwise be handed the Approve's outcome.
            got = tc.run(_srv()._wf_decide(wid, approve, revision, update_id="gate-" + hashlib.sha256(
                token.encode()).hexdigest()[:24] + ("-a" if approve else "-d")))
        except TimeoutError:
            self._send(503, json.dumps({"error": "no worker answered — try again shortly"})); return
        except Exception as e:  # noqa: BLE001 - refused (GateMoved) or the run is gone: final
            delivery.redeem_action_token(token)
            self._send(409, json.dumps({"error": str(e)[:200]})); return
        applied = (got or {}).get("decision")
        if applied and applied != ("approved" if approve else "denied"):
            # Report what the WORKFLOW decided, never what was tapped.
            delivery.redeem_action_token(token)
            self._send(409, json.dumps({"error": f"the gate was already {applied}"})); return
        if not delivery.redeem_action_token(token):
            # A second tap raced this one; the workflow already admitted exactly one of them.
            engine.trace("GATE", f"action token for {wid} was spent concurrently")
        self._send(200, json.dumps({"ok": True, "id": wid,
                                    "decision": "approved" if approve else "denied"}))

    def _resolve_pin(self, name):
        """Resolve an optional pinned-capability name against the TRUSTED registry (never trust
        risk from the client, same as /api/submit). Returns (cap_dict|None, error):
        empty name -> (None, None) = auto-route; unknown name -> (None, message)."""
        name = (name or "").strip()
        if not name:
            return None, None
        c = next((c for c in _srv().CAPS if c.name == name), None)
        if c is None:
            return None, f"unknown capability '{name}'"
        return c.ref(), None

    def _check_caps(self, body):
        """Every capability a runbook names — its own and each step's — must exist NOW. Caught at
        save time so the author fixes a typo in the form, rather than at fire time on a schedule
        nobody is watching (engine._plan_step_caps is the second, unavoidable check for a cap
        deleted between saving and running)."""
        names = [body.get("cap")] + [s.get("cap") for s in (body.get("steps") or [])
                                     if isinstance(s, dict)]
        for n in names:
            _, err = self._resolve_pin(n)
            if err:
                return err
        return None
