#!/usr/bin/env python3
"""
Gitea — Link Packages to Repositories

Automatically links each package under an owner (org or user) to the
repository whose name matches the package name.

Matching strategies (tried in order):
  1. Exact match: package name == repo name
  2. Case-insensitive match
  3. Normalized match: dots/underscores → hyphens (e.g. my_lib → my-lib)

Usage:
  # Link packages for a specific owner
  python3 link_packages.py --owner myorg

  # Link packages for multiple owners
  python3 link_packages.py --owner org1 --owner org2

  # Dry run (show what would be linked without making changes)
  python3 link_packages.py --owner myorg --dry-run

  # Filter by package type
  python3 link_packages.py --owner myorg --type generic --type npm
"""

import argparse
import os
import re
import sys
from typing import Dict, List, Optional, Set, Tuple

import requests
import dotenv

dotenv.load_dotenv()

GITEA_URL = os.getenv("GITEA_URL", "https://gitea.example.com").rstrip("/")
GITEA_TOKEN = os.getenv("GITEA_TOKEN", "")


class bcolors:
    HEADER = "\033[95m"
    OKBLUE = "\033[94m"
    OKGREEN = "\033[92m"
    WARNING = "\033[93m"
    FAIL = "\033[91m"
    ENDC = "\033[0m"
    BOLD = "\033[1m"


def print_info(msg: str):
    print(f"{bcolors.OKBLUE}{msg}{bcolors.ENDC}")


def print_success(msg: str):
    print(f"{bcolors.OKGREEN}{msg}{bcolors.ENDC}")


def print_warning(msg: str):
    print(f"{bcolors.WARNING}{msg}{bcolors.ENDC}")


def print_error(msg: str):
    print(f"{bcolors.FAIL}{msg}{bcolors.ENDC}")


def auth_headers() -> dict:
    return {"Authorization": f"token {GITEA_TOKEN}"}


def api_url(path: str) -> str:
    return f"{GITEA_URL}/api/v1{path}"


def normalize_name(name: str) -> str:
    """Normalize a package/repo name for fuzzy matching."""
    return re.sub(r"[._]", "-", name).lower().strip("-")


def paginated_get(url: str, params: Optional[dict] = None) -> list:
    """Fetch all pages from a paginated Gitea API endpoint."""
    results = []
    page = 1
    per_page = 50
    while True:
        p = {"page": page, "limit": per_page}
        if params:
            p.update(params)
        resp = requests.get(url, headers=auth_headers(), params=p)
        if not resp.ok:
            print_error(f"API error: GET {url} → {resp.status_code} {resp.text[:300]}")
            break
        batch = resp.json()
        if not batch:
            break
        results.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    return results


def list_packages(owner: str, pkg_type: Optional[str] = None) -> list:
    """List all packages for an owner, optionally filtered by type."""
    params = {}
    if pkg_type:
        params["type"] = pkg_type
    return paginated_get(api_url(f"/packages/{owner}"), params)


def list_repos(owner: str) -> list:
    """List all repositories for an owner (works for both orgs and users)."""
    repos = paginated_get(api_url(f"/orgs/{owner}/repos"))
    if repos:
        return repos
    return paginated_get(api_url(f"/users/{owner}/repos"))


def link_package(owner: str, pkg_type: str, pkg_name: str, repo_name: str) -> Tuple[bool, str]:
    """Link a package to a repository. Returns (success, message)."""
    url = api_url(f"/packages/{owner}/{pkg_type}/{pkg_name}/-/link/{repo_name}")
    resp = requests.post(url, headers=auth_headers())
    if resp.status_code in (200, 201):
        return True, "linked"
    if resp.status_code == 404:
        return False, "not found (404)"
    return False, f"{resp.status_code} {resp.text[:200]}"


def deduplicate_packages(packages: list) -> list:
    """
    The list packages API returns one entry per version.
    We only need one entry per (type, name) since linking is per-package.
    """
    seen: Set[Tuple[str, str]] = set()
    unique = []
    for pkg in packages:
        key = (pkg.get("type", ""), pkg.get("name", ""))
        if key not in seen:
            seen.add(key)
            unique.append(pkg)
    return unique


def build_repo_lookup(repos: list) -> Dict[str, str]:
    """
    Build lookup tables for matching:
      normalized_name -> original repo name
    """
    lookup: Dict[str, str] = {}
    for repo in repos:
        name = repo.get("name", "")
        if not name:
            continue
        norm = normalize_name(name)
        if norm not in lookup:
            lookup[norm] = name
    return lookup


def find_matching_repo(pkg_name: str, repo_names: Dict[str, str], exact_names: Dict[str, str]) -> Optional[str]:
    """
    Try to match a package name to a repository name.
    Returns the repo name if found, else None.
    """
    if pkg_name in exact_names:
        return exact_names[pkg_name]

    lower = pkg_name.lower()
    if lower in exact_names:
        return exact_names[lower]

    norm = normalize_name(pkg_name)
    if norm in repo_names:
        return repo_names[norm]

    return None


def process_owner(owner: str, type_filters: List[str], dry_run: bool) -> Tuple[int, int, int]:
    """
    Process all packages for a single owner.
    Returns (linked_count, skipped_count, error_count).
    """
    print(f"\n{'='*60}")
    print(f"{bcolors.HEADER}Owner: {owner}{bcolors.ENDC}")
    print(f"{'='*60}")

    print_info("Fetching repositories...")
    repos = list_repos(owner)
    if not repos:
        print_warning(f"No repositories found for {owner}")
        return 0, 0, 0

    exact_names: Dict[str, str] = {}
    for repo in repos:
        name = repo.get("name", "")
        if name:
            exact_names[name] = name
            exact_names[name.lower()] = name
    norm_lookup = build_repo_lookup(repos)

    print_info(f"Found {len(repos)} repository(ies)")

    all_packages = []
    if type_filters:
        for t in type_filters:
            pkgs = list_packages(owner, t)
            all_packages.extend(pkgs)
    else:
        all_packages = list_packages(owner)

    packages = deduplicate_packages(all_packages)
    if not packages:
        print_warning(f"No packages found for {owner}")
        return 0, 0, 0

    print_info(f"Found {len(packages)} unique package(s) ({len(all_packages)} version(s) total)")

    linked = 0
    skipped = 0
    errors = 0

    for pkg in packages:
        pkg_name = pkg.get("name", "")
        pkg_type = pkg.get("type", "")
        repo_info = pkg.get("repository")

        if repo_info:
            print(f"  [{pkg_type}] {pkg_name} — already linked to {repo_info.get('full_name', '?')}, skipping")
            skipped += 1
            continue

        matched_repo = find_matching_repo(pkg_name, norm_lookup, exact_names)

        if not matched_repo:
            print_warning(f"  [{pkg_type}] {pkg_name} — no matching repository found")
            skipped += 1
            continue

        if dry_run:
            print_info(f"  [{pkg_type}] {pkg_name} → {owner}/{matched_repo} (dry run)")
            linked += 1
            continue

        ok, msg = link_package(owner, pkg_type, pkg_name, matched_repo)
        if ok:
            print_success(f"  [{pkg_type}] {pkg_name} → {owner}/{matched_repo} ✓")
            linked += 1
        else:
            print_error(f"  [{pkg_type}] {pkg_name} → {owner}/{matched_repo} — {msg}")
            errors += 1

    return linked, skipped, errors


def list_all_owners() -> List[str]:
    """List all orgs + the authenticated user as potential owners."""
    owners = []
    orgs = paginated_get(api_url("/admin/orgs"))
    for org in orgs:
        name = org.get("username") or org.get("name", "")
        if name:
            owners.append(name)
    return owners


def main():
    parser = argparse.ArgumentParser(
        description="Link Gitea packages to their matching repositories"
    )
    parser.add_argument(
        "--owner", action="append", default=[],
        help="Owner (org or user) to process. Can be specified multiple times. "
             "If omitted, all orgs are processed."
    )
    parser.add_argument(
        "--type", dest="types", action="append", default=[],
        help="Only process packages of this type (e.g. generic, npm, maven). "
             "Can be specified multiple times. Default: all types."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be linked without making changes"
    )
    args = parser.parse_args()

    print(f"{bcolors.HEADER}---=== Gitea: Link Packages to Repositories ===---{bcolors.ENDC}")
    print(f"Gitea URL: {GITEA_URL}")
    if args.dry_run:
        print_warning("DRY RUN — no changes will be made")
    print()

    if not GITEA_TOKEN:
        print_error("GITEA_TOKEN is not set")
        sys.exit(1)

    resp = requests.get(api_url("/version"), headers=auth_headers())
    if not resp.ok:
        print_error(f"Cannot connect to Gitea: {resp.status_code}")
        sys.exit(1)
    print_info(f"Connected to Gitea (version: {resp.json().get('version', '?')})")

    owners = args.owner
    if not owners:
        print_info("No --owner specified, discovering all organizations...")
        owners = list_all_owners()
        if not owners:
            print_warning("No organizations found. Use --owner to specify an owner.")
            sys.exit(0)
        print_info(f"Found {len(owners)} organization(s): {', '.join(owners)}")

    total_linked = 0
    total_skipped = 0
    total_errors = 0

    for owner in owners:
        l, s, e = process_owner(owner, args.types, args.dry_run)
        total_linked += l
        total_skipped += s
        total_errors += e

    print(f"\n{'='*60}")
    action_word = "Would link" if args.dry_run else "Linked"
    print(f"{action_word}: {total_linked}  |  Skipped: {total_skipped}  |  Errors: {total_errors}")
    if total_errors == 0:
        print_success("Done!")
    else:
        print_error(f"Finished with {total_errors} error(s)")
        sys.exit(1)


if __name__ == "__main__":
    main()
