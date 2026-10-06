"""Portable config snapshot (#166) — one install's WHOLE configuration, carried to another.

Every `data/` store is classified once, below: EXPORTED (a section carries it) or RUNTIME (earned
or derived per install — memory, audit, transcripts, cursors, caches). A store in neither fails
`SnapshotStoreClassificationTests`.

Import is two calls: `preview` lists every change and a fingerprint of them, `apply` recomputes
the plan and refuses unless the fingerprint still matches — so an import changes exactly what
its preview showed. `merge` never overwrites (a differing item is reported as kept), except on a
store this install has never written, which is matched outright; `replace` makes the target match.

Nothing an import writes can start work by itself: an ingress, trigger, webhook rule or cron that
the import changes lands DISABLED (crons as a paused Temporal schedule). Secret-free by
construction: an env-var NAME travels, a literal never does, and a blank secret in the snapshot
never overwrites a local one. Surfaces: `profile.py`, GET /api/profile/export,
POST /api/profile/{preview,import}.
"""
import copy
import datetime
import hashlib
import json
import os
import re
import time

import config
import storage

VERSION = 2

EXPORTED = {
    "settings.json": "settings",
    "capabilities.json": "capabilities",
    "mcp-servers.json": "mcp_servers",
    "policy.json": "policy",
    "models.json": "models",
    "projects.json": "projects",
    "runbooks.json": "runbooks",
    "runbook-order.json": "runbooks",
    "slack.json": "slack",
    "slack-triggers.json": "slack_triggers",
    "event-rules.json": "event_rules",
    "pr-review.json": "pr_review",
    "board.json": "board",
}
# otto.db is runtime as a FILE; its behaviors + knowledge tables travel as their own sections.
RUNTIME = frozenset({
    "otto.db", "conventions.json", "notify-state.json", "event-replay.json",
    "gateway-stats.json", "mcp-tools.json", "mcp-connectors-cache.json", "pr-review-state.json",
    "slack-state.json", "slack-triggers-state.json", "schedules.json", "dismissed.json",
    "retries.json", "update-state.json", "ESTOP", "transcripts", "codex-home", "local-sessions", "repos", "logs",
    "workspaces", "uploads", ".api",
})

MODES = ("merge", "replace")
_ENV_REF = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")


class StaleSnapshotPreview(Exception):
    """The plan changed between preview and apply — nothing was written."""


# --- secret handling ---------------------------------------------------------------------

def _resolves(name):
    return bool(os.environ.get(name) or config.secret(name))


def _portable_ref(v):
    """An env-var NAME this machine resolves travels; anything else may be a pasted key and is
    blanked. The name SHAPE alone isn't enough: an AWS access key id (AKIA…) matches it."""
    v = str(v or "")
    return v if v and config.is_secret_ref(v) and _resolves(v) else ""


def _safe_conn(d):
    """An endpoint / legacy pool entry with its credential fields made portable. Returns
    (entry, lost) — lost is True when a credential had to be blanked."""
    d = copy.deepcopy(d)
    lost = False
    if d.get("api_key_env"):
        d["api_key_env"] = _portable_ref(d["api_key_env"])
        lost = lost or not d["api_key_env"]
    if isinstance(d.get("headers"), dict):
        d["headers"] = {h: _portable_ref(v) for h, v in d["headers"].items()}
        lost = lost or any(not v for v in d["headers"].values())
    return d, lost


def _keep_local_secrets(inc, loc):
    """A blank credential field in the snapshot means "set it on this machine", never "wipe it"."""
    if not loc:
        return inc
    out = copy.deepcopy(inc)
    if "api_key_env" in out and not out["api_key_env"] and loc.get("api_key_env"):
        out["api_key_env"] = loc["api_key_env"]
    if isinstance(out.get("headers"), dict) and isinstance(loc.get("headers"), dict):
        for h, v in out["headers"].items():
            if not v and loc["headers"].get(h):
                out["headers"][h] = loc["headers"][h]
    return out


def _missing_ref(v, label, what):
    if v and config.is_secret_ref(v) and not _resolves(v):
        return {"name": v, "for": label, "what": what}
    return None


def _mask_secret(v):
    """`gateway.mask_value` trusts the NAME shape, which an AKIA… literal has."""
    import gateway
    v = str(v or "")
    return v if not v or _ENV_REF.match(v) or _portable_ref(v) else gateway.MASK + v[-4:]


def _mask(v):
    """Preview output view: `before` holds LOCAL values, literal keys included."""
    if isinstance(v, list):
        return [_mask(x) for x in v]
    if not isinstance(v, dict):
        return v
    out = {}
    for k, x in v.items():
        if k == "api_key_env":
            out[k] = _mask_secret(x)
        elif k in ("headers", "env") and isinstance(x, dict):
            out[k] = {h: _mask_secret(y) for h, y in x.items()}
        else:
            out[k] = _mask(x)
    return out


def _redact(v):
    import privacy
    if isinstance(v, str):
        return privacy.redact(v)
    if isinstance(v, list):
        return [_redact(x) for x in v]
    if isinstance(v, dict):
        return {k: _redact(x) for k, x in v.items()}
    return v


def _sans(d, *keys):
    return {k: v for k, v in (d or {}).items() if k not in keys}


# --- sections ----------------------------------------------------------------------------
# A section is a keyed dict of items. `prepare` turns snapshot items into what this install
# WOULD store (secrets kept, ingresses disabled), so the diff is against the real write.
# Sections are shared singletons in a threaded server: per-call notes go in `ctx`, never `self`.

class _Section:
    whole = False          # one config object diffed per field, never removed from
    path = None

    def export(self, ctx):
        return self.local()

    def local(self):
        raise NotImplementedError

    def exists(self):
        return bool(self.path and os.path.exists(self.path()))

    def prepare(self, inc, loc, ctx):
        return inc

    def secrets(self, items, snap):
        return []

    def write(self, new, applied, loc, ctx):
        raise NotImplementedError

    def after(self, applied):
        return None


class _Settings(_Section):
    def path(self):
        return config._settings_path()

    def local(self):
        raw = storage.read_json(self.path(), {}) or {}
        return {k: v for k, v in raw.items() if k in config._SETTING_SPECS}

    def prepare(self, inc, loc, ctx):
        return {k: v for k, v in inc.items() if k in config._SETTING_SPECS}

    def write(self, new, applied, loc, ctx):
        defaults = config.settings_all()
        config.save_settings({c["key"]: (defaults[c["key"]]["default"] if c["action"] == "remove"
                                         else c["after"]) for c in applied})


class _Capabilities(_Section):
    def path(self):
        import policy
        return policy._CUSTOM

    def local(self):
        import policy
        return {c["name"]: c for c in policy.custom_caps() if c.get("name")}

    def prepare(self, inc, loc, ctx):
        out = {}
        for n, c in inc.items():
            if not isinstance(c, dict) or not str(n).strip():
                continue
            out[n] = {**(loc.get(n) or {}), "name": n, "description": str(c.get("description", "")),
                      "risk": "read" if c.get("risk") == "read" else "write",
                      "prompt": str(c.get("prompt", ""))}
        return out

    def write(self, new, applied, loc, ctx):
        import policy
        policy.save_custom_caps(list(new.values()))


class _McpServers(_Section):
    def path(self):
        import policy
        return policy._MCPDEF

    def export(self, ctx):
        import policy
        out = {}
        for n, d in policy.mcp_defs().items():
            e = {"command": d.get("command", ""), "args": list(d.get("args", []))}
            if d.get("env"):
                e["env"] = {k: (v if _ENV_REF.match(str(v or "")) else "")
                            for k, v in d["env"].items()}
            out[n] = e
        return out

    def local(self):
        import policy
        return copy.deepcopy(policy.mcp_defs())

    def prepare(self, inc, loc, ctx):
        out = {}
        for n, d in inc.items():
            import policy
            if not policy.valid_mcp_name(n):
                continue
            e = {"command": str(d.get("command", "")), "args": [str(a) for a in d.get("args", [])]}
            env = {k: (v if _ENV_REF.match(str(v or "")) else "")
                   for k, v in (d.get("env") or {}).items()}
            prior = loc.get(n)
            if prior:
                env = {k: (v or (prior.get("env") or {}).get(k, "")) for k, v in env.items()}
                same_cmd = (prior.get("command"), prior.get("args")) == (e["command"], e["args"])
                e = {**prior, **e, "confirmed": bool(prior.get("confirmed")) and same_cmd}
            else:
                # Stamped UNCONFIRMED: a command line from another machine is exactly what
                # activation exists for (policy.add_mcp_def).
                e["confirmed"] = False
            if env:
                e["env"] = env
            else:
                e.pop("env", None)
            out[n] = e
        return out

    def secrets(self, items, snap):
        return [{"name": k, "for": f"MCP server {n}", "what": "env value (Admin → MCP servers)"}
                for n, d in items.items() for k, v in (d.get("env") or {}).items() if not v]

    def write(self, new, applied, loc, ctx):
        import policy
        now = time.time()
        policy.save_mcp_defs({n: ({**d, "added_at": d.get("added_at") or now}) for n, d in new.items()})


class _Policy(_Section):
    def path(self):
        import policy
        return policy._PATH

    def local(self):
        import policy
        pol = policy.load()
        out = {f"cap:{n}": v for n, v in (pol.get("capabilities") or {}).items()
               if isinstance(v, dict)}
        out.update({f"mcp:{n}": v for n, v in (pol.get("mcps") or {}).items() if isinstance(v, dict)})
        return out

    _KEYS = {"cap": ("risk", "enabled", "tool_free", "mcp"), "mcp": ("enabled", "notes")}

    def prepare(self, inc, loc, ctx):
        import policy
        out = {}
        for k, v in inc.items():
            kind = str(k).split(":", 1)[0]
            if kind not in self._KEYS or not isinstance(v, dict):
                continue
            v = {f: v[f] for f in self._KEYS[kind] if f in v}
            if "notes" in v:
                v["notes"] = str(v["notes"] or "").strip()[:policy.MCP_NOTE_MAX]
            out[k] = {**_sans(loc.get(k), *self._KEYS[kind]), **v}
        return out

    def write(self, new, applied, loc, ctx):
        import policy
        caps = {k[4:]: v for k, v in new.items() if k.startswith("cap:")}
        mcps = {k[4:]: v for k, v in new.items() if k.startswith("mcp:")}
        storage.mutate_json(policy._PATH, lambda p: {**(p or {}), "capabilities": caps,
                                                     "mcps": mcps},
                            {"capabilities": {}, "mcps": {}})


class _Models(_Section):
    """Endpoints travel with the pool, so an entry naming `Acme Dev` still resolves."""
    _MAPS = ("assign", "cap_exec", "cap_local_exec")

    def path(self):
        import gateway
        return gateway._PATH

    def local(self):
        import gateway
        cfg = gateway._dehydrate(gateway.load())
        out = {f"endpoint:{e['name']}": e for e in cfg.get("endpoints") or []}
        out.update({f"model:{m['name']}": m for m in cfg.get("pool") or [] if m.get("name")})
        for key in self._MAPS:
            out.update({f"{key}:{k}": v for k, v in (cfg.get(key) or {}).items()})
        out["pool_order"] = [m["name"] for m in cfg.get("pool") or [] if m.get("name")]
        return out

    def export(self, ctx):
        out = self.local()
        for k, v in list(out.items()):
            if k.startswith(("endpoint:", "model:")):
                out[k], lost = _safe_conn(v)
                if lost:
                    ctx["lost"].append(k)
        return out

    def prepare(self, inc, loc, ctx):
        import gateway
        known = gateway._tier_ids()
        out = {}
        for k, v in inc.items():
            if k.startswith(("endpoint:", "model:")) and isinstance(v, dict):
                v = _keep_local_secrets(v, loc.get(k))
                # A Claude tier's id is this machine's catalogue, re-derived on every load.
                if k.startswith("model:") and v.get("name") in known:
                    v = {**v, "model": known[v["name"]]}
            out[k] = v
        return out

    def secrets(self, items, snap):
        lost = set((snap.get("needs") or {}).get("models") or [])
        out = []
        for k, v in items.items():
            if not k.startswith(("endpoint:", "model:")):
                continue
            label = k.replace(":", " ", 1)
            if k in lost and not v.get("api_key_env") and v.get("base_url"):
                out.append({"name": "", "for": label, "what": "API key (not exported)"})
            miss = _missing_ref(v.get("api_key_env"), label, "env var")
            if miss:
                out.append(miss)
            for h, hv in (v.get("headers") or {}).items():
                miss = _missing_ref(hv, label, f"header {h}")
                if miss:
                    out.append(miss)
                elif not hv:
                    out.append({"name": "", "for": label, "what": f"header {h} (not exported)"})
        return out

    def write(self, new, applied, loc, ctx):
        import gateway
        order = new.get("pool_order") or []
        pool = {k[6:]: v for k, v in new.items() if k.startswith("model:")}
        names = [n for n in order if n in pool] + [n for n in pool if n not in order]

        def _fn(cfg):
            cfg["endpoints"] = [v for k, v in new.items() if k.startswith("endpoint:")]
            cfg["pool"] = [copy.deepcopy(pool[n]) for n in names]
            for key in self._MAPS:
                cfg[key] = {k[len(key) + 1:]: v for k, v in new.items() if k.startswith(key + ":")}
        gateway._mutate(_fn)


class _Behaviors(_Section):
    def exists(self):
        return True

    def local(self):
        import engine
        return {self._key(b): {"rule": b["rule"], "scope": b.get("scope") or "global"}
                for b in engine.behaviors() if b.get("rule")}

    @staticmethod
    def _key(b):
        return f"{b.get('scope') or 'global'}|{' '.join((b.get('rule') or '').split())}"

    def write(self, new, applied, loc, ctx):
        import engine
        ids = {self._key(b): b["id"] for b in engine.behaviors()}
        for c in applied:
            if c["action"] == "remove" and c["key"] in ids:
                engine.delete_behavior(ids[c["key"]])
            elif c["action"] == "add":
                engine.add_behavior(c["after"]["rule"], c["after"]["scope"])


class _Knowledge(_Section):
    def exists(self):
        import knowledge
        return bool(knowledge.documents() or knowledge.settings().get("embed_model"))

    def local(self):
        import knowledge
        s = knowledge.settings()
        out = {"settings": {"threshold": s.get("threshold"), "embed_model": s.get("embed_model")}}
        for d in knowledge.export_docs():
            out.setdefault(f"doc:{d['title']}", d)
        return out

    def write(self, new, applied, loc, ctx):
        import knowledge
        ids = {}
        for d in knowledge.documents():
            ids.setdefault(d["title"], d["id"])
        for c in applied:
            if c["key"] == "settings":
                s = c["after"] or {}
                knowledge.set_settings(threshold=s.get("threshold"),
                                       embed_model=s.get("embed_model") or "")   # None = "leave it"
                continue
            title = c["key"][4:]
            if c["action"] in ("add", "update"):         # add FIRST: a failed add keeps the old doc
                d = c["after"]
                knowledge.add_document(d["title"], d["text"], source=d.get("source") or "profile-import")
            if c["action"] in ("update", "remove") and title in ids:
                knowledge.delete_document(ids[title])


class _Projects(_Section):
    """Keyed by the repo's remote URL; a checkout path is machine-local and never travels."""

    def path(self):
        import registry
        return registry.PROJECTS_FILE

    def _entries(self):
        import registry
        import repos
        out, unportable = {}, []
        for e in registry._project_entries():
            url = e.get("url") or ""
            if not url and e.get("path") and os.path.isdir(e["path"]):
                url = (repos.parse(repos.origin_of(e["path"]) or "") or {}).get("url", "")
            if not url:
                unportable.append(os.path.basename((e.get("path") or "").rstrip("/")) or "?")
                continue
            out.setdefault(url, ({"url": url, "instructions": e.get("instructions", "")},
                                 registry.project_path(e)))
        return out, unportable

    def local(self):
        return {u: item for u, (item, _) in self._entries()[0].items()}

    def export(self, ctx):
        items, unportable = self._entries()
        ctx["warnings"] += [f"project {n}: no remote URL, not exported" for n in unportable]
        return {u: item for u, (item, _) in items.items()}

    def prepare(self, inc, loc, ctx):
        import repos
        out = {}
        for v in inc.values():
            url = (repos.parse((v or {}).get("url") or "") or {}).get("url")
            if url:
                out[url] = {"url": url, "instructions": (v.get("instructions") or "").strip()}
        return out

    def write(self, new, applied, loc, ctx):
        import registry
        import repos
        paths = {u: p for u, (_, p) in self._entries()[0].items()}
        for c in applied:
            if c["action"] == "remove" and c["key"] in paths:
                registry.remove_project(paths[c["key"]])
            elif c["action"] == "add":
                _, err = repos.ensure(c["key"])
                if err:
                    ctx["status"].append(f"{c['key']}: clone failed ({err})")
                root = registry.add_project(url=c["key"])
                if c["after"].get("instructions"):
                    registry.set_project_instructions(root, c["after"]["instructions"])
            elif c["action"] == "update" and c["key"] in paths:
                registry.set_project_instructions(paths[c["key"]], c["after"].get("instructions", ""))


class _Runbooks(_Section):
    def path(self):
        import runbooks
        return runbooks.store_path()

    def local(self):
        import runbooks
        out = dict(runbooks.load())
        order = runbooks.order()
        if order:
            out["order"] = order
        return out

    def prepare(self, inc, loc, ctx):
        import runbooks
        out = {}
        for k, v in inc.items():
            if k == "order":
                out[k] = [str(i) for i in v or []] if isinstance(v, list) else []
                continue
            if not str(k).startswith(runbooks.ID_PREFIX):
                ctx["warnings"].append(f"runbooks: {k}: not a runbook id")
                continue
            try:
                out[k] = runbooks.normalize(v or {})
            except ValueError as e:
                ctx["warnings"].append(f"runbooks: {(v or {}).get('name') or k}: {e}")
        return out

    def write(self, new, applied, loc, ctx):
        """Schedules FIRST: a cron stored without its paused schedule is recreated UNPAUSED by
        the next startup `reconcile`, so one that can't be created paused is not stored."""
        import runbooks
        import scheduler
        for c in applied:
            k = c["key"]
            if k == "order":
                continue
            try:
                if c["action"] == "remove":
                    if scheduler.available():
                        scheduler.tc.run(scheduler._delete(k))
                elif (c["after"] or {}).get("cron"):
                    scheduler.sync_paused(k, c["after"])
                    ctx["status"].append(f"{k}: schedule created paused")
                elif c["action"] == "update" and scheduler.available():
                    scheduler.tc.run(scheduler._sync(k, c["after"]))
            except Exception as e:  # noqa: BLE001 - reported; that runbook stays as it was
                ctx["status"].append(f"{k}: not imported, schedule sync failed ({str(e)[:80]})")
                if k in loc:
                    new[k] = loc[k]
                else:
                    new.pop(k, None)
        defs = {k: v for k, v in new.items() if k != "order"}
        storage.mutate_json(runbooks.store_path(), lambda _s: defs, default={})
        if "order" in new:
            runbooks.set_order(new["order"])


class _Whole(_Section):
    """A single config object (slack/board/pr-review), diffed per field."""
    whole = True
    FLAGS = ("enabled",)

    def mod(self):
        raise NotImplementedError

    def path(self):
        return self.mod().config_path()

    def local(self):
        return self.mod().load()

    def prepare(self, inc, loc, ctx):
        known = self.mod()._DEFAULTS
        out = {k: v for k, v in inc.items() if k in known}
        # Unchanged apart from the switches: nothing new would run, so the local switches stand.
        same = _sans({**loc, **out}, *self.FLAGS) == _sans(loc, *self.FLAGS)
        for f in self.FLAGS:
            out[f] = loc.get(f, False) if same else False
        return out

    def write(self, new, applied, loc, ctx):
        self.mod().save(new)

    def after(self, applied):
        return [str(self.mod().reconcile_schedule())]


class _Slack(_Whole):
    FLAGS = ("enabled", "bot_enabled")

    def mod(self):
        import slack
        return slack

    def secrets(self, items, snap):
        out = []
        if items.get("allow_users") or items.get("allow_channels"):
            out.append("OTTO_SLACK_USER_TOKEN")
        if items.get("bot_allow_users") or items.get("bot_allow_channels"):
            out.append("OTTO_SLACK_BOT_TOKEN")
        return [{"name": n, "for": "Slack", "what": "env var"} for n in out if not _resolves(n)]

    def after(self, applied):
        import slack_socket
        return [str(self.mod().reconcile_schedule()), str(slack_socket.reconcile())]


class _Board(_Whole):
    def mod(self):
        import board
        return board


class _PrReview(_Whole):
    def mod(self):
        import pr_review
        return pr_review


class _Rules(_Section):
    """A rule list whose rules fire on their own; an imported or changed rule lands disabled."""

    def prepare(self, inc, loc, ctx):
        out = {}
        for r in inc.values():
            r = self.normalize(r)
            if not r:
                continue
            k = self.key(r)                      # the key `local` derives, not the file's
            prior = loc.get(k)
            same = prior is not None and _sans(prior, "enabled") == _sans(r, "enabled")
            if same and prior.get("enabled", True) is not False:
                r.pop("enabled", None)
            else:
                r["enabled"] = False
            out[k] = r
        return out

    def write(self, new, applied, loc, ctx):
        self.save(list(new.values()))


class _SlackTriggers(_Rules):
    def path(self):
        import slack_triggers
        return slack_triggers._RULES

    def normalize(self, r):
        import slack_triggers
        return slack_triggers.normalize(r)

    @staticmethod
    def key(r):
        return r["id"]

    def local(self):
        import slack_triggers
        return {r["id"]: r for r in slack_triggers.load_rules()}

    def save(self, rules):
        import slack_triggers
        slack_triggers.save_rules(rules)

    def after(self, applied):
        import slack
        return [str(slack.reconcile_schedule())]


class _EventRules(_Rules):
    def path(self):
        import events
        return events._RULES

    @staticmethod
    def key(r):
        return hashlib.sha256(json.dumps(_sans(r, "enabled"), sort_keys=True).encode()).hexdigest()[:10]

    def normalize(self, r):
        ok = isinstance(r, dict) and (r.get("source") or "").strip() and (r.get("template") or "").strip()
        return dict(r) if ok else None

    def local(self):
        import events
        return {self.key(r): r for r in events.load_rules() if isinstance(r, dict)}

    def save(self, rules):
        import events
        events.save_rules(rules)

    def secrets(self, items, snap):
        return ([{"name": "OTTO_EVENT_SECRET", "for": "webhook rules", "what": "env var"}]
                if items and not _resolves("OTTO_EVENT_SECRET") else [])


SECTIONS = {
    "settings": _Settings(),
    "capabilities": _Capabilities(),
    "mcp_servers": _McpServers(),
    "policy": _Policy(),
    "models": _Models(),
    "behaviors": _Behaviors(),
    "knowledge": _Knowledge(),
    "projects": _Projects(),
    "runbooks": _Runbooks(),
    "slack": _Slack(),
    "slack_triggers": _SlackTriggers(),
    "event_rules": _EventRules(),
    "pr_review": _PrReview(),
    "board": _Board(),
}


# --- export / preview / apply ------------------------------------------------------------

def _ctx():
    return {"lost": [], "warnings": [], "status": []}


def export():
    ctx = _ctx()
    sections = {name: sec.export(ctx) for name, sec in SECTIONS.items()}
    snap = {
        "otto_profile": VERSION,
        "exported_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "sections": sections,
        "needs": {"models": ctx["lost"]},
        "warnings": ctx["warnings"],
    }
    return _redact(snap)


def _check(snap, mode):
    if not isinstance(snap, dict) or "otto_profile" not in snap:
        raise ValueError("not an Otto profile (missing 'otto_profile')")
    if snap.get("otto_profile") != VERSION or not isinstance(snap.get("sections"), dict):
        raise ValueError(f"profile format {snap.get('otto_profile')!r} is not supported — "
                         f"re-export it from the source install (format {VERSION})")
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")


def _cron_blocked(sec, change):
    if sec is not SECTIONS["runbooks"] or change["key"] == "order":
        return False
    return change["action"] != "remove" and bool((change["after"] or {}).get("cron"))


def _plan(snap, mode):
    _check(snap, mode)
    changes, secrets, ctx = [], [], _ctx()
    temporal = None
    for name, sec in SECTIONS.items():
        inc = snap["sections"].get(name)
        if not isinstance(inc, dict):
            continue                      # absent section: untouched, even under replace
        loc = sec.local()
        items = sec.prepare(inc, loc, ctx)
        fresh = not sec.exists()
        for k, v in items.items():
            if k not in loc:
                action = "add"
            elif loc[k] == v:
                continue
            else:
                action = "update" if (mode == "replace" or fresh) else "keep"
            c = {"section": name, "key": k, "action": action, "before": loc.get(k), "after": v}
            if action != "keep" and _cron_blocked(sec, c):
                if temporal is None:
                    import scheduler
                    temporal = scheduler.available()
                if not temporal:
                    c["action"], c["reason"] = "keep", "Temporal is down — a cron can't be created paused"
            changes.append(c)
        # A store never written here holds DEFAULTS, not a choice: match it, removals included.
        if (mode == "replace" or fresh) and not sec.whole:
            changes += [{"section": name, "key": k, "action": "remove", "before": v, "after": None}
                        for k, v in loc.items() if k not in items]
        secrets += sec.secrets(items, snap)
    applied = [c for c in changes if c["action"] != "keep"]
    fp = hashlib.sha256(json.dumps(applied, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return {"mode": mode, "fingerprint": fp, "changes": changes, "secrets": secrets,
            "warnings": ctx["warnings"] + list(snap.get("warnings") or [])}


def _view(plan):
    return {**plan, "changes": [{**c, "before": _mask(c["before"]), "after": _mask(c["after"])}
                                for c in plan["changes"]]}


def preview(snap, mode="merge"):
    """Every change `apply` would make, secrets masked, plus the fingerprint it must be given."""
    return _view(_plan(snap, mode))


def apply(snap, mode, expect):
    """Apply the plan `preview` showed. Raises StaleSnapshotPreview if it no longer matches."""
    plan = _plan(snap, mode)
    if plan["fingerprint"] != expect:
        raise StaleSnapshotPreview("this install changed since the preview — preview again")
    status, failed = [], set()
    for name, sec in SECTIONS.items():
        applied = [c for c in plan["changes"] if c["section"] == name and c["action"] != "keep"]
        if not applied:
            continue
        loc = sec.local()
        new = dict(loc)
        for c in applied:
            if c["action"] == "remove":
                new.pop(c["key"], None)
            else:
                new[c["key"]] = c["after"]
        ctx = _ctx()
        try:
            sec.write(new, applied, loc, ctx)
        except Exception as e:  # noqa: BLE001 - reported; the other sections still apply
            failed.add(name)
            status.append(f"{name}: failed, may be partly applied ({str(e)[:120]})")
            continue
        finally:
            status += [f"{name}: {m}" for m in ctx["status"]]
        try:
            status += [f"{name}: {s}" for s in (sec.after(applied) or []) if s]
        except Exception as e:  # noqa: BLE001 - a schedule hiccup must not undo the writes
            status.append(f"{name}: reconcile failed ({str(e)[:80]})")
    view = _view(plan)
    return {"mode": mode, "failed": sorted(failed),
            "applied": [c for c in view["changes"] if c["action"] != "keep" and c["section"] not in failed],
            "kept": [c for c in view["changes"] if c["action"] == "keep"],
            "secrets": plan["secrets"], "warnings": plan["warnings"], "status": status}
