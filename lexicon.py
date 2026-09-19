"""The ONE keyword tokenizer.

Five modules used to carry their own copy — `registry._rank_tokens`, `memory._keywords`,
`knowledge._keywords`, `conventions._keywords`, `mcp_client._words` — and every one of the five
docstrings claimed to mirror the others. They had already drifted on three axes nobody chose:
the character class, the length threshold, and the return type. A mirror maintained by prose is
not a mirror.

So the shared part is here and the differences are ARGUMENTS — visible at the call site, and
impossible to diverge by accident. Each caller keeps exactly the behaviour it had, because
aligning them is a RANKING change, not a refactor: measured over 316 real requests, facts and
rules, folding digits into words moved 168 of them. It trades `python` matching `python3` for
`projectv2` staying whole, and it lets a run id ("b2099290") and a timestamp ("15t01") score as
vocabulary — which the corpora behind `digits=False` are full of. Worth doing deliberately, with
its own measurement; not worth smuggling into a de-duplication commit.

URL stripping is unconditional and comes first: a pasted link's path segments (a CI build URL's
"buildConfiguration/.../Infrastructure") are topic nouns that were never in the request, and they
skew every consumer — the routing shortlist toward topic-matching read caps, and MCP tool
selection toward whatever server shares a word with the hostname.
"""
import re

_URL = re.compile(r"https?://\S+")
_WORD = re.compile(r"[a-z0-9]+")
_ALPHA = re.compile(r"[a-z]+")


def tokens(text, *, min_len=4, stop=frozenset(), digits=True, as_set=True):
    """Significant tokens of `text`, lowercased, URLs stripped.

    `min_len` is inclusive: 3 keeps "api" and drops "pr". `stop` is the caller's filler list.
    `digits=False` breaks a word at its digits, so "python3" tokenizes as "python". `as_set=False`
    preserves order and repeats, which only `registry.rank` wants — it counts term frequency, and
    a set would silently turn its IDF into a presence test."""
    text = _URL.sub(" ", (text or "").lower())
    pattern = _WORD if digits else _ALPHA
    words = [w for w in pattern.findall(text) if len(w) >= min_len and w not in stop]
    return set(words) if as_set else words
