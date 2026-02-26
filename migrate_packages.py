#!/usr/bin/env python3
"""
GitLab to Gitea — Package & Container Image Migration

Supports migrating:
  - Generic packages
  - Maven packages
  - npm packages
  - NuGet packages
  - PyPI packages
  - Container images (requires skopeo)

Usage:
  python3 migrate_packages.py
"""

import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from typing import List, Optional
from urllib.parse import quote as urlquote

import gitlab
import gitlab.v4.objects
import requests
import dotenv

dotenv.load_dotenv()

SCRIPT_VERSION = "1.0"

#######################
# CONFIG
#######################

GITLAB_URL = os.getenv("GITLAB_URL", "https://gitlab.source.com")
GITLAB_TOKEN = os.getenv("GITLAB_TOKEN", "")
GITLAB_ADMIN_USER = os.getenv("GITLAB_ADMIN_USER", "")
GITLAB_ADMIN_PASS = os.getenv("GITLAB_ADMIN_PASS", "")

GITEA_URL = os.getenv("GITEA_URL", "https://gitea.dest.com")
GITEA_TOKEN = os.getenv("GITEA_TOKEN", "")

MIGRATE_PACKAGES = os.getenv("MIGRATE_PACKAGES", "1") == "1"
MIGRATE_CONTAINERS = os.getenv("MIGRATE_CONTAINERS", "1") == "1"

PACKAGE_TYPES_TO_MIGRATE = os.getenv(
    "PACKAGE_TYPES_TO_MIGRATE", "generic,maven,npm,nuget,pypi"
).split(",")

TMP_DIR = os.getenv("MIGRATE_TMP_DIR", "/tmp/gitlab_to_gitea_packages")

# GitLab container registry URL (often differs from the main URL)
# e.g. registry.gitlab.example.com  or  gitlab.example.com:5050
GITLAB_REGISTRY_HOST = os.getenv("GITLAB_REGISTRY_HOST", "")

# Gitea container registry host (usually same as GITEA_URL host)
GITEA_REGISTRY_HOST = os.getenv("GITEA_REGISTRY_HOST", "")

GLOBAL_ERROR_COUNT = 0

#######################
# Logging helpers
#######################

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
    global GLOBAL_ERROR_COUNT
    GLOBAL_ERROR_COUNT += 1
    print(f"{bcolors.FAIL}{msg}{bcolors.ENDC}")


def name_clean(name: str) -> str:
    n = name.replace(" ", "")
    for src, dst in [("ä", "ae"), ("ö", "oe"), ("ü", "ue"),
                     ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue")]:
        n = n.replace(src, dst)
    n = re.sub(r"[^a-zA-Z0-9_\.\-]", "-", n)
    if n.lower() == "plugins":
        return n + "-user"
    return n


#######################
# GitLab helpers
#######################

def gitlab_api_get(path: str, stream: bool = False) -> requests.Response:
    url = f"{GITLAB_URL}/api/v4{path}"
    return requests.get(url, headers={"PRIVATE-TOKEN": GITLAB_TOKEN}, stream=stream)


def gitlab_download_generic(project_id: int, pkg_name: str, pkg_version: str, file_name: str) -> Optional[bytes]:
    resp = gitlab_api_get(
        f"/projects/{project_id}/packages/generic/{urlquote(pkg_name, safe='')}/{urlquote(pkg_version, safe='')}/{urlquote(file_name, safe='')}"
    )
    if resp.ok:
        return resp.content
    print_error(f"Failed to download generic package file {file_name}: {resp.status_code} {resp.text[:200]}")
    return None


def gitlab_download_maven(project_id: int, pkg_name: str, pkg_version: str, file_name: str) -> Optional[bytes]:
    """Maven path: groupId/artifactId/version/filename  (groupId dots -> slashes)"""
    # pkg_name is like "com.example/my-artifact" or "com/example/my-artifact"
    # In GitLab, maven package name is stored as "groupId/artifactId" with dots in groupId
    # The download path needs dots replaced with slashes in groupId part
    parts = pkg_name.split("/")
    if len(parts) == 2:
        group_id_path = parts[0].replace(".", "/")
        artifact_id = parts[1]
        maven_path = f"{group_id_path}/{artifact_id}/{pkg_version}/{file_name}"
    else:
        maven_path = f"{pkg_name}/{pkg_version}/{file_name}"

    resp = gitlab_api_get(f"/projects/{project_id}/packages/maven/{maven_path}")
    if resp.ok:
        return resp.content
    print_error(f"Failed to download maven file {file_name}: {resp.status_code} {resp.text[:200]}")
    return None


def gitlab_download_npm(project_id: int, pkg_name: str, file_name: str) -> Optional[bytes]:
    resp = gitlab_api_get(
        f"/projects/{project_id}/packages/npm/{urlquote(pkg_name, safe='@/')}/-/{urlquote(file_name, safe='')}"
    )
    if resp.ok:
        return resp.content
    print_error(f"Failed to download npm file {file_name}: {resp.status_code} {resp.text[:200]}")
    return None


def gitlab_download_nuget(project_id: int, pkg_name: str, pkg_version: str, file_name: str) -> Optional[bytes]:
    resp = gitlab_api_get(
        f"/projects/{project_id}/packages/nuget/download/{urlquote(pkg_name, safe='')}/{urlquote(pkg_version, safe='')}/{urlquote(file_name, safe='')}"
    )
    if resp.ok:
        return resp.content
    print_error(f"Failed to download nuget file {file_name}: {resp.status_code} {resp.text[:200]}")
    return None


def gitlab_download_pypi(project_id: int, file_sha256: str, file_name: str) -> Optional[bytes]:
    """Try project-level first, fall back to downloading via package file URL."""
    url = f"/projects/{project_id}/packages/pypi/files/{file_sha256}/{urlquote(file_name, safe='')}"
    resp = gitlab_api_get(url)
    if resp.ok:
        return resp.content
    print_warning(
        f"Project-level PyPI download failed for {file_name}: "
        f"status={resp.status_code}, url={GITLAB_URL}/api/v4{url}, "
        f"body={resp.text[:300]}"
    )
    return None


#######################
# Gitea helpers
#######################

def gitea_api_url(path: str) -> str:
    return f"{GITEA_URL}/api/v1{path}"


def gitea_packages_url(path: str) -> str:
    return f"{GITEA_URL}/api/packages{path}"


def gitea_auth_headers() -> dict:
    return {"Authorization": f"token {GITEA_TOKEN}"}


def gitea_package_exists(owner: str, pkg_type: str, pkg_name: str, pkg_version: str) -> bool:
    """Check if a package already exists in Gitea via the v1 API."""
    resp = requests.get(
        gitea_api_url(f"/packages/{owner}"),
        headers=gitea_auth_headers(),
        params={"type": pkg_type, "q": pkg_name},
    )
    if resp.ok:
        for p in resp.json():
            if p.get("name") == pkg_name and p.get("version") == pkg_version:
                return True
    return False


#######################
# Upload functions
#######################

def upload_generic(owner: str, pkg_name: str, pkg_version: str, file_name: str, data: bytes) -> bool:
    url = gitea_packages_url(f"/{owner}/generic/{urlquote(pkg_name, safe='')}/{urlquote(pkg_version, safe='')}/{urlquote(file_name, safe='')}")
    resp = requests.put(url, headers=gitea_auth_headers(), data=data)
    if resp.status_code in (201, 200, 409):
        if resp.status_code == 409:
            print_warning(f"  Generic file {file_name} already exists, skipping")
        else:
            print_info(f"  Uploaded generic: {pkg_name}/{pkg_version}/{file_name}")
        return True
    print_error(f"  Failed to upload generic {file_name}: {resp.status_code} {resp.text[:300]}")
    return False


def upload_maven(owner: str, pkg_name: str, pkg_version: str, file_name: str, data: bytes) -> bool:
    """Upload a Maven artifact. Gitea Maven accepts PUT on arbitrary paths."""
    parts = pkg_name.split("/")
    if len(parts) == 2:
        group_id_path = parts[0].replace(".", "/")
        artifact_id = parts[1]
        maven_path = f"{group_id_path}/{artifact_id}/{pkg_version}/{file_name}"
    else:
        maven_path = f"{pkg_name}/{pkg_version}/{file_name}"

    url = gitea_packages_url(f"/{owner}/maven/{maven_path}")
    resp = requests.put(url, headers=gitea_auth_headers(), data=data)
    if resp.status_code in (201, 200, 409):
        if resp.status_code == 409:
            print_warning(f"  Maven file {file_name} already exists, skipping")
        else:
            print_info(f"  Uploaded maven: {maven_path}")
        return True
    print_error(f"  Failed to upload maven {file_name}: {resp.status_code} {resp.text[:300]}")
    return False


def upload_nuget(owner: str, file_name: str, data: bytes) -> bool:
    """Upload a .nupkg file to Gitea NuGet registry."""
    url = gitea_packages_url(f"/{owner}/nuget/")
    resp = requests.put(
        url,
        headers={**gitea_auth_headers(), "Content-Type": "application/octet-stream"},
        data=data,
    )
    if resp.status_code in (201, 200, 409):
        if resp.status_code == 409:
            print_warning(f"  NuGet package {file_name} already exists, skipping")
        else:
            print_info(f"  Uploaded nuget: {file_name}")
        return True
    print_error(f"  Failed to upload nuget {file_name}: {resp.status_code} {resp.text[:300]}")
    return False


def upload_npm(owner: str, pkg_name: str, pkg_version: str, tgz_data: bytes) -> bool:
    """
    Upload an npm package by constructing the npm publish JSON payload.
    The payload contains the package metadata and the tarball base64-encoded.
    """
    package_json = _extract_package_json_from_tgz(tgz_data)
    if not package_json:
        print_error(f"  Could not extract package.json from npm tarball for {pkg_name}@{pkg_version}")
        return False

    pkg_json_name = package_json.get("name", pkg_name)
    pkg_json_version = package_json.get("version", pkg_version)

    tarball_b64 = base64.b64encode(tgz_data).decode("ascii")
    shasum = hashlib.sha1(tgz_data).hexdigest()
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(tgz_data).digest()).decode("ascii")

    tgz_filename = f"{pkg_json_name.replace('/', '-').lstrip('@')}-{pkg_json_version}.tgz"

    version_data = dict(package_json)
    version_data.setdefault("name", pkg_json_name)
    version_data.setdefault("version", pkg_json_version)
    version_data["dist"] = {
        "shasum": shasum,
        "integrity": integrity,
    }

    payload = {
        "_id": pkg_json_name,
        "name": pkg_json_name,
        "versions": {
            pkg_json_version: version_data,
        },
        "dist-tags": {
            "latest": pkg_json_version,
        },
        "_attachments": {
            tgz_filename: {
                "content_type": "application/octet-stream",
                "data": tarball_b64,
                "length": len(tgz_data),
            }
        },
    }

    if pkg_json_name.startswith("@"):
        scope_and_name = pkg_json_name  # e.g. @scope/name
        url_path = f"/{owner}/npm/{urlquote(scope_and_name, safe='@/')}"
    else:
        url_path = f"/{owner}/npm/{urlquote(pkg_json_name, safe='')}"

    url = gitea_packages_url(url_path)
    resp = requests.put(
        url,
        headers={**gitea_auth_headers(), "Content-Type": "application/json"},
        data=json.dumps(payload),
    )
    if resp.status_code in (201, 200, 409):
        if resp.status_code == 409:
            print_warning(f"  npm package {pkg_json_name}@{pkg_json_version} already exists, skipping")
        else:
            print_info(f"  Uploaded npm: {pkg_json_name}@{pkg_json_version}")
        return True
    print_error(f"  Failed to upload npm {pkg_json_name}@{pkg_json_version}: {resp.status_code} {resp.text[:300]}")
    return False


def _extract_package_json_from_tgz(tgz_data: bytes) -> Optional[dict]:
    """Extract package.json from the npm tarball (usually at package/package.json)."""
    try:
        with tarfile.open(fileobj=io.BytesIO(tgz_data), mode="r:gz") as tar:
            for member in tar.getmembers():
                if member.name.endswith("/package.json") or member.name == "package.json":
                    f = tar.extractfile(member)
                    if f:
                        return json.load(f)
    except Exception as e:
        print_warning(f"  Error extracting package.json from tarball: {e}")
    return None


def upload_pypi(owner: str, pkg_name: str, pkg_version: str, file_name: str, data: bytes) -> bool:
    """Upload a PyPI package file via multipart form (twine-compatible)."""
    sha256_digest = hashlib.sha256(data).hexdigest()

    if file_name.endswith(".whl"):
        filetype = "bdist_wheel"
    else:
        filetype = "sdist"

    url = gitea_packages_url(f"/{owner}/pypi/")
    fields = {
        ":action": (None, "file_upload"),
        "protocol_version": (None, "1"),
        "metadata_version": (None, "2.1"),
        "name": (None, pkg_name),
        "version": (None, pkg_version),
        "filetype": (None, filetype),
        "sha256_digest": (None, sha256_digest),
        "content": (file_name, data, "application/octet-stream"),
    }
    resp = requests.post(url, headers=gitea_auth_headers(), files=fields)
    if resp.status_code in (201, 200, 409):
        if resp.status_code == 409:
            print_warning(f"  PyPI package {file_name} already exists, skipping")
        else:
            print_info(f"  Uploaded pypi: {pkg_name}/{pkg_version}/{file_name}")
        return True
    print_error(f"  Failed to upload pypi {file_name}: {resp.status_code} {resp.text[:300]}")
    return False


#######################
# Package migration
#######################

def migrate_project_packages(gl: gitlab.Gitlab, project: gitlab.v4.objects.Project, owner: str):
    """Migrate all supported package types for a single project."""
    project_id = project.id
    project_name = project.path_with_namespace

    try:
        packages = project.packages.list(get_all=True)
    except Exception as e:
        print_warning(f"  Could not list packages for {project_name}: {e}")
        return

    if not packages:
        return

    print(f"\n  Found {len(packages)} package(s) in {project_name}")

    for pkg in packages:
        pkg_type = pkg.package_type
        pkg_name = pkg.name
        pkg_version = pkg.version

        if pkg_type not in PACKAGE_TYPES_TO_MIGRATE:
            print(f"    Skipping {pkg_type} package {pkg_name}@{pkg_version} (not in PACKAGE_TYPES_TO_MIGRATE)")
            continue

        if pkg_version is None or pkg_version == "":
            print_warning(f"    Skipping versionless package {pkg_name} (type={pkg_type})")
            continue

        print(f"    Migrating {pkg_type} package: {pkg_name}@{pkg_version}")

        try:
            pkg_files = pkg.package_files.list(get_all=True)
        except Exception as e:
            print_error(f"    Failed to list files for {pkg_name}@{pkg_version}: {e}")
            continue

        if not pkg_files:
            print_warning(f"    No files found for {pkg_name}@{pkg_version}")
            continue

        for pf in pkg_files:
            file_name = pf.file_name
            file_sha256 = getattr(pf, "file_sha256", None)

            print(f"      File: {file_name}")

            data = _download_package_file(project_id, pkg_type, pkg_name, pkg_version, file_name, file_sha256)
            if data is None:
                continue

            _upload_package_file(owner, pkg_type, pkg_name, pkg_version, file_name, data)


def _download_package_file(
    project_id: int, pkg_type: str, pkg_name: str, pkg_version: str,
    file_name: str, file_sha256: Optional[str]
) -> Optional[bytes]:
    if pkg_type == "generic":
        return gitlab_download_generic(project_id, pkg_name, pkg_version, file_name)
    elif pkg_type == "maven":
        return gitlab_download_maven(project_id, pkg_name, pkg_version, file_name)
    elif pkg_type == "npm":
        return gitlab_download_npm(project_id, pkg_name, file_name)
    elif pkg_type == "nuget":
        return gitlab_download_nuget(project_id, pkg_name, pkg_version, file_name)
    elif pkg_type == "pypi":
        if file_sha256:
            data = gitlab_download_pypi(project_id, file_sha256, file_name)
            if data:
                return data
        else:
            print_warning(f"      No sha256 available for PyPI file {file_name}, skipping project-level endpoint")
        # Fallback: try downloading via generic-style endpoint
        fallback_sha = file_sha256 or "unknown"
        fallback_url = f"/projects/{project_id}/packages/pypi/files/{fallback_sha}/{urlquote(file_name, safe='')}"
        print_warning(f"      PyPI download fallback: trying {GITLAB_URL}/api/v4{fallback_url}")
        resp = gitlab_api_get(fallback_url)
        if resp.ok:
            return resp.content
        print_error(
            f"      All PyPI download methods failed for {file_name}: "
            f"status={resp.status_code}, body={resp.text[:300]}"
        )
        return None
    else:
        print_warning(f"      Unsupported package type for download: {pkg_type}")
        return None


def _upload_package_file(
    owner: str, pkg_type: str, pkg_name: str, pkg_version: str,
    file_name: str, data: bytes
):
    if pkg_type == "generic":
        upload_generic(owner, pkg_name, pkg_version, file_name, data)
    elif pkg_type == "maven":
        upload_maven(owner, pkg_name, pkg_version, file_name, data)
    elif pkg_type == "npm":
        upload_npm(owner, pkg_name, pkg_version, data)
    elif pkg_type == "nuget":
        upload_nuget(owner, file_name, data)
    elif pkg_type == "pypi":
        upload_pypi(owner, pkg_name, pkg_version, file_name, data)


#######################
# Container migration
#######################

def _resolve_gitlab_registry_host(gl: gitlab.Gitlab) -> str:
    """Try to determine the GitLab container registry host."""
    if GITLAB_REGISTRY_HOST:
        return GITLAB_REGISTRY_HOST
    # Try the GitLab settings API
    try:
        settings = gl.settings.get()
        reg_url = getattr(settings, "container_registry_url", None)
        if reg_url:
            return reg_url.replace("https://", "").replace("http://", "").rstrip("/")
    except Exception:
        pass
    # Common convention: same host, port 5050
    from urllib.parse import urlparse
    parsed = urlparse(GITLAB_URL)
    return f"{parsed.hostname}:5050"


def _resolve_gitea_registry_host() -> str:
    if GITEA_REGISTRY_HOST:
        return GITEA_REGISTRY_HOST
    from urllib.parse import urlparse
    parsed = urlparse(GITEA_URL)
    return parsed.hostname


def _list_registry_repositories(project_id: int) -> list:
    """List container registry repositories via REST API (more reliable than python-gitlab wrapper)."""
    repos = []
    page = 1
    while True:
        resp = gitlab_api_get(
            f"/projects/{project_id}/registry/repositories?tags=true&per_page=100&page={page}"
        )
        if not resp.ok:
            break
        batch = resp.json()
        if not batch:
            break
        repos.extend(batch)
        page += 1
    return repos


def migrate_project_containers(
    gl: gitlab.Gitlab,
    project: gitlab.v4.objects.Project,
    owner: str,
    gitlab_registry: str,
    gitea_registry: str,
):
    """Migrate container images for a project using skopeo."""
    project_name = project.path_with_namespace

    try:
        repos = _list_registry_repositories(project.id)
    except Exception as e:
        print_warning(f"  Could not list container repositories for {project_name}: {e}")
        return

    if not repos:
        return

    print(f"\n  Found {len(repos)} container repository(ies) in {project_name}")

    for repo in repos:
        repo_path = repo.get("path", "")
        print(f"    Container repo: {repo_path}")

        tags = repo.get("tags", [])
        if not tags:
            # Try fetching tags separately if not included
            repo_id = repo.get("id")
            if repo_id:
                tag_resp = gitlab_api_get(
                    f"/projects/{project.id}/registry/repositories/{repo_id}/tags?per_page=100"
                )
                if tag_resp.ok:
                    tags = tag_resp.json()

        if not tags:
            print_warning(f"    No tags found for {repo_path}")
            continue

        # Determine the Gitea image name
        # GitLab path: group/project/optional-image-name
        # Gitea path: owner/image-name
        path_parts = repo_path.split("/")
        if len(path_parts) > 2:
            image_suffix = "/".join(path_parts[2:])
            gitea_image = f"{owner}/{image_suffix}"
        else:
            gitea_image = f"{owner}/{path_parts[-1]}"

        for tag in tags:
            tag_name = tag.get("name", "") if isinstance(tag, dict) else tag.name
            if not tag_name:
                continue

            src_image = f"docker://{gitlab_registry}/{repo_path}:{tag_name}"
            dst_image = f"docker://{gitea_registry}/{gitea_image}:{tag_name}"

            print(f"      Copying {tag_name}: {src_image} -> {dst_image}")

            success = _skopeo_copy(src_image, dst_image)
            if success:
                print_info(f"      Copied {repo_path}:{tag_name}")
            else:
                print_error(f"      Failed to copy {repo_path}:{tag_name}")


def _skopeo_copy(src: str, dst: str) -> bool:
    """Use skopeo to copy a container image between registries."""
    src_creds = f"oauth2:{GITLAB_TOKEN}"
    # Gitea accepts username:token for container auth
    dst_creds = f"__token__:{GITEA_TOKEN}"

    cmd = [
        "skopeo", "copy",
        "--src-creds", src_creds,
        "--dest-creds", dst_creds,
        src, dst,
    ]

    # Allow insecure registries if using HTTP
    if GITLAB_URL.startswith("http://"):
        cmd.insert(2, "--src-tls-verify=false")
    if GITEA_URL.startswith("http://"):
        cmd.insert(2, "--dest-tls-verify=false")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode == 0:
            return True
        print_error(f"      skopeo error: {result.stderr.strip()[:500]}")
        return False
    except FileNotFoundError:
        print_error("      skopeo is not installed! Install it: apt-get install skopeo")
        return False
    except subprocess.TimeoutExpired:
        print_error("      skopeo timed out after 600 seconds")
        return False
    except Exception as e:
        print_error(f"      skopeo exception: {e}")
        return False


#######################
# Main
#######################

def main():
    print(f"{bcolors.HEADER}---=== GitLab to Gitea Package & Container Migration ===---{bcolors.ENDC}")
    print(f"Version: {SCRIPT_VERSION}")
    print()

    # Validate required config
    if not GITLAB_TOKEN:
        print_error("GITLAB_TOKEN is not set")
        sys.exit(1)
    if not GITEA_TOKEN:
        print_error("GITEA_TOKEN is not set")
        sys.exit(1)

    # Connect to GitLab
    gl = gitlab.Gitlab(GITLAB_URL, private_token=GITLAB_TOKEN)
    gl.auth()
    print_info(f"Connected to GitLab: {GITLAB_URL} (version: {gl.version()[0]})")

    # Verify Gitea connectivity
    gt_resp = requests.get(gitea_api_url("/version"), headers=gitea_auth_headers())
    if gt_resp.ok:
        print_info(f"Connected to Gitea: {GITEA_URL} (version: {gt_resp.json().get('version', '?')})")
    else:
        print_error(f"Cannot connect to Gitea: {gt_resp.status_code}")
        sys.exit(1)

    # Prepare temp directory
    os.makedirs(TMP_DIR, exist_ok=True)

    # Resolve container registry hosts
    gitlab_registry = _resolve_gitlab_registry_host(gl) if MIGRATE_CONTAINERS else ""
    gitea_registry = _resolve_gitea_registry_host() if MIGRATE_CONTAINERS else ""

    if MIGRATE_CONTAINERS:
        if not shutil.which("skopeo"):
            print_error("MIGRATE_CONTAINERS=1 but skopeo is not installed!")
            print_error("Install: apt-get install skopeo  /  brew install skopeo")
            sys.exit(1)
        print_info(f"GitLab registry: {gitlab_registry}")
        print_info(f"Gitea  registry: {gitea_registry}")

    if MIGRATE_PACKAGES:
        print_info(f"Package types to migrate: {', '.join(PACKAGE_TYPES_TO_MIGRATE)}")

    # Gather all projects
    print()
    print("Gathering projects...")
    projects: List[gitlab.v4.objects.Project] = gl.projects.list(get_all=True, iterator=True)
    project_list = list(projects)
    print(f"Found {len(project_list)} project(s)")

    # Migrate each project
    for project in project_list:
        owner = name_clean(project.namespace["name"])
        project_display = f"{owner}/{name_clean(project.name)}"

        print(f"\n{'='*60}")
        print(f"Project: {project.path_with_namespace} -> {project_display}")
        print(f"{'='*60}")

        if MIGRATE_PACKAGES:
            migrate_project_packages(gl, project, owner)

        if MIGRATE_CONTAINERS:
            migrate_project_containers(gl, project, owner, gitlab_registry, gitea_registry)

    # Summary
    print()
    print("=" * 60)
    if GLOBAL_ERROR_COUNT == 0:
        print_success("Migration finished with no errors!")
    else:
        print_error(f"Migration finished with {GLOBAL_ERROR_COUNT} error(s)!")

    # Cleanup
    if os.path.exists(TMP_DIR) and not os.listdir(TMP_DIR):
        os.rmdir(TMP_DIR)


if __name__ == "__main__":
    main()
