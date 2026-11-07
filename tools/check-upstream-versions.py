#!/usr/bin/env python3
"""Check installed build components against their newest upstream release.

Runs on the host that carries the binaries (it needs both the local version and
the target's network access). Reads a declarative catalog, resolves the newest
upstream tag (or HEAD for commit-pinned checkouts), compares with the version
installed on the host, and prints a table.

Exit codes:
  0  everything current
  1  at least one component is behind upstream
  2  at least one check could not be performed (repo unreachable / version
     unreadable) and nothing is behind

Usage:
  python3 tools/check-upstream-versions.py
  python3 tools/check-upstream-versions.py --catalog tools/upstream-versions.yml
  python3 tools/check-upstream-versions.py --json
  python3 tools/check-upstream-versions.py --only batctl,knot-resolver
  python3 tools/check-upstream-versions.py --no-network   # offline: errors only
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

# The catalog is looked up next to the script first (repo checkout or the
# installed /usr/local/bin copy), then at the installed shared path. The
# deployment puts script and catalog in different directories, so a default
# derived only from the script's directory would not find it.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CATALOG_CANDIDATES = [
    os.environ.get("UPSTREAM_VERSIONS_CATALOG", ""),
    os.path.join(_SCRIPT_DIR, "upstream-versions.yml"),
    "/usr/local/share/check-upstream-versions.yml",
    "/etc/check-upstream-versions.yml",
]
DEFAULT_CATALOG = next((c for c in CATALOG_CANDIDATES
                        if c and os.path.exists(c)), CATALOG_CANDIDATES[1])


def run(cmd: list[str] | str, timeout: int = 30) -> tuple[int, str]:
    shell = isinstance(cmd, str)
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    # Checkouts on the gateway are root-owned; running as an unprivileged user
    # must not trip git's dubious-ownership refusal.
    if not shell and cmd and cmd[0] == "git":
        cmd = ["git", "-c", "safe.directory=*"] + cmd[1:]
    try:
        p = subprocess.run(cmd, shell=shell, capture_output=True, text=True,
                           timeout=timeout, env=env)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except OSError as exc:
        return 127, str(exc)


def version_key(text: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", text)
    return tuple(int(n) for n in nums)


def upstream_tags(repo: str, tag_regex: str) -> list[str]:
    rc, out = run(["git", "ls-remote", "--tags", "--refs", repo])
    if rc != 0:
        raise RuntimeError(f"git ls-remote failed for {repo}: {out}")
    tags = []
    for line in out.splitlines():
        if "refs/tags/" in line:
            tag = line.split("refs/tags/", 1)[1].strip()
            if re.match(tag_regex, tag):
                tags.append(tag)
    return tags


def newest_tag(repo: str, tag_regex: str) -> str:
    tags = upstream_tags(repo, tag_regex)
    if not tags:
        raise RuntimeError(f"no tags match {tag_regex} in {repo}")
    return max(tags, key=version_key)


def upstream_head(repo: str) -> str:
    rc, out = run(["git", "ls-remote", repo, "HEAD"])
    if rc != 0:
        raise RuntimeError(f"git ls-remote failed for {repo}: {out}")
    for line in out.splitlines():
        if line.endswith("HEAD"):
            return line.split()[0]
    raise RuntimeError(f"no HEAD found in {repo}")


def installed_version(spec: dict[str, Any]) -> str:
    if "file" in spec:
        try:
            with open(spec["file"], encoding="utf-8", errors="replace") as fh:
                return fh.read().strip()
        except OSError as exc:
            raise RuntimeError(f"cannot read {spec['file']}: {exc}")
    if "git_dir" in spec:
        rc, out = run(["git", "-C", spec["git_dir"], "rev-parse", "HEAD"])
        if rc != 0:
            raise RuntimeError(f"cannot read commit in {spec['git_dir']}: {out}")
        return out.splitlines()[0].strip()
    if "command" in spec:
        rc, out = run(spec["command"])
        if rc != 0:
            raise RuntimeError(f"command failed: {spec['command']}: {out}")
        pattern = spec.get("regex")
        if pattern:
            match = re.search(pattern, out)
            if not match:
                raise RuntimeError(
                    f"version regex {pattern!r} did not match output: {out!r}")
            return match.group(1).strip()
        return out.strip()
    raise RuntimeError("installed spec has no command/file/git_dir")


def check(entry: dict[str, Any], offline: bool) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": entry["name"],
        "kind": entry.get("kind", "tag"),
        "repo": entry.get("repo", ""),
        "installed": None,
        "upstream": None,
        "status": "unknown",
        "policy": entry.get("policy", "latest"),
        "note": "",
    }
    try:
        result["installed"] = installed_version(entry.get("installed", {}))
    except RuntimeError as exc:
        result["note"] = str(exc)
        return result

    if offline:
        result["note"] = "network checks skipped (--no-network)"
        return result

    try:
        if result["kind"] == "commit":
            result["upstream"] = upstream_head(entry["repo"])
            if result["installed"] == result["upstream"]:
                result["status"] = "current"
            else:
                result["status"] = "differs"
        else:
            # LTS entries are compared against the newest tag inside the LTS
            # series (e.g. Kea 3.0.x), not against the newest release overall.
            if result["policy"] == "lts":
                tag_regex = entry["lts_tag_regex"]
            else:
                tag_regex = entry["tag_regex"]
            result["upstream"] = newest_tag(entry["repo"], tag_regex)
            a, b = version_key(result["installed"]), version_key(result["upstream"])
            if a == b:
                result["status"] = "current"
            elif a < b:
                result["status"] = "outdated"
            else:
                result["status"] = "ahead (unreleased build?)"
    except RuntimeError as exc:
        result["note"] = str(exc)
    return result


def short_commit(sha: str | None) -> str:
    return (sha or "")[:12]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--catalog", default=DEFAULT_CATALOG,
                    help="component catalog (default: next to the script, else "
                         "/usr/local/share/check-upstream-versions.yml)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--only", default="", help="comma-separated component names")
    ap.add_argument("--no-network", action="store_true",
                    help="only resolve installed versions")
    args = ap.parse_args()

    try:
        import yaml
    except ImportError:
        print("PyYAML is required (apt: python3-yaml)", file=sys.stderr)
        return 2

    with open(args.catalog, encoding="utf-8") as fh:
        catalog = yaml.safe_load(fh)

    entries = catalog.get("components", [])
    if args.only:
        wanted = {n.strip() for n in args.only.split(",") if n.strip()}
        entries = [e for e in entries if e["name"] in wanted]
    if not entries:
        print("no components selected", file=sys.stderr)
        return 2

    results = [check(e, args.no_network) for e in entries]

    outdated = [r for r in results if r["status"] == "outdated"]
    differing = [r for r in results if r["status"] == "differs"]
    errors = [r for r in results if r["status"] == "unknown"]

    if args.json:
        # --json must be pure JSON: the wrapper pipes it into jq and caches it
        # as state.json. Printing the human summary here made the cached file
        # unparseable ("all 0 components current" in the motd).
        json.dump({
            "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "components": results,
            "counts": {
                "total": len(results),
                "current": sum(1 for r in results if r["status"] == "current"),
                "outdated": len(outdated),
                "differs": len(differing),
                "unknown": len(errors),
            },
        }, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        hdr = f"{'component':<34} {'installed':<16} {'upstream':<16} status"
        print(hdr)
        print("-" * len(hdr))
        for r in results:
            inst = short_commit(r["installed"]) if r["kind"] == "commit" else (r["installed"] or "?")
            up = short_commit(r["upstream"]) if r["kind"] == "commit" else (r["upstream"] or "?")
            print(f"{r['name']:<34} {inst:<16} {up:<16} {r['status']}")
            if r["note"]:
                print(f"{'':<34} note: {r['note']}")

        if outdated:
            print(f"\n{len(outdated)} component(s) behind upstream: "
                  + ", ".join(r["name"] for r in outdated))
        if differing:
            print(f"{len(differing)} checkout(s) differ from upstream HEAD: "
                  + ", ".join(r["name"] for r in differing))
        if errors:
            print(f"{len(errors)} check(s) could not be completed: "
                  + ", ".join(r["name"] for r in errors))
        if not (outdated or differing or errors):
            print("\nall components current")

    if outdated:
        return 1
    if errors:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
