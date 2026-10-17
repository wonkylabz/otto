#!/usr/bin/env python3
"""Cut a release: bump the version, close the changelog section, open the release PR.

    python3 release.py patch|minor|major|X.Y.Z [--dry-run]

The version lives in exactly one place (`pyproject.toml`), the changelog in exactly one
(`CHANGELOG.md`), and the tag must agree with both — three files a human keeps in sync by
hand is three chances to ship a version that lies about itself. So this script is the release
process, and `docs/releasing.md` describes what it does rather than listing steps to retype.

It never tags. `main` takes changes through PRs and squash-merges them, so the commit this
script makes is never the one that lands — a local tag pointed at an orphan and left `main`
diverged (v0.4.0). `.github/workflows/release-tag.yml` tags the commit that DID land, once
the PR merges; merging is the operator's call that the release goes out."""
import datetime
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PYPROJECT = os.path.join(HERE, "pyproject.toml")
CHANGELOG = os.path.join(HERE, "CHANGELOG.md")
REPO_URL = "https://github.com/wonkylabz/otto"
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def sh(*args, check=True):
    return subprocess.run(args, cwd=HERE, capture_output=True, text=True, check=check).stdout.strip()


def die(msg):
    sys.exit(f"release: {msg}")


def current_version():
    m = re.search(r'^version = "([^"]+)"', open(PYPROJECT).read(), re.M)
    if not m:
        die(f"no [project].version in {PYPROJECT}")
    return m.group(1)


def bump(old, part):
    if SEMVER.match(part):
        return part
    major, minor, patch = (int(x) for x in old.split("."))
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    die(f"expected major|minor|patch or X.Y.Z, got {part!r}")


def unreleased_body(text):
    """What sits under `## [Unreleased]` — releasing nothing is the mistake worth catching."""
    m = re.search(r"^## \[Unreleased\]\n(.*?)(?=^## \[|\Z)", text, re.M | re.S)
    return (m.group(1) if m else "").strip()


def rewrite_changelog(text, old, new, today):
    """Close Unreleased into a dated section and re-point the compare links."""
    text = text.replace("## [Unreleased]\n", f"## [Unreleased]\n\n## [{new}] - {today}\n", 1)
    text = re.sub(r"^\[Unreleased\]: .*$", f"[Unreleased]: {REPO_URL}/compare/v{new}...HEAD",
                  text, count=1, flags=re.M)
    return re.sub(r"^(\[Unreleased\]: .*\n)",
                  rf"\1[{new}]: {REPO_URL}/compare/v{old}...v{new}\n", text, count=1, flags=re.M)


_RELEASED = re.compile(r"^## \[(\d+\.\d+\.\d+)\][^\n]*\n(.*?)(?=^## \[|^\[Unreleased\]:|\Z)",
                       re.M | re.S)


def released_sections(text):
    """{version: body} for every dated section — what each release's changelog SAID."""
    return {v: body.strip() for v, body in _RELEASED.findall(text)}


def rewritten_sections(text, shipped):
    """Released sections whose body differs from the copy their own tag shipped.

    A branch merged after a release can resolve its [Unreleased] entries INTO that release's
    section (git sees two adjacent hunks, not a heading), so the changelog then credits a tag
    with work it does not contain — 0.3.0 claimed onboarding. `shipped` maps a version to its
    tag's CHANGELOG.md, or None when no such tag exists here."""
    now = released_sections(text)
    return sorted(v for v, then in shipped.items()
                  if then is not None and v in now
                  and released_sections(then).get(v, now[v]) != now[v])


def _tagged_changelog(version):
    r = subprocess.run(["git", "show", f"v{version}:CHANGELOG.md"], cwd=HERE,
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def main(argv):
    dry = "--dry-run" in argv
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 1:
        die(__doc__.splitlines()[2].strip())

    old = current_version()
    new = bump(old, args[0])
    today = datetime.date.today().isoformat()
    tag = f"v{new}"

    branch = f"release/{tag}"

    # Every precondition first, so a failure leaves the tree untouched rather than half-bumped.
    if sh("git", "rev-parse", "--abbrev-ref", "HEAD") != "main":
        die("not on main")
    if sh("git", "status", "--porcelain"):
        die("working tree is dirty — commit or stash first")
    # A stale base cuts a version someone already released (0.3.0 shipped while this checkout
    # still read 0.2.0), and a base ahead of origin carries commits the PR would smuggle in.
    sh("git", "fetch", "--quiet", "--tags", "origin")
    if sh("git", "rev-parse", "HEAD") != sh("git", "rev-parse", "origin/main"):
        die("main is not origin/main — `git pull --ff-only` (or drop local commits) first")
    if sh("git", "tag", "--list", tag):
        die(f"tag {tag} already exists")
    if sh("git", "ls-remote", "--heads", "origin", branch):
        die(f"{branch} already exists on origin — is that release PR still open?")
    changelog = open(CHANGELOG).read()
    if not unreleased_body(changelog):
        die("CHANGELOG.md has an empty [Unreleased] section — nothing to release")
    bad = rewritten_sections(changelog, {v: _tagged_changelog(v)
                                         for v in released_sections(changelog)})
    if bad:
        die("CHANGELOG.md's released section(s) " + ", ".join(bad) + " differ from what their "
            "tag shipped — a later merge probably filed [Unreleased] entries there; move them "
            "back (`git diff v<X> -- CHANGELOG.md`)")

    print(f"release: {old} -> {new} ({tag}, {today})")
    if dry:
        return print("release: --dry-run, nothing written")

    # The suite is the gate, not a formality: it ratchets the docs and guards the version's
    # single source of truth, both of which this commit is about to move.
    print("release: running the suite…")
    venv = os.path.join(HERE, ".venv", "bin", "python")
    if subprocess.run([venv if os.path.exists(venv) else sys.executable, "-m", "unittest"],
                      cwd=HERE).returncode:
        die("tests failed")

    py = open(PYPROJECT).read()
    open(PYPROJECT, "w").write(py.replace(f'version = "{old}"', f'version = "{new}"', 1))
    open(CHANGELOG, "w").write(rewrite_changelog(changelog, old, new, today))

    # The commit goes on its own branch so local `main` never gets a commit origin won't have.
    sh("git", "switch", "--quiet", "-c", branch)
    sh("git", "add", "pyproject.toml", "CHANGELOG.md")
    sh("git", "commit", "-m", f"Release {tag}")
    sh("git", "push", "--quiet", "-u", "origin", branch)
    body = (f"Cuts {tag}: bumps `pyproject.toml` and dates the changelog section.\n\n"
            f"Merging tags the commit that lands on `main` as `{tag}` "
            "(`.github/workflows/release-tag.yml`).")
    pr = subprocess.run(["gh", "pr", "create", "--base", "main", "--head", branch,
                         "--title", f"Release {tag}", "--body", body],
                        cwd=HERE, capture_output=True, text=True)
    sh("git", "switch", "--quiet", "main")
    if pr.returncode:
        die(f"pushed {branch} but could not open the PR ({pr.stderr.strip()}) — open it by hand")
    print(f"release: opened {pr.stdout.strip()}\n"
          f"release: merging it tags {tag}; then `git pull` here")


if __name__ == "__main__":
    main(sys.argv[1:])
