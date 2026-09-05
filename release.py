#!/usr/bin/env python3
"""Cut a release: bump the version, close the changelog section, commit, tag.

    python3 release.py patch|minor|major|X.Y.Z [--dry-run]

The version lives in exactly one place (`pyproject.toml`), the changelog in exactly one
(`CHANGELOG.md`), and the tag must agree with both — three files a human keeps in sync by
hand is three chances to ship a version that lies about itself. So this script is the release
process, and `docs/releasing.md` describes what it does rather than listing steps to retype.

It deliberately stops before `git push`: pushing a tag is the irreversible half, and it is the
operator's call whether the release goes out now."""
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


def main(argv):
    dry = "--dry-run" in argv
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 1:
        die(__doc__.splitlines()[2].strip())

    old = current_version()
    new = bump(old, args[0])
    today = datetime.date.today().isoformat()
    tag = f"v{new}"

    # Every precondition first, so a failure leaves the tree untouched rather than half-bumped.
    if sh("git", "rev-parse", "--abbrev-ref", "HEAD") != "main":
        die("not on main")
    if sh("git", "status", "--porcelain"):
        die("working tree is dirty — commit or stash first")
    if sh("git", "tag", "--list", tag):
        die(f"tag {tag} already exists")
    changelog = open(CHANGELOG).read()
    if not unreleased_body(changelog):
        die("CHANGELOG.md has an empty [Unreleased] section — nothing to release")

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

    sh("git", "add", "pyproject.toml", "CHANGELOG.md")
    sh("git", "commit", "-m", f"release {tag}")
    sh("git", "tag", "-a", tag, "-m", f"Otto {tag}")
    print(f"release: committed and tagged {tag}\n"
          f"release: review with `git show {tag}`, then publish:\n"
          f"           git push && git push origin {tag}")


if __name__ == "__main__":
    main(sys.argv[1:])
