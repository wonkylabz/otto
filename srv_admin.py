"""Admin routes: policy, capabilities, MCP servers, project repos, models, settings, onboarding, update, snapshot.

A mixin of `server.Handler` (split out of server.py by route group). Module-level names
that live in server.py are read through `_srv()` at call time, never bound at import:
tests replace `server._wf_start`, `server.CAPS` and others, and a bound copy would not
see it. Guard tests read this file through `test_support.server_src()`.
"""
import json
import os

import config
import conventions
import engine
import gateway
import mcp_client
import onboarding
import policy
import registry
import repos
import snapshot
import updater

from facade import srv as _srv


class AdminRoutes:
    def _post_conventions_refresh(self, body):
        """POST /api/conventions/refresh"""
        path = (body.get("path") or "").strip()
        # Only a registered project — deriving reads a caller-named path off disk and
        # spends model calls, so the path comes from the trusted registry, not the client.
        if path not in registry.projects():
            self._send(400, json.dumps({"error": "not a registered project repo"})); return
        self._send(200, json.dumps(conventions.refresh(path)))

    def _post_policy(self, body):
        """POST /api/policy"""
        # The panel POSTs the WHOLE policy on any toggle and only tracks `enabled`, so the
        # stored MCP notes are re-attached server-side rather than trusted from the client —
        # otherwise flipping one switch (or a stale tab doing it) erases every note.
        pol = _srv()._set_policy({**_srv().POLICY,
                           "capabilities": policy.keep_cap_mcp(
                               policy.load().get("capabilities", {}),
                               body.get("capabilities", {})),
                           "mcps": policy.keep_notes(policy.load().get("mcps", {}),
                                                     body.get("mcps", {}))})
        policy.save(pol)
        registry.apply_policy(_srv().CAPS, pol)   # take effect immediately
        self._send(200, json.dumps({"ok": True}))

    def _post_capability_add(self, body):
        """POST /api/capability/add"""
        name = (body.get("name") or "").strip()
        if not name:
            self._send(400, json.dumps({"error": "name is required"})); return
        if any(c.name == name for c in _srv().CAPS):
            self._send(400, json.dumps({"error": "a capability with that name already exists"})); return
        lst = policy.custom_caps()
        lst.append({"name": name, "description": body.get("description", ""),
                    "risk": body.get("risk", "write"), "prompt": body.get("prompt", "")})
        policy.save_custom_caps(lst); _srv().rebuild()
        self._send(200, json.dumps({"ok": True}))

    def _post_capability_edit(self, body):
        """POST /api/capability/edit"""
        name = (body.get("name") or "").strip()
        lst = policy.custom_caps()
        cap = next((c for c in lst if c["name"] == name), None)
        if cap is None:
            self._send(400, json.dumps({"error": "only custom capabilities can be edited"})); return
        cap["description"] = body.get("description", cap.get("description", ""))
        cap["prompt"] = body.get("prompt", cap.get("prompt", ""))
        cap["risk"] = body.get("risk", cap.get("risk", "write"))
        policy.save_custom_caps(lst)
        # Risk for custom caps is resolved by apply_policy (override > classify), so
        # persist the chosen risk as a policy override too, or the edit won't stick.
        caps_pol = {k: dict(v) for k, v in (_srv().POLICY.get("capabilities") or {}).items()}
        caps_pol.setdefault(name, {})["risk"] = cap["risk"]
        policy.save(_srv()._set_policy({**_srv().POLICY, "capabilities": caps_pol}))
        _srv().rebuild()
        self._send(200, json.dumps({"ok": True}))

    def _post_capability_remove(self, body):
        """POST /api/capability/remove"""
        lst = [c for c in policy.custom_caps() if c["name"] != body.get("name")]
        policy.save_custom_caps(lst)
        gateway.set_cap_exec(body.get("name"), None)   # drop any stale exec override
        gateway.set_cap_local_exec(body.get("name"), None)
        _srv().rebuild()
        self._send(200, json.dumps({"ok": True}))

    def _post_mcp_add(self, body):
        """POST /api/mcp/add — REGISTER a server; it does not run until activated.

        `command`+`args` are spawned as the operator on the next run that uses this server, so
        this endpoint is the largest single primitive the API exposes and adding is deliberately
        not the same act as enabling: policy.add_mcp_def stores it inactive, and /api/mcp/activate
        is where a human confirms the exact command line. Both halves are audited."""
        name = (body.get("name") or "").strip()
        cmd = (body.get("command") or "").strip()
        if not name or not cmd:
            self._send(400, json.dumps({"error": "name and command are required"})); return
        # `env` carries the credentials a stdio server needs (see mcp_client.env_for). Shape it
        # HERE rather than trusting the client: a non-string value reaches `subprocess.Popen`'s
        # env, which raises at spawn time — i.e. the server is registered, activated, and only
        # then found to be unstartable, with the error a run's problem rather than the form's.
        env = body.get("env") or {}
        if not isinstance(env, dict) or any(
                not isinstance(k, str) or not isinstance(v, str) or not k.strip()
                for k, v in env.items()):
            self._send(400, json.dumps({"error": "env must be name/value pairs of text"}))
            return
        # EDITING an existing server is the same act as adding one: `add_mcp_def` overwrites and
        # resets `confirmed`, which is exactly right — the argv changed, so the human approves
        # the new one. Only two things differ, and both matter. A value the form sent back as
        # the mask keeps whatever is stored (the form is never handed a literal credential to
        # hand back), and the trail says `edit`, so the row reads as a change to something that
        # was already running rather than a first registration.
        existed = name in policy.mcp_defs()
        entry = {"command": cmd, "args": body.get("args", [])}
        env = {k.strip(): v for k, v in env.items()}
        env = policy.merge_mcp_env(name, env) if existed else env
        if env:
            entry["env"] = env
        try:
            stored = policy.add_mcp_def(name, entry)
            _srv()._set_policy(policy.load())          # add_mcp_def cleared its safe tags (#193)
        except ValueError as e:          # an unusable server name — policy is the authority
            self._send(400, json.dumps({"error": str(e)})); return
        engine.audit_mcp_change("edit" if existed else "add", name, stored)
        self._send(200, json.dumps({"ok": True, "confirmed": False, "edited": existed,
                                    "command": policy.mcp_command_line(stored)}))

    def _post_mcp_activate(self, body):
        """POST /api/mcp/activate — the human confirmation that lets a stored def be spawned."""
        name = (body.get("name") or "").strip()
        entry = policy.confirm_mcp_def(name) if name else None
        if entry is None:
            self._send(400, json.dumps({"error": "unknown MCP server"})); return
        engine.audit_mcp_change("activate", name, entry)
        self._send(200, json.dumps({"ok": True, "confirmed": True,
                                    "command": policy.mcp_command_line(entry)}))

    def _post_mcp_note(self, body):
        """POST /api/mcp/note — operator usage guidance for one MCP server (empty text clears
        it). Written in place via policy.set_mcp_note, then swapped into the live POLICY so
        a later /api/policy save doesn't overwrite what we just stored."""
        name = (body.get("name") or "").strip()
        if not name or not any(m["name"] == name for m in policy.all_mcps(_srv().POLICY)):
            self._send(400, json.dumps({"error": "unknown MCP server"})); return
        saved = policy.set_mcp_note(name, body.get("notes", ""))
        _srv()._set_policy(saved)
        self._send(200, json.dumps({"ok": True,
                                    "notes": (saved.get("mcps", {}).get(name) or {}).get("notes", "")}))

    def _send_mcp_tools(self, name, spawn):
        """One server's tools + safe flags. `note` (never `error`, which the UI's getJSON reads
        as a failed request) says why a list is empty."""
        if not any(m["name"] == name for m in policy.all_mcps(_srv().POLICY)):
            self._send(400, json.dumps({"error": "unknown MCP server"})); return
        if spawn:
            tools, note = mcp_client.server_tools(name)
        else:
            tools = mcp_client.catalogue().get(name) or []
            note = None if tools else "not listed yet — press Re-list tools to start it once"
        pol = policy.load()
        stored = policy.safe_tool_names(pol, name)
        # Tags whose command has changed grant nothing (policy.safe_tools) — shown UNTICKED with
        # a note, so trusting the new command is a fresh decision, never an inherited one.
        live = set(stored) if (stored and ((pol.get("mcps") or {}).get(name) or {}).get("safe_key")
                               == mcp_client.def_key(name, pol)) else set()
        if stored and not live:
            note = ((note + " ") if note else "") + ("This server's command changed since its "
                                                     "tools were ticked — tick them again to trust it.")
        listed = {t.get("name") for t in tools}
        # A stored tag the list no longer shows stays visible, so it can always be UN-ticked.
        rows = list(tools) + [{"name": t, "description": "", "unlisted": True}
                              for t in stored if t not in listed]
        # `key` rides back on Save: the tags bind to the command THIS list came from (409 if it
        # changed meanwhile), never to whatever is stored when the POST lands.
        self._send(200, json.dumps({"name": name, "note": note,
                                    "key": mcp_client.def_key(name, pol), "tools": [
            {**t, "safe": t.get("name") in live} for t in rows]}))

    def _post_mcp_tools_list(self, body):
        """POST /api/mcp/tools/list — start one servable stdio server once to list its tools
        (warming the catalogue). A POST so `_csrf_ok` stands between a page and the spawn."""
        self._send_mcp_tools((body.get("name") or "").strip(), spawn=True)

    def _post_mcp_safe_tools(self, body):
        """POST /api/mcp/safe-tools — replace one server's safe-tagged tools (issue #193). Only
        names the server actually LISTS are kept: a tag is a grant, so a name nothing lists is
        one nobody reviewed. Written in place, then swapped into the live POLICY like a note."""
        name = (body.get("name") or "").strip()
        if not name or not any(m["name"] == name for m in policy.all_mcps(_srv().POLICY)):
            self._send(400, json.dumps({"error": "unknown MCP server"})); return
        listed = {t.get("name") for t in (mcp_client.catalogue().get(name) or [])}
        # Keeping a tag already stored is not a new grant, so an unlisted one may stay; a NEW
        # name must be one the server lists.
        listed |= set(policy.safe_tool_names(policy.load(), name))
        want = [t for t in (body.get("tools") or []) if isinstance(t, str)]
        unknown = [t for t in want if t not in listed]
        if unknown:
            self._send(400, json.dumps({"error": "not tools this server lists: "
                                        + ", ".join(unknown[:5])})); return
        key = mcp_client.def_key(name)
        if want and not key:
            self._send(400, json.dumps({"error": "only a launchable stdio server's tools can be "
                                        "marked safe — this one has no command to bind them to"}))
            return
        if want and body.get("key") != key:
            self._send(409, json.dumps({"error": "this server's command changed since the list "
                                        "was opened — reopen it and review again"})); return
        # Bound to the command the operator reviewed (policy.safe_tools).
        saved = policy.set_safe_tools(name, want, key=key)
        _srv()._set_policy(saved)
        engine.audit_mcp_change("safe-tools", name, detail="safe: " + (", ".join(sorted(want))
                                                                    or "(none — all gated)"))
        self._send(200, json.dumps({"ok": True,
                                    "safe_tools": policy.safe_tool_names(saved, name)}))

    def _post_mcp_remove(self, body):
        """POST /api/mcp/remove"""
        name = body.get("name")
        defs = policy.mcp_defs(); gone = defs.pop(name, None)
        policy.save_mcp_defs(defs)
        if gone is not None:
            # Its tags go with it: a later def under this name is a different command (#193).
            _srv()._set_policy(policy.set_safe_tools(name, []))
            engine.audit_mcp_change("remove", name, gone)
        self._send(200, json.dumps({"ok": True}))

    def _post_mcp_recheck(self, body):
        """POST /api/mcp/recheck"""
        self._send(200, json.dumps({"ok": True,
            "mcps": policy.all_mcps(_srv().POLICY, allow_refresh=True,
                                    force=bool(body.get("force", True)))}))

    def _post_cap_mcp(self, body):
        """POST /api/cap-mcp — which MCP servers the LOCAL backend spawns for one capability.

        Its own endpoint rather than a field on the policy POST for the reason `keep_notes`
        exists: the Admin panel re-POSTs the WHOLE capability map on any toggle, so a stale
        tab flipping a switch would wipe a declaration it never knew about.

        The names are checked against the servers Otto can actually spawn, not trusted from
        the client. This does not activate or enable anything — an unconfirmed or disabled
        def stays unspawnable (`policy.mcp_confirmed`, `mcp_client.servable`); it only says
        which of the already-usable servers this capability is about."""
        name = (body.get("name") or "").strip()
        if not any(c.name == name for c in _srv().CAPS):
            self._send(400, json.dumps({"error": "unknown capability"})); return
        servable = set(mcp_client.servable(_srv().POLICY))
        want = [n for n in (body.get("servers") or []) if isinstance(n, str)]
        unknown = [n for n in want if n not in servable]
        if unknown:
            self._send(400, json.dumps({
                "error": "not launchable stdio servers: " + ", ".join(unknown)})); return
        caps_pol = {k: dict(v) for k, v in (_srv().POLICY.get("capabilities") or {}).items()}
        entry = caps_pol.setdefault(name, {})
        if want:
            entry["mcp"] = want
        else:
            entry.pop("mcp", None)
        pol = _srv()._set_policy({**_srv().POLICY, "capabilities": caps_pol})
        policy.save(pol)
        registry.apply_policy(_srv().CAPS, pol)   # take effect immediately
        self._send(200, json.dumps({"ok": True, "servers": want}))

    def _post_cap_latch_clear(self, body):
        """POST /api/cap-latch/clear"""
        gateway.clear_cap_local((body.get("name") or "").strip() or None,
                                (body.get("model") or "").strip() or None)
        self._send(200, json.dumps({"ok": True}))

    def _post_mcp_reconnect(self, body):
        """POST /api/mcp/reconnect"""
        self._send(200, json.dumps(policy.reconnect_mcp((body.get("name") or "").strip(), _srv().POLICY)))

    def _post_project_add(self, body):
        """POST /api/project/add — a repo is registered by its remote URL; a local checkout is
        an optional accelerator (`workspace` clones from it and `file_safety` write-denies it).

        The clone runs INLINE, because a registration that returns ok and then resolves to an
        empty directory reads as "Otto imported nothing from my repo" with nothing to click.
        A shallow clone of a 200MB history takes seconds; the form's own spinner covers it."""
        url = (body.get("url") or "").strip()
        path = (body.get("path") or "").strip()
        if not url and not path:
            self._send(400, json.dumps({"error": "a repo URL is required"})); return
        p = os.path.abspath(os.path.expanduser(path)) if path else ""
        if p and not os.path.isdir(p):
            self._send(400, json.dumps({"error": "no such directory: " + p})); return
        if url and not repos.parse(url):
            self._send(400, json.dumps({"error": "not a git remote URL (expected e.g. "
                                                 "https://github.com/owner/repo)"})); return
        if url and not p:
            _, err = repos.ensure(url)
            if err:
                self._send(400, json.dumps({"error": "clone failed: " + err})); return
        url = (repos.parse(url) or {}).get("url", "") if url else ""   # store/echo one spelling
        root = registry.add_project(p, url); _srv().rebuild()
        caps = sum(1 for c in registry.project_skills() if c[4] == root)
        self._send(200, json.dumps({"ok": True, "path": root, "url": url, "capabilities": caps,
                                    "warning": "" if os.path.isdir(os.path.join(root, ".claude"))
                                    else "no .claude/ directory in this repo — it is registered "
                                         "for repo-mode and conventions, but imports no "
                                         "capabilities"}))

    def _post_project_remove(self, body):
        """POST /api/project/remove"""
        registry.remove_project(body.get("path", "")); _srv().rebuild()
        self._send(200, json.dumps({"ok": True}))

    def _post_project_instructions(self, body):
        """POST /api/project/instructions"""
        registry.set_project_instructions(body.get("path", ""), body.get("instructions", ""))
        self._send(200, json.dumps({"ok": True, "meta": registry.project_meta(body.get("path", ""))}))

    def _post_project_verify(self, body):
        """POST /api/project/verify — the command run against every repo-mode PR's head."""
        if not registry.set_project_verify(body.get("path", ""), body.get("verify", "")):
            self._send(404, json.dumps({"error": "not a registered project"})); return
        self._send(200, json.dumps({"ok": True, "meta": registry.project_meta(body.get("path", ""))}))

    def _post_bundle_import(self, body):
        """POST /api/bundle/import"""
        try:
            summary = policy.import_bundle(
                body,
                existing_caps=[c.name for c in _srv().CAPS],
                existing_mcps=[m["name"] for m in policy.all_mcps(_srv().POLICY)])
        except ValueError as e:
            self._send(400, json.dumps({"error": str(e)})); return
        _srv().rebuild()
        self._send(200, json.dumps({"ok": True, **summary}))

    def _post_profile_preview(self, body):
        """POST /api/profile/preview"""
        try:
            plan = snapshot.preview(body.get("profile"), body.get("mode") or "merge")
        except ValueError as e:
            self._send(400, json.dumps({"error": str(e)})); return
        self._send(200, json.dumps({"ok": True, **plan}))

    def _post_profile_import(self, body):
        """POST /api/profile/import — applies only the plan its `expect` fingerprint names."""
        try:
            summary = snapshot.apply(body.get("profile"), body.get("mode") or "merge",
                                     body.get("expect") or "")
        except ValueError as e:
            self._send(400, json.dumps({"error": str(e)})); return
        except snapshot.StaleSnapshotPreview as e:
            self._send(409, json.dumps({"error": str(e)})); return
        _srv()._set_policy(policy.load())
        _srv().rebuild()
        self._send(200, json.dumps({"ok": True, **summary}))

    def _post_update(self, body):
        """POST /api/update — takes no input: what is pulled is always origin/main."""
        st = _srv()._update_status()
        if st["blockers"]:
            self._send(409, json.dumps({"error": st["blockers"][0], **st})); return
        if not st["behind"]:
            self._send(409, json.dumps({"error": "Already up to date.", **st})); return
        ok, err = updater.launch(self.server.server_address[1], updater.service())
        code = 200 if ok else 409 if err == updater.ALREADY_RUNNING else 500
        self._send(code, json.dumps({"started": ok, "error": err}))

    def _post_update_check(self, body):
        """POST /api/update/check — the ONLY fetch: the header button asks on click, nothing polls."""
        updater.fetch()
        self._send(200, json.dumps(_srv()._update_status()))

    def _post_models(self, body):
        """POST /api/models

        The client is posting back the config the GET handed it, whose literal keys were masked
        (issue #27) — so `gateway.unmask` restores each one from the store before saving, or the
        save would overwrite the key with its own mask. `lost` names the values it could not
        recover (an endpoint renamed AND repointed in one save); they are cleared rather than
        stored as a bogus key, and reported so the UI can say which endpoint needs its key
        re-entered instead of leaving it to 401."""
        stored = gateway.load()
        cfg = dict(stored)
        cfg["pool"] = body.get("pool", stored.get("pool", []))
        cfg["assign"] = body.get("assign", stored.get("assign", {}))
        cfg["endpoints"] = body.get("endpoints", stored.get("endpoints", []))
        cfg, lost = gateway.unmask(cfg, stored)
        gateway.save(cfg)
        self._send(200, json.dumps({"ok": not lost, "lost_keys": lost,
                                    "endpoints": gateway.endpoints(gateway.masked(gateway.load()))}))

    def _post_models_discover(self, body):
        """POST /api/models/discover"""
        ep = next((e for e in gateway.endpoints() if e["name"] == body.get("endpoint")),
                  None) if body.get("endpoint") else None
        url = (ep or body).get("base_url") or ""
        if not url:
            self._send(200, json.dumps({"ok": False, "detail": "no endpoint / base URL"}))
        else:
            try:
                found = gateway.discover_models(
                    url, (ep or body).get("api_key_env") or "",
                    headers=(ep or body).get("headers") or {})
                known = {m.get("model") for m in gateway.load().get("pool", [])
                         if m.get("endpoint") == (ep or {}).get("name")}
                # `served` is every id before grouping — a dedupe the operator can't see
                # reads as "that was the whole catalogue".
                self._send(200, json.dumps(
                    {"ok": True, "models": found,
                     "served": sum(1 + len(e["aliases"]) for e in found),
                     "known": sorted(x for x in known if x)}))
            except Exception as e:  # noqa: BLE001 - a dead endpoint is a UI verdict
                self._send(200, json.dumps({"ok": False, "detail": str(e)[:180]}))

    def _post_models_claude(self, body):
        """POST /api/models/claude — the Claude models available to add, and the list's source
        (`api` when ANTHROPIC_API_KEY could enumerate them, `known` for the built-in fallback)."""
        self._send(200, json.dumps(gateway.claude_catalog()))

    def _post_models_recheck(self, body):
        """POST /api/models/recheck"""
        self._send(200, json.dumps({"ok": True, "health": gateway.probe_models(force=True)}))

    def _post_models_capexec(self, body):
        """POST /api/models/capexec"""
        overrides = gateway.set_cap_exec(body.get("name"), body.get("model") or None)
        self._send(200, json.dumps({"ok": True, "cap_exec": overrides}))

    def _post_models_caplocal(self, body):
        """POST /api/models/caplocal"""
        overrides = gateway.set_cap_local_exec(body.get("name"), body.get("model") or None)
        self._send(200, json.dumps({"ok": True, "cap_local_exec": overrides}))

    def _post_onboarding_preview(self, body):
        """POST /api/onboarding/preview — the settings diff + integration checklist for `answers`.
        MCPs are the CACHED listing: the wizard waits on this, so it must never shell out."""
        self._send(200, json.dumps({"ok": True, **onboarding.preview(
            body.get("answers"), **_srv()._mcp_view())}))

    def _post_onboarding_apply(self, body):
        """POST /api/onboarding/apply — applies only the plan its `expect` fingerprint names."""
        try:
            out = onboarding.apply(body.get("answers"), body.get("expect") or "")
        except onboarding.StalePreview as e:
            self._send(409, json.dumps({"error": str(e)})); return
        self._send(200, json.dumps({"ok": True, **out}))

    def _post_onboarding_draft(self, body):
        """POST /api/onboarding/draft — park the wizard's answers (an integration detour)."""
        self._send(200, json.dumps({"ok": True, **onboarding.save_draft(body.get("answers"))}))

    def _post_onboarding_recheck(self, body):
        """POST /api/onboarding/recheck — re-run every lookup the review reads (Slack scopes,
        the gh login, the connector list) and answer when they are done. A POST: it spawns every
        stdio MCP server, so it sits behind _csrf_ok like any other side effect."""
        # `connectors:false` (a resume) re-asks the cheap lookups only — no MCP server spawn.
        only = body.get("probes")
        only = {p for p in only if isinstance(p, str)} if isinstance(only, list) else None
        out = _srv()._recheck(mcp=body.get("connectors", True) is not False, only=only)
        self._send(200, json.dumps({"ok": True, **out}))

    def _post_onboarding_skip(self, body):
        """POST /api/onboarding/skip"""
        self._send(200, json.dumps({"ok": True, **onboarding.skip(body.get("answers"))}))

    def _post_settings(self, body):
        """POST /api/settings"""
        self._send(200, json.dumps(
            {"ok": True, "settings": config.save_settings(body.get("settings") or {})}))

    def _post_models_probe(self, body):
        """POST /api/models/probe — probe a model the operator is about to ADD, before it is
        saved. `{entry: {...}}` in, `{ok, detail}` out; nothing is stored either way."""
        self._send(200, json.dumps(gateway.probe_candidate(body.get("entry") or {})))

    def _post_models_test(self, body):
        """POST /api/models/test"""
        self._send(200, json.dumps(gateway.test_model(body.get("name"))))
