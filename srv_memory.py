"""Routes over what Otto keeps: chats, workstreams, memory, solutions, behaviors, knowledge.

A mixin of `server.Handler` (split out of server.py by route group). Module-level names
that live in server.py are read through `_srv()` at call time, never bound at import:
tests replace `server._wf_start`, `server.CAPS` and others, and a bound copy would not
see it. Guard tests read this file through `test_support.server_src()`.
"""
import json

import attachments
import chats
import datahealth
import engine
import knowledge
import temporal_client as tc
import workstreams

from facade import srv as _srv


class MemoryRoutes:
    def _post_chats_save(self, body):
        """POST /api/chats/save"""
        self._send(200, json.dumps({"id": chats.save(body)}))

    def _post_chats_pin(self, body):
        """POST /api/chats/pin"""
        self._send(200, json.dumps({"pinned": chats.set_pinned(body.get("id"), body.get("pinned"))}))

    def _post_chats_reorder(self, body):
        """POST /api/chats/reorder — the sidebar's drag order; display only."""
        ids = body.get("ids")
        if not isinstance(ids, list):
            return self._send(400, json.dumps({"error": "ids must be a list"}))
        self._send(200, json.dumps({"order": chats.set_order(ids)}))

    def _post_chats_clear(self, body):
        """POST /api/chats/clear — deletes every chat except pinned and running ones."""
        self._send(200, json.dumps({"deleted": chats.delete_unpinned()}))

    def _post_chats_delete(self, body):
        """POST /api/chats/delete"""
        cid = body.get("id")
        # If this chat has an in-flight workflow (paused awaiting approval/clarification,
        # or still executing), deleting the chat abandons the task — so hard-stop the
        # workflow too, else its board card lingers as a zombie awaiting a human who's
        # gone. Mirrors the board Terminate button: TERMINATE + dismiss the card.
        # Best-effort; a finished run has cleared its run_id (clearRun) so nothing to kill,
        # and terminating an already-closed workflow just raises and is swallowed.
        wid = (chats.get(cid) or {}).get("run_id")
        if wid and _srv().TEMPORAL_OK:
            try:
                tc.run(_srv()._wf_terminate(wid))
                _srv()._dismiss(wid)
            except Exception:  # noqa: BLE001 - already-closed workflow / unknown id
                pass
        chats.delete(cid)
        self._send(200, json.dumps({"ok": True}))

    def _post_data_health_fix(self, body):
        """POST /api/data-health/fix — {key} and nothing else: `datahealth.fix` re-runs the checks
        and applies the action IT picked for that finding, so the client names no action or target."""
        if not datahealth.fix(body.get("key")):
            self._send(409, json.dumps({"error": "that finding is no longer present"}))
            return
        self._send(200, json.dumps({"ok": True, "data_health": datahealth.report()}))

    def _post_workstreams_assign(self, body):
        """POST /api/workstreams/assign — {chat_id, workstream_id | title | neither (take out)}"""
        got = workstreams.assign(body.get("chat_id"), body.get("workstream_id") or None,
                                 (body.get("title") or "").strip() or None)
        if got is None:
            self._send(404, json.dumps({"error": "no such chat or workstream"}))
            return
        self._send(200, json.dumps({"workstream_id": got or None}))

    def _post_workstreams_create(self, body):
        """POST /api/workstreams/create — {title, chat_ids}: a workstream is never empty."""
        wsid = workstreams.create(body.get("title"), body.get("chat_ids") or [])
        if not wsid:
            self._send(400, json.dumps({"error": "a name and at least one existing chat are required"}))
            return
        self._send(200, json.dumps({"id": wsid}))

    def _post_workstreams_rename(self, body):
        """POST /api/workstreams/rename"""
        if not workstreams.rename(body.get("id"), body.get("title")):
            self._send(400, json.dumps({"error": "a title is required, for a workstream that exists"}))
            return
        self._send(200, json.dumps({"ok": True}))

    def _post_workstreams_delete(self, body):
        """POST /api/workstreams/delete — ungroups; the chats are kept."""
        self._send(200, json.dumps({"ok": workstreams.delete(body.get("id"))}))

    def _post_memory_delete(self, body):
        """POST /api/memory/delete"""
        ok = engine.delete_fact(body.get("id"), body.get("fact", ""))
        self._send(200 if ok else 404, json.dumps({"ok": ok}))

    def _post_memory_clear(self, body):
        """POST /api/memory/clear"""
        engine.clear_memory()
        self._send(200, json.dumps({"ok": True}))

    def _post_solutions_delete(self, body):
        """POST /api/solutions/delete"""
        engine.delete_solution(body.get("id"))
        self._send(200, json.dumps({"ok": True}))

    def _post_solutions_clear(self, body):
        """POST /api/solutions/clear"""
        engine.clear_solutions()
        self._send(200, json.dumps({"ok": True}))

    def _post_behaviors_add(self, body):
        """POST /api/behaviors/add"""
        rule = engine.add_behavior(body.get("rule", ""), body.get("scope", "global"))
        if not rule:
            self._send(400, json.dumps({"error": "rule text is required"})); return
        self._send(200, json.dumps({"ok": True, "behavior": rule}))

    def _post_behaviors_update(self, body):
        """POST /api/behaviors/update"""
        engine.update_behavior(body.get("id"), body.get("rule", ""))
        self._send(200, json.dumps({"ok": True}))

    def _post_behaviors_delete(self, body):
        """POST /api/behaviors/delete"""
        engine.delete_behavior(body.get("id"))
        self._send(200, json.dumps({"ok": True}))

    def _post_behaviors_suggest(self, body):
        """POST /api/behaviors/suggest"""
        self._send(200, json.dumps(
            engine.suggest_behavior_rule(body.get("message", ""), body.get("cap"))))

    def _post_memory_gc_run(self, body):
        """POST /api/memory/gc/run"""
        self._send(200, json.dumps(engine.gc_start()))

    def _post_memory_gc_evict(self, body):
        """POST /api/memory/gc/evict"""
        n = engine.gc_evict(body.get("candidates") or [])
        self._send(200, json.dumps({"ok": True, "evicted": n}))

    def _post_knowledge_add(self, body):
        """POST /api/knowledge/add — pasted `text`, or an `attachment` id from /api/attachments
        whose file is extracted server-side and then discarded."""
        aid = body.get("attachment")
        if aid:
            meta = attachments.get(aid)
            if not meta:
                self._send(400, json.dumps({"error": "upload expired or unknown — attach it again"})); return
            try:
                with open(meta["path"], "rb") as f:
                    doc = knowledge.add_file(body.get("title", ""), meta["name"], f.read())
            except ValueError as e:
                self._send(400, json.dumps({"error": f"{meta['name']}: {e}"})); return
            finally:
                attachments.discard(aid)
            if not doc:
                self._send(400, json.dumps({"error": f"{meta['name']}: no text to add"})); return
            self._send(200, json.dumps({"ok": True, "doc": doc})); return
        doc = knowledge.add_document(body.get("title", ""), body.get("text", ""),
                                     body.get("source", "paste"))
        if not doc:
            self._send(400, json.dumps({"error": "title and non-empty text are required"})); return
        self._send(200, json.dumps({"ok": True, "doc": doc}))

    def _post_knowledge_delete(self, body):
        """POST /api/knowledge/delete"""
        knowledge.delete_document(body.get("id"))
        self._send(200, json.dumps({"ok": True}))

    def _post_knowledge_clear(self, body):
        """POST /api/knowledge/clear"""
        knowledge.clear()
        self._send(200, json.dumps({"ok": True}))

    def _post_knowledge_settings(self, body):
        """POST /api/knowledge/settings"""
        self._send(200, json.dumps({"ok": True, "settings": knowledge.set_settings(
            threshold=body.get("threshold"), embed_model=body.get("embed_model"))}))

    def _post_knowledge_reembed(self, body):
        """POST /api/knowledge/reembed"""
        self._send(200, json.dumps({"ok": True, "result": knowledge.reembed_all()}))

    def _post_knowledge_preview(self, body):
        """POST /api/knowledge/preview"""
        self._send(200, json.dumps({"hits": knowledge.recall_knowledge(body.get("query", ""))}))
