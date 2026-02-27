#!/usr/bin/env python3
"""
Gitea — Protect Default Branches (Disable Force Push)

Adds a branch protection rule to every repository's default branch,
disabling force push.  If a protection rule already exists for the
default branch, it will be patched to ensure force push is off.

Usage:
  # Protect all repos (dry-run first)
  python3 protect_default_branches.py --dry-run

  # Protect all repos for real
  python3 protect_default_branches.py

  # Only process repos owned by specific owners
  python3 protect_default_branches.py --owner myorg --owner myuser

  # Skip repos that already have any branch protection on the default branch
  python3 protect_default_branches.py --skip-existing

  # Force overwrite existing protection rules (delete + recreate)
  python3 protect_default_branches.py --force-overwrite
"""

import argparse
import os
import sys
from typing import List, Optional

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
    return {
        "Authorization": f"token {GITEA_TOKEN}",
        "Content-Type": "application/json",
    }


def api_url(path: str) -> str:
    return f"{GITEA_URL}/api/v1{path}"


def paginated_get(url: str, params: Optional[dict] = None) -> list:
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
        body = resp.json()
        # Gitea 1.20+ wraps search results in {"data": [...], "ok": true}
        if isinstance(body, dict) and "data" in body:
            batch = body["data"]
        elif isinstance(body, list):
            batch = body
        else:
            break
        if not batch:
            break
        results.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    return results


def get_all_repos(owners: Optional[List[str]] = None) -> list:
    """Fetch all repositories visible to the token user."""
    repos = paginated_get(api_url("/repos/search"))
    if owners:
        owners_lower = {o.lower() for o in owners}
        repos = [r for r in repos if r["owner"]["login"].lower() in owners_lower]
    return repos


def get_branch_protections(owner: str, repo: str) -> list:
    url = api_url(f"/repos/{owner}/{repo}/branch_protections")
    resp = requests.get(url, headers=auth_headers())
    if not resp.ok:
        print_error(f"  Failed to list branch protections for {owner}/{repo}: {resp.status_code}")
        return []
    return resp.json()


def find_protection_for_branch(protections: list, branch_name: str) -> Optional[dict]:
    for bp in protections:
        bp_name = bp.get("branch_name") or bp.get("rule_name") or ""
        if bp_name == branch_name:
            return bp
    return None


def is_force_push_disabled(bp: dict) -> bool:
    """Check if force push is already disabled in an existing protection rule."""
    return not bp.get("enable_force_push", False)


def build_protection_payload(branch_name: str) -> dict:
    """Build the branch protection creation payload.

    Works across Gitea versions: older versions ignore unknown fields,
    newer versions (1.23+) honour enable_force_push.
    """
    return {
        "branch_name": branch_name,
        "enable_push": False,
        "enable_force_push": False,
        "enable_push_whitelist": False,
        "enable_merge_whitelist": False,
        "enable_status_check": False,
        "enable_approvals_whitelist": False,
        "block_on_rejected_reviews": False,
        "block_on_outdated_branch": False,
        "dismiss_stale_approvals": False,
        "require_signed_commits": False,
        "protected_file_patterns": "",
        "block_on_official_review_requests": False,
    }


def create_branch_protection(owner: str, repo: str, branch_name: str, dry_run: bool) -> bool:
    payload = build_protection_payload(branch_name)
    if dry_run:
        print_info(f"  [DRY-RUN] Would CREATE branch protection for '{branch_name}'")
        return True

    url = api_url(f"/repos/{owner}/{repo}/branch_protections")
    resp = requests.post(url, headers=auth_headers(), json=payload)
    if resp.status_code == 201:
        print_success(f"  Created branch protection for '{branch_name}'")
        return True
    elif resp.status_code == 422:
        print_warning(f"  Protection for '{branch_name}' already exists (422), attempting patch...")
        return patch_force_push_off(owner, repo, branch_name, dry_run)
    else:
        print_error(f"  Failed to create protection: {resp.status_code} {resp.text[:300]}")
        return False


def delete_branch_protection(owner: str, repo: str, branch_name: str, dry_run: bool) -> bool:
    if dry_run:
        print_info(f"  [DRY-RUN] Would DELETE branch protection for '{branch_name}'")
        return True

    url = api_url(f"/repos/{owner}/{repo}/branch_protections/{branch_name}")
    resp = requests.delete(url, headers=auth_headers())
    if resp.ok:
        print_success(f"  Deleted branch protection for '{branch_name}'")
        return True
    else:
        print_error(f"  Failed to delete protection: {resp.status_code} {resp.text[:300]}")
        return False


def patch_force_push_off(owner: str, repo: str, branch_name: str, dry_run: bool) -> bool:
    if dry_run:
        print_info(f"  [DRY-RUN] Would PATCH branch protection for '{branch_name}' to disable force push")
        return True

    url = api_url(f"/repos/{owner}/{repo}/branch_protections/{branch_name}")
    payload = {"enable_force_push": False}
    resp = requests.patch(url, headers=auth_headers(), json=payload)
    if resp.ok:
        print_success(f"  Patched branch protection for '{branch_name}': force push disabled")
        return True
    else:
        print_error(f"  Failed to patch protection: {resp.status_code} {resp.text[:300]}")
        return False


def process_repo(repo: dict, dry_run: bool, skip_existing: bool, force_overwrite: bool) -> bool:
    full_name = repo["full_name"]
    owner = repo["owner"]["login"]
    name = repo["name"]
    default_branch = repo.get("default_branch", "main")
    empty = repo.get("empty", False)
    archived = repo.get("archived", False)
    mirror = repo.get("mirror", False)

    if empty:
        print_warning(f"[SKIP] {full_name} — empty repository")
        return True
    if archived:
        print_warning(f"[SKIP] {full_name} — archived")
        return True

    print_info(f"[REPO] {full_name} (default: {default_branch}, mirror: {mirror})")

    protections = get_branch_protections(owner, name)
    existing = find_protection_for_branch(protections, default_branch)

    if existing:
        if force_overwrite:
            print_warning(f"  Protection exists, force overwriting (--force-overwrite)...")
            ok = delete_branch_protection(owner, name, default_branch, dry_run)
            if not ok:
                return False
            return create_branch_protection(owner, name, default_branch, dry_run)
        if skip_existing:
            print_warning(f"  Already protected, skipping (--skip-existing)")
            return True
        if is_force_push_disabled(existing):
            print_success(f"  Already protected with force push disabled — nothing to do")
            return True
        print_warning(f"  Protection exists but force push is enabled, patching...")
        return patch_force_push_off(owner, name, default_branch, dry_run)
    else:
        return create_branch_protection(owner, name, default_branch, dry_run)


def main():
    parser = argparse.ArgumentParser(
        description="Add branch protection (disable force push) to all Gitea repos' default branches."
    )
    parser.add_argument(
        "--owner", action="append", default=None,
        help="Only process repos owned by this user/org (repeatable)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be done without making changes",
    )
    parser.add_argument(
        "--skip-existing", action="store_true",
        help="Skip repos that already have a protection rule for the default branch",
    )
    parser.add_argument(
        "--force-overwrite", action="store_true",
        help="Delete existing protection rule and recreate it (fully overwrite)",
    )
    args = parser.parse_args()

    if args.skip_existing and args.force_overwrite:
        print_error("--skip-existing and --force-overwrite are mutually exclusive.")
        sys.exit(1)

    if not GITEA_TOKEN:
        print_error("GITEA_TOKEN is not set. Please configure it in .env or environment.")
        sys.exit(1)

    print_info(f"Gitea URL       : {GITEA_URL}")
    print_info(f"Dry run         : {args.dry_run}")
    print_info(f"Force overwrite : {args.force_overwrite}")
    if args.owner:
        print_info(f"Owners          : {', '.join(args.owner)}")
    print()

    # Verify connectivity
    resp = requests.get(api_url("/version"), headers=auth_headers())
    if not resp.ok:
        print_error(f"Cannot connect to Gitea API: {resp.status_code}")
        sys.exit(1)
    version = resp.json().get("version", "unknown")
    print_info(f"Gitea version: {version}")
    print()

    repos = get_all_repos(args.owner)
    print_info(f"Found {len(repos)} repositories\n")

    success_count = 0
    fail_count = 0
    skip_count = 0

    for repo in sorted(repos, key=lambda r: r["full_name"]):
        if repo.get("empty") or repo.get("archived"):
            skip_count += 1
            process_repo(repo, args.dry_run, args.skip_existing, args.force_overwrite)
            continue

        ok = process_repo(repo, args.dry_run, args.skip_existing, args.force_overwrite)
        if ok:
            success_count += 1
        else:
            fail_count += 1

    print()
    print_info("=" * 50)
    print_success(f"  Success : {success_count}")
    print_warning(f"  Skipped : {skip_count}")
    if fail_count:
        print_error(f"  Failed  : {fail_count}")
    else:
        print_info(f"  Failed  : {fail_count}")
    print_info("=" * 50)

    if fail_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
