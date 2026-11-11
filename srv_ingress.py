"""Routes configuring the ingresses: webhooks, Slack, the board, factory, PR reviews, runbooks.

A mixin of `server.Handler` (split out of server.py by route group). Module-level names
that live in server.py are read through `_srv()` at call time, never bound at import:
tests replace `server._wf_start`, `server.CAPS` and others, and a bound copy would not
see it. Guard tests read this file through `test_support.server_src()`.
"""
import json

import board
import events
import factory
import pr_review
import runbooks
import scheduler
import slack
import slack_socket
import slack_triggers

from facade import srv as _srv


class IngressRoutes:
    def _post_event_rules(self, body):
        """POST /api/event-rules"""
        self._send(200, json.dumps({"ok": True, "rules": events.save_rules(body.get("rules", []))}))

    def _post_slack_triggers(self, body):
        """POST /api/slack-triggers — the first active trigger has to create the poll schedule."""
        saved = slack_triggers.save_rules(body.get("rules", []))
        status = slack.reconcile_schedule() if _srv().TEMPORAL_OK else "temporal unavailable"
        self._send(200, json.dumps({"ok": True, "rules": saved, "schedule": status}))

    def _post_board_config(self, body):
        """POST /api/board-config"""
        saved = board.save(body.get("config", body))
        # Reconcile the poll schedule so enable/disable + interval changes take effect now.
        status = board.reconcile_schedule() if _srv().TEMPORAL_OK else "temporal unavailable"
        self._send(200, json.dumps({"ok": True, "config": saved, "schedule": status}))

    def _post_factory_enable(self, body):
        """POST /api/factory/enable — turn Factory mode on (or update its knobs). Everything that
        runs unattended under it is derived here from the operator's own act, server-side;
        nothing a later run does reads approval or merge from a client body."""
        if not _srv().TEMPORAL_OK:
            self._send(409, json.dumps({"error": "Factory mode needs Temporal"}))
            return
        try:
            state = factory.enable(body)
        except factory.Refused as e:
            self._send(400, json.dumps({"error": str(e)}))
            return
        state.pop("prev_board", None)
        self._send(200, json.dumps({"ok": True, "state": state}))

    def _post_factory_start(self, body):
        """POST /api/factory/start — release the hold and fire a sprint now."""
        try:
            state = factory.start()
        except factory.Refused as e:
            self._send(400, json.dumps({"error": str(e)}))
            return
        state.pop("prev_board", None)
        self._send(200, json.dumps({"ok": True, "state": state}))

    def _post_factory_hold(self, body):
        """POST /api/factory/hold — stop starting work, keep the factory configured."""
        try:
            state = factory.hold()
        except factory.Refused as e:
            self._send(400, json.dumps({"error": str(e)}))
            return
        state.pop("prev_board", None)
        self._send(200, json.dumps({"ok": True, "state": state}))

    def _post_factory_disable(self, body):
        """POST /api/factory/disable — back to Manual; restores the board config it changed."""
        state = factory.disable("manual", "switched to Manual in Admin")
        state.pop("prev_board", None)
        self._send(200, json.dumps({"ok": True, "state": state}))

    def _post_pr_review_config(self, body):
        """POST /api/pr-review-config"""
        saved = pr_review.save(body.get("config", body))
        status = pr_review.reconcile_schedule() if _srv().TEMPORAL_OK else "temporal unavailable"
        self._send(200, json.dumps({"ok": True, "config": saved, "schedule": status}))

    def _post_pr_review_post(self, body):
        """POST /api/pr-review/post — write Otto's review to the PR, as the operator.

        Thin on purpose: `pr_review.publish` is the ONE path a review reaches GitHub, shared
        with the unattended auto-post sweep, so the approve decision cannot differ between a
        button press and a poll. Nothing about WHAT is posted comes from the request — the
        API is unauthenticated, so a client-supplied body or approve flag would let any page
        the operator visits write to a colleague's PR under their name."""
        key = str(body.get("key") or "")
        ok, detail, approved = pr_review.publish(key)
        code = 200 if ok else (404 if detail == "unknown PR"
                               else 409 if detail.startswith("no review yet") else 502)
        self._send(code, json.dumps({"ok": ok, "detail": detail, "approved": approved}))

    def _post_pr_review_dismiss(self, body):
        """POST /api/pr-review/dismiss — hide a review from the panel without posting it.

        UI-only, and deliberately NOT a re-review trigger: the request is still pending on
        GitHub, so `decide()` keeps this round marked done and only a genuine re-request
        brings it back."""
        key = str(body.get("key") or "")
        if not (pr_review.state().get("prs") or {}).get(key):
            self._send(404, json.dumps({"ok": False, "error": "unknown PR"}))
            return
        pr_review.update_entry(key, {"dismissed": bool(body.get("dismissed", True))})
        self._send(200, json.dumps({"ok": True}))

    def _post_slack_config(self, body):
        """POST /api/slack-config"""
        saved = slack.save(body.get("config", body))
        # Reconcile the Slack poll schedule so the toggle takes effect immediately.
        status = slack.reconcile_schedule() if _srv().TEMPORAL_OK else "temporal unavailable"
        # The socket thread follows the same config, so it reconciles at the same moment the
        # schedule does — otherwise switching the bot off leaves a live socket waking a poll
        # that then does nothing, which reads as "I disabled it and it is still busy".
        slack_socket.reconcile()
        self._send(200, json.dumps({"ok": True, "config": saved, "schedule": status}))

    def _post_prefix_runbooks(self, body):
        """POST /api/runbooks/*"""
        self._runbooks(self.path.rsplit("/", 1)[-1], body)

    def _runbooks(self, action, body):
        """CRUD + run for runbooks. Every validation failure is a 400 carrying runbooks.py's own
        message, which is written to be shown to the author verbatim."""
        # Only the actions that actually touch Temporal need it. DEFINING an on-demand runbook is
        # a plain store write, so it must keep working under `python3 server.py` (no Temporal) —
        # 503-ing the whole tab there would make the feature look broken rather than degraded.
        cron = (body.get("cron") or "").strip()
        needs_temporal = action in ("toggle", "run") or (action in ("add", "edit") and cron)
        if needs_temporal and not _srv().TEMPORAL_OK:
            self._send(503, json.dumps(
                {"error": "a cron schedule needs Temporal — run via ./run.sh"
                          if cron else "running a runbook needs Temporal — run via ./run.sh"})); return
        try:
            if action in ("add", "edit"):
                err = self._check_caps(body)
                if err:
                    self._send(400, json.dumps({"error": err})); return
                if action == "add":
                    self._send(200, json.dumps({"id": scheduler.add(body)}))
                else:
                    scheduler.update(body["id"], body)
                    self._send(200, json.dumps({"ok": True}))
            elif action == "remove":
                scheduler.remove(body.get("id"))
                self._send(200, json.dumps({"ok": True}))
            elif action == "reorder":
                # Display only, and deliberately NOT routed through scheduler: it must not be
                # able to touch a definition or a Temporal schedule, and it has to keep working
                # with Temporal down like every other pure store write here.
                self._send(200, json.dumps({"ok": True,
                                            "order": runbooks.set_order(body.get("ids"))}))
            elif action == "toggle":
                scheduler.set_paused(body.get("id"), not bool(body.get("enabled")))
                self._send(200, json.dumps({"ok": True}))
            elif action == "run":
                wid = scheduler.run_now(body.get("id"), body.get("values"),
                                        unattended=bool(body.get("unattended")))
                self._send(200, json.dumps({"ok": True, "status": "started", "id": wid}))
            else:
                self._send(404, "not found", "text/plain")
        except scheduler.AlreadyRunning as e:
            self._send(409, json.dumps({"error": str(e)}))
        except ValueError as e:            # validation — the message IS the user-facing text
            self._send(400, json.dumps({"error": str(e)}))
        except KeyError:
            self._send(404, json.dumps({"error": "no such runbook"}))
