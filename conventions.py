"""Repo-conventions digest — the target repo's own CLAUDE.md, distilled and injected
automatically into Otto's judged phases (verify, QA judge).

Why: the EXECUTOR already reads the repo's CLAUDE.md for free (`claude -p` loads it from
cwd in repo-mode / project caps), but the judge phases are bare `gateway.complete` calls
with no repo context — so a request that itself prescribes a convention violation (e.g. a
ticket saying "generate the secret with random_password, it's acceptable here") sails
through verification because the output faithfully matches the request. The digest gives
the judges the repo's hard rules plus an explicit precedence instruction: conventions
override the request. Always on, zero-config — derived from the repo itself, never from
anything the user has to curate in Otto (PR #251 post-mortem).

Distillation runs once per repo on the cheap 'memory' tier (local-eligible — see
test_core.PortabilityTests) and is cached in data/conventions.json keyed on the source
files' mtime+size, so steady-state verify calls add zero LLM cost. If the model is
unreachable a pure keyword heuristic extracts the constraint-shaped lines instead —
the digest degrades, it never disappears. NOT a security control: like behaviour rules,
it never changes risk, the approval gate, or tool allowlists.
"""

import glob
import os
import re

import config
import gateway
import storage
from ui import trace

_STORE = os.path.join(config.DATA_DIR, "conventions.json")
# Both locations Claude Code itself treats as project memory, plus the on-demand rules files a
# repo splits its layer detail into. A rule in a file listed nowhere here is invisible to every
# judge, so a repo that moves rules out of CLAUDE.md silently loses their enforcement.
_SOURCES = ("CLAUDE.md", os.path.join(".claude", "CLAUDE.md"))
_SOURCE_GLOBS = (os.path.join(".claude", "rules", "*.md"),)
_SOURCE_CHARS = 80_000      # total source bound fed to distillation
_CHUNK_CHARS = 12_000       # per-call input bound (the memory tier can be a small local model)
_DIGEST_CHARS = 2_000       # last-resort bound if the settings store is unreadable;
                            # the live value is config.CONVENTIONS_DIGEST_CHARS
_MAX_RULES = 500            # sanity bound on the CACHED extraction (not on what a judge sees)
_MARKERS = ("never ", "always ", "must ", "must not", "do not ", "don't ")
# Cache schema version. v1 stored the already-truncated 2k-char digest, so a v1 entry whose
# fingerprint still matches would serve that truncation forever — bump to force re-derivation.
# v2 entries were extracted before `_distill` excluded operator instructions, so they still
# carry rules no judge can check — bump again.
_CACHE_V = 3


def _source_paths(project_path):
    """Relative paths of every convention source in a repo, in a STABLE order — the fixed
    locations first, then each glob's matches sorted. The ONE list: `_fingerprint` and
    `_read_sources` must agree exactly or a cache entry never matches and re-derives forever."""
    rels = list(_SOURCES)
    for pattern in _SOURCE_GLOBS:
        hits = glob.glob(os.path.join(project_path, pattern))
        rels.extend(sorted(os.path.relpath(h, project_path) for h in hits))
    return rels


def _fingerprint(project_path):
    """The change fingerprint alone, by stat — no file reads, no derivation. `status()` runs on
    every Admin page load, and `loadAdmin` fans its fetches out in one `Promise.all`, so anything
    slow here becomes the panel's load time."""
    fp = {}
    for rel in _source_paths(project_path):
        try:
            st = os.stat(os.path.join(project_path, rel))
        except OSError:
            continue
        fp[rel] = [int(st.st_mtime), st.st_size]
    return fp


def _read_sources(project_path):
    """Concatenated convention text for a repo + a change fingerprint {relpath: [mtime, size]}.
    Returns ("", {}) when the repo has no convention file at any known location."""
    text, fingerprint = [], {}
    for rel in _source_paths(project_path):
        path = os.path.join(project_path, rel)
        try:
            st = os.stat(path)
            with open(path, encoding="utf-8", errors="replace") as f:
                text.append(f.read())
            fingerprint[rel] = [int(st.st_mtime), st.st_size]
        except OSError:
            continue
    return "\n\n".join(text)[:_SOURCE_CHARS], fingerprint


def _heuristic_rules(text):
    """Pure fallback extraction: the constraint-shaped lines (never/always/must/do not),
    deduped, bounded by _MAX_RULES. Noisier than the model digest but deterministic and free.
    Bounded for the CACHE only — `select_rules` does the per-request trimming."""
    seen, rules = set(), []
    for line in text.splitlines():
        line = line.strip().lstrip("-*• ").strip()
        low = line.lower()
        if not line or not any(m in low for m in _MARKERS):
            continue
        if low in seen:
            continue
        seen.add(low)
        rules.append(line[:300])
        if len(rules) >= _MAX_RULES:
            break
    return rules


_OPERATOR_LABEL = re.compile(r"^\**\s*operator\s*\**\s*:\s*", re.I)
_CODE_LABEL = re.compile(r"^\**\s*code\s*\**\s*:\s*", re.I)


def _judgeable(line):
    """One extracted line, stripped of its kind label — "" if the model called it OPERATOR.

    The judge sees only the run's final text output, so a rule about what a PERSON must do
    around the change (restart the worker, run the suite first) can never be satisfied by any
    result, and a judge holding one fails compliant work for it. The prompt does the semantic
    sort because phrasing varies; this does the DROP, so the exclusion is testable without a
    model in the loop.

    An UNLABELLED line is kept: the model ignoring the format entirely is a formatting failure,
    not a verdict that the rule is operator-only, and this module's contract is that the digest
    degrades rather than disappears. Keeping a stray operator rule costs a phantom critique;
    dropping every rule on a format wobble costs the repo its enforcement."""
    line = (line or "").strip()
    if _OPERATOR_LABEL.match(line):
        return ""
    return _CODE_LABEL.sub("", line).strip()


def _distill(text):
    """Model-extracted hard rules, one per line, chunked so a small-context local model
    copes with a large CLAUDE.md. Falls back to the keyword heuristic when the gateway is
    unusable. A repo whose CLAUDE.md states no hard rules legitimately yields [].

    Only rules a judge can check FROM THE FINISHED WORK are extracted. A repo's CLAUDE.md
    also carries operator instructions phrased as hard imperatives ("restart the worker
    after changing any module it imports"), and the judge sees only the run's final text
    output — so such a rule can never be satisfied by any diff, and a judge holding it
    fails compliant work for it. Measured on this repo: at the old digest budget it was the
    top-ranked rule for an unrelated request and produced a FAIL on 4 of 5 runs, violating
    and compliant alike, citing it every time (`ConventionsNoiseTests`)."""
    chunks = [text[i:i + _CHUNK_CHARS] for i in range(0, len(text), _CHUNK_CHARS)]
    rules, failed = [], 0
    for chunk in chunks:
        try:
            reply = gateway.complete(
                "memory",
                "From this excerpt of a repository's CLAUDE.md (its working conventions), "
                "extract only the HARD RULES a judge can check BY READING THE FINISHED WORK — "
                "explicit constraints on the code itself (the 'never X', 'always Y', 'X must Z' "
                "statements about what the code may contain, call, import or write). Skip "
                "descriptions, architecture notes and anything advisory. Also skip any rule "
                "about what a PERSON must DO around the change — running, restarting, testing, "
                "deploying, committing or checking something — those leave no trace in the work "
                "and cannot be verified from it. One rule per line, short and self-contained, "
                "no bullets or numbering, each line PREFIXED with its kind:\n"
                "CODE: a constraint the finished work either satisfies or violates\n"
                "OPERATOR: an instruction to the person doing the work\n"
                "Examples:\nCODE: JSON writes must go through storage.mutate_json\n"
                "OPERATOR: restart the worker after changing a module\n"
                "OPERATOR: run the test suite before editing a prompt\n"
                "If the excerpt states no rules at all, reply exactly: NONE.\n\n" + chunk)
        except Exception:  # noqa: BLE001 - gateway (incl. Claude fallback) unusable
            failed += 1
            continue
        reply = (reply or "").strip()
        if not reply or reply.upper().startswith("NONE"):
            continue
        for line in reply.splitlines():
            line = line.strip().lstrip("-*•0123456789. ").strip()
            line = _judgeable(line)
            if line and line.upper() != "NONE":
                rules.append(line[:300])
    if failed == len(chunks):
        return _heuristic_rules(text)
    out, seen = [], set()
    for r in rules:
        low = r.lower()
        if low in seen:
            continue
        seen.add(low)
        out.append(r)
        if len(out) >= _MAX_RULES:
            break
    return out


def _keywords(text):
    """Significant words for overlap matching — mirrors `memory._keywords` (and through it
    `registry.Capability.score`), so a convention is ranked the way facts and caps already are."""
    text = re.sub(r"https?://\S+", " ", (text or "").lower())
    return {w for w in re.findall(r"[a-z]+", text) if len(w) > 3}


def _budget():
    """Chars of conventions one judging prompt may carry — a runtime setting, so a repo whose
    rules outgrow it is fixed from Admin rather than by a deploy.

    It is sized against the RULE CORPUS, not the model: at the old fixed 2_000 this repo put
    20 of its 224 rules in front of the judge, and what survived was whatever ranked top for
    the request rather than what the change actually touched — so the judge failed a compliant
    result 4 runs in 5, naming a rule the result could not have violated, while the real
    violation in the same output went unnamed (`ConventionsDigestBudgetTests`)."""
    return config.setting("conventions_digest_chars") or _DIGEST_CHARS


def select_rules(rules, request=None, budget=None):
    """Trim `rules` to what fits one judging prompt. Returns (kept, dropped_count).

    The ranking exists for the repo whose rules OUTGROW the budget — the bug it replaces took
    whichever rules appeared first in the document, making the enforced subset a function of
    section order rather than of the run. Ranked by keyword overlap with the request instead
    (same shape as `memory.recent_facts`), with the remaining slots filled in document order
    so an unrelated request still gets a stable, non-empty block.

    Dropping is no longer the normal case: `_budget()` is sized to hold a corpus this repo's
    size whole (224 rules, ~19.5k chars). A trim is the exception it degrades into, not the
    steady state it used to be.

    `dropped` is returned, never swallowed: a silently truncated list reads to the judge as
    "these were all the rules there were" — the same failure `mcp_client.Pool.trimmed` exists
    to prevent."""
    budget = _budget() if budget is None else budget
    rules = [r for r in (rules or []) if r]
    if not rules:
        return [], 0

    def _fill(ordered):
        kept, total = [], 0
        for _, i, r in ordered:
            if total + len(r) > budget:
                continue                     # a shorter later rule may still fit
            kept.append((i, r))
            total += len(r)
        return kept

    want = _keywords(request) if request else set()
    if want:
        # Stripping conversational filler on the QUERY side only, for the reason memory.py
        # documents: it keeps "should I ALWAYS run the tests" from matching on "always".
        try:
            import memory
            want -= memory._FACT_QUERY_STOP
        except Exception:  # noqa: BLE001 - ranking is best-effort, never a hard dependency
            pass
    scored = [(len(want & _keywords(r)) if want else 0, i, r) for i, r in enumerate(rules)]
    # strongest overlap first, then document order; ties are stable so two runs with the same
    # request select the same rules.
    scored.sort(key=lambda x: (-x[0], x[1]))
    kept = _fill(scored)
    kept.sort()                              # back into document order for a readable block
    return [r for _, r in kept], len(rules) - len(kept)


def digest(project_path):
    """The repo's COMPLETE hard-rule set (list of rule strings), recomputed only when a source
    CLAUDE.md changes. Distillation happens BEFORE the store mutation (the mutate fn must
    stay fast — no network inside the lock, same rule as knowledge.add_document).

    Cache the whole extraction, not a trimmed view of it: which rules a given run needs depends
    on the request, which isn't known at extraction time. `select_rules` trims per request."""
    if not project_path:
        return []
    text, fingerprint = _read_sources(project_path)
    if not text:
        return []
    cached = storage.read_json(_STORE, {}).get(project_path)
    if cached and cached.get("fingerprint") == fingerprint and cached.get("v") == _CACHE_V:
        return cached.get("rules", [])
    rules = _distill(text)

    def _update(store):
        store[project_path] = {"v": _CACHE_V, "fingerprint": fingerprint, "rules": rules}
        return store

    storage.mutate_json(_STORE, _update, {})
    return rules


def status(project_path):
    """What a repo's conventions look like right now, from the CACHE ONLY — never derives.

    `state` is one of: `absent` (no CLAUDE.md to read), `none` (never derived), `stale` (the
    file changed, or the entry predates `_CACHE_V` — it re-derives on the repo's next judged
    run), `fresh`. Surfacing this is the point: the rules the judge enforces used to be
    invisible, so nobody could tell what was being applied or that it had gone stale."""
    fp = _fingerprint(project_path)
    entry = storage.read_json(_STORE, {}).get(project_path) or {}
    rules = entry.get("rules") or []
    if not fp:
        state = "absent"
    elif not entry:
        state = "none"
    elif entry.get("fingerprint") != fp or entry.get("v") != _CACHE_V:
        state = "stale"
    else:
        state = "fresh"
    return {"path": project_path, "rules": rules, "count": len(rules), "state": state}


def refresh(project_path):
    """Force re-derivation now, dropping any cached entry first. The expensive path (one cheap-
    tier call per `_CHUNK_CHARS` of CLAUDE.md) — only ever reached from an explicit user action,
    never a page load. Derivation itself stays in `digest()`; this only invalidates."""
    def _drop(store):
        store.pop(project_path, None)
        return store

    storage.mutate_json(_STORE, _drop, {})
    trace("VERIFY", f"re-deriving repo conventions for {project_path}")
    digest(project_path)
    return status(project_path)


def judge_block(project_path, request=None):
    """The digest formatted for a judging prompt (verify / QA judge), with the precedence
    rule that makes it bite: the repo's conventions override the request itself — a
    faithfully-implemented violation is still a FAIL. None when the repo has no rules.

    `request` ranks which rules survive the prompt budget; without one the block falls back to
    document order. Any trim is declared in the block, so the judge cannot read a truncated
    list as the repo's complete rules and infer that an unlisted practice is allowed."""
    rules = digest(project_path)
    if not rules:
        return None
    kept, dropped = select_rules(rules, request)
    if not kept:
        return None
    if dropped:
        trace("VERIFY", f"repo conventions: {len(kept)} of {len(rules)} rules fit the prompt "
                        f"budget ({dropped} not shown)")
    block = (
        "REPO CONVENTIONS — hard rules from the target repository's own CLAUDE.md:\n"
        + "\n".join(f"- {r}" for r in kept)
    )
    if dropped:
        block += (
            f"\n\n(This is the {len(kept)} of {len(rules)} conventions most relevant to this "
            f"request — {dropped} more exist and are NOT shown. Treat the list as partial: never "
            "infer from a rule's absence that the repo permits something.)"
        )
    return block + (
        "\n\nThese conventions OVERRIDE the request: if the output does something a "
        "convention forbids, the verdict is FAIL even if the request explicitly asked for "
        "it or claimed it was acceptable — name the violated convention in the critique "
        "and state the compliant approach."
    )
