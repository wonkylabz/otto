# Versioning and releases

Otto is versioned with [semantic versioning](https://semver.org/spec/v2.0.0.html), read as a
contract with the **operator** rather than with a caller of an API — Otto is a service you
clone and run, not a library anyone imports.

## The single source of truth

`pyproject.toml`'s `[project].version`. Nothing else states a version:

- `config.VERSION` parses that file at import; `config.revision()` adds the short commit sha.
- `/api/health` serves both, and the UI paints `v0.1.0` beside the wordmark (sha in its title).
- `CHANGELOG.md` records what each version changed.
- the git tag `vX.Y.Z` is what an operator checks out.

`test_core.VersioningTests` fails if any of those disagree, or if a second version literal
appears in the tree. A service that misreports its own version is worse than one with none:
every later bug report is filed against the wrong build.

## What the number means

The public surface is everything an operator's install depends on: the `data/` stores, the
`OTTO_*` environment variables and runtime settings, the HTTP API, `install.sh`/`run.sh`, and
the ingress config files.

| Bump | When |
| --- | --- |
| **MAJOR** | the upgrade needs the operator to *do* something — a manual migration, a renamed or removed env var, setting or endpoint, a store an older Otto can no longer read, or a changed default that quietly alters how existing installs behave |
| **MINOR** | new surface, backwards compatible — a capability, an ingress, a setting, an endpoint; a schema migration that runs itself; a prompt change that changes what a run *does* |
| **PATCH** | fixes with no new surface — including prompt changes that repair a defect rather than move behaviour |

Otto is mostly prompts, which is why they appear twice: a prompt is behaviour, so "no code
changed" is never the reason a release is a patch.

While the major version is `0`, MINOR carries the breaks — the table's MAJOR row means MINOR
until `1.0.0`.

## Cutting a release

Write the changes under `## [Unreleased]` in `CHANGELOG.md` as you go — for an operator
upgrading a running install, in their vocabulary, not Otto's internals. Then:

```bash
python3 release.py patch          # or minor | major | 1.2.0
```

It refuses a dirty tree, a branch other than `main`, an existing tag and an empty Unreleased
section; then runs the suite, bumps `pyproject.toml`, dates the changelog section, fixes the
compare links, commits and tags. `--dry-run` prints the version it would cut.

Publishing is deliberately separate — a pushed tag is the irreversible half:

```bash
git push && git push origin vX.Y.Z
```

## Upgrading an install

```bash
git pull && ./install.sh --no-service   # deps + smoke tests
```

then restart the service (`docs/operating.md`). Confirm what is actually running from the
version beside the wordmark — the sha in its tooltip is the half the number cannot answer,
since Otto runs from a working checkout and every commit after a tag reports the same number.
