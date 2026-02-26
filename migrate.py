import base64
import os
import time
import random
import string
import requests
import json
import dateutil.parser
import datetime
import re
from typing import List
import json
import pytz

import gitlab  # pip install python-gitlab
import gitlab.v4.objects
import pygitea
import psycopg2
import dotenv

dotenv.load_dotenv()


SCRIPT_VERSION = "1.0"
GLOBAL_ERROR_COUNT = 0

#######################
# CONFIG SECTION START
#######################

# Gitea 資料庫設定 (這裡以 PostgreSQL 為例，請根據你的 Gitea 資料庫類型修改)
PG_HOST = os.getenv("PG_HOST", "localhost")
PG_PORT = os.getenv("PG_PORT", "5432")
PG_NAME = os.getenv("PG_NAME", "gitea")
PG_USER = os.getenv("PG_USER", "gitea")
PG_PASS = os.getenv("PG_PASS", "gitea")

# 預設的登入來源 ID (0 表示預設/本機或未指定特定登入來源，可根據需求修改)
LOGIN_SOURCE_ID = 0
# 供應商名稱，Gitea 預設吃 'gitlab'
PROVIDER_NAME = "gitlab"

# Gitea user to use as a fallback for groups
# for cases where the user's permissions are too limited to access group member details on GitLab.
GITEA_FALLBACK_GROUP_MEMBER = os.getenv("GITEA_FALLBACK_GROUP_MEMBER", "gitea_admin")

REPOSITORY_MIRROR = (
    os.getenv("REPOSITORY_MIRROR", "false")
) == "true"  # if true, the repository will be mirrored
GITLAB_URL = os.getenv("GITLAB_URL", "https://gitlab.source.com")
GITLAB_API_BASEURL = GITLAB_URL + "/api/v4"
GITLAB_TOKEN = os.getenv("GITLAB_TOKEN", "gitlab token")

# needed to clone the repositories, keep empty to try publickey (untested)
GITLAB_ADMIN_USER = os.getenv("GITLAB_ADMIN_USER", "admin username")
GITLAB_ADMIN_PASS = os.getenv("GITLAB_ADMIN_PASS", "admin password")

if (
    GITLAB_URL == "https://gitlab.com/"
    and GITLAB_ADMIN_USER == ""
    and GITLAB_ADMIN_PASS == ""
):
    # see https://forum.gitlab.com/t/how-to-git-clone-via-https-with-personal-access-token-in-private-project/43418/4
    GITLAB_ADMIN_USER = "oauth2"
    GITLAB_ADMIN_PASS = GITLAB_TOKEN
GITEA_URL = os.getenv("GITEA_URL", "https://gitea.dest.com")
GITEA_API_BASEURL = GITEA_URL + "/api/v1"
GITEA_TOKEN = os.getenv("GITEA_TOKEN", "gitea token")

# For migrating from a self-hosted gitlab instance, use MIGRATE_BY_GROUPS=0
# For migrating from the global gitlab.com, use MIGRATE_BY_GROUPS=1 which
# migrates only projects and users which belong to groups accessible to the
# user of the GITLAB_TOKEN.
MIGRATE_BY_GROUPS = (os.getenv("MIGRATE_BY_GROUPS", "0")) == "1"
TRUNCATE_GITEA = (os.getenv("TRUNCATE_GITEA", "0")) == "1"

# Migrated projects can be automatically archived on gitlab to avoid users pushing
# there commits after the migration to gitea
GITLAB_ARCHIVE_MIGRATED_PROJECTS = (
    os.getenv("GITLAB_ARCHIVE_MIGRATED_PROJECTS", "0")
) == "1"
#######################
# CONFIG SECTION END
#######################


def main():
    print_color(bcolors.HEADER, "---=== Gitlab to Gitea migration ===---")
    print("Version: " + SCRIPT_VERSION)
    print()

    # private token or personal token authentication
    gl = gitlab.Gitlab(GITLAB_URL, private_token=GITLAB_TOKEN)
    gl.auth()
    assert isinstance(gl.user, gitlab.v4.objects.CurrentUser)
    print_info("Connected to Gitlab, version: " + str(gl.version()))

    gt = pygitea.API(GITEA_URL, token=GITEA_TOKEN)
    gt_version = gt.get("/version").json()
    print_info("Connected to Gitea, version: " + str(gt_version["version"]))

    if TRUNCATE_GITEA:
        print("Truncate...")
        truncate_all(gt)
        print("Truncate... done")

    # Create a directory in /tmp called gitlab_to_gitea
    tmp_dir = "/tmp/gitlab_to_gitea"
    if not os.path.exists(tmp_dir):
        os.makedirs(tmp_dir)
        print(f"Directory {tmp_dir} created.")
    else:
        print(f"Directory {tmp_dir} already exists.")

    print("Gathering projects and users...")
    users: List[gitlab.v4.objects.User] = []
    groups: List[gitlab.v4.objects.Group] = gl.groups.list(all=True)
    projects: List[gitlab.v4.objects.Project] = []

    if MIGRATE_BY_GROUPS:
        user_ids: Dict[int, int] = {}
        project_ids: Dict[int, int] = {}
        groups = gl.groups.list(all=True)
        for group in groups:
            print("group:", group.full_path)
            # ıf we do not have access memberlist do not run member creating
            try:
                for member in group.members.list(get_all=True, iterator=True):
                    print("    member:", member.username)
                    user_ids[member.id] = 1
            except Exception as e:
                print(
                    "Skipping group member import for group "
                    + group.full_path
                    + " due to error: "
                    + str(e)
                )

            for group_project in group.projects.list(get_all=True, iterator=True):
                print("    group_project:", group_project.name_with_namespace)
                project_ids[group_project.id] = 1
                project = gl.projects.get(id=group_project.id)
                print("    project:", project.name_with_namespace)
                for member in project.members.list(get_all=True, iterator=True):
                    print("        member:", member.username)
                    user_ids[member.id] = 1
                for user in project.users.list(get_all=True, iterator=True):
                    print("        user:", user.username)
                    user_ids[user.id] = 1

        for user_id in user_ids:
            user = gl.users.get(id=user_id)
            # # Skip internal or bot users
            # if getattr(user, 'bot', False) or user.username.endswith('-bot') or user.username in ['ghost', 'support-bot', 'alert-bot']:
            #     print('Skipping internal user:', user.username)
            #     continue
            print("user_id:", user_id, " user:", user.username)
            users.append(user)
            for project in user.projects.list(iterator=True):
                print("    project:", project.name_with_namespace)

        for project_id in project_ids:
            project = gl.projects.get(id=project_id)
            print(
                "project_id:",
                project_id,
                " project:",
                project.name_with_namespace,
                " archived:",
                project.archived,
            )
            projects.append(project)

    else:
        for u in gl.users.list(get_all=True, iterator=True):
            # if (getattr(u, 'bot', False) or u.username.endswith('-bot') or u.username in ['ghost', 'support-bot', 'alert-bot']):
            #     print('Skipping internal user:', u.username)
            #     continue
            print("user_id:", u.id, " user:", u.username)
            users.append(u)

        for project in gl.projects.list(get_all=True, iterator=True):
            print(
                "project_id:",
                project.id,
                " project:",
                project.name_with_namespace,
                " archived:",
                project.archived,
            )
            projects.append(project)

    print("Gathering projects and users...done")

    # IMPORT USERS AND GROUPS
    import_users_groups(gl, gt, users, groups)

    # MAP USERS
    map_users()

    # IMPORT PROJECTS
    import_projects(gl, gt, projects)

    print()
    if GLOBAL_ERROR_COUNT == 0:
        print_success("Migration finished with no errors!")
    else:
        print_error("Migration finished with " + str(GLOBAL_ERROR_COUNT) + " errors!")


#
# Data loading helpers for Gitea
#


def get_project_labels(gitea_api: pygitea, owner: string, repo: string) -> []:
    existing_labels = []
    label_response: requests.Response = gitea_api.get(
        "/repos/" + owner + "/" + repo + "/labels"
    )
    if label_response.ok:
        existing_labels = label_response.json()
    else:
        print_error(
            "Failed to load existing labels for project "
            + repo
            + "! "
            + label_response.text
        )

    return existing_labels


def get_group_labels(gitea_api: pygitea, group: string) -> []:
    existing_labels = []
    label_response: requests.Response = gitea_api.get("/orgs/" + group + "/labels")
    if label_response.ok:
        existing_labels = label_response.json()
    else:
        print_error(
            "Failed to load existing labels for group "
            + group
            + "! "
            + label_response.text
        )

    return existing_labels


def get_merged_labels(
    gitea_api: pygitea, owner: string, repo: string, is_group: bool = False
) -> []:
    project_labels = get_project_labels(gitea_api, owner, repo)
    group_labels = get_group_labels(gitea_api, owner) if is_group else []
    return project_labels + group_labels


def get_milestones(gitea_api: pygitea, owner: string, repo: string) -> []:
    existing_milestones = []
    milestone_response: requests.Response = gitea_api.get(
        "/repos/" + owner + "/" + repo + "/milestones"
    )
    if milestone_response.ok:
        existing_milestones = milestone_response.json()
    else:
        print_error(
            "Failed to load existing milestones for project "
            + repo
            + "! "
            + milestone_response.text
        )

    return existing_milestones


def get_issues(gitea_api: pygitea, owner: string, repo: string) -> []:
    existing_issues = []
    issue_response: requests.Response = gitea_api.get(
        "/repos/" + owner + "/" + repo + "/issues", params={"state": "all", "page": -1}
    )
    if issue_response.ok:
        existing_issues = issue_response.json()
    else:
        print_error(
            "Failed to load existing issues for project "
            + repo
            + "! "
            + issue_response.text
        )

    return existing_issues


def get_issue_comments(gitea_api: pygitea, owner: string, repo: string) -> []:
    existing_issue_comments = []
    issue_comments_response: requests.Response = gitea_api.get(
        "/repos/" + owner + "/" + repo + "/issues/comments",
        params={"state": "all", "page": -1},
    )
    if issue_comments_response.ok:
        existing_issue_comments = issue_comments_response.json()
    else:
        print_error(
            "Failed to load existing issue comments for project "
            + repo
            + "! "
            + issue_comments_response.text
        )

    return existing_issue_comments


def get_teams(gitea_api: pygitea, orgname: string) -> []:
    existing_teams = []
    team_response: requests.Response = gitea_api.get("/orgs/" + orgname + "/teams")
    if team_response.ok:
        existing_teams = team_response.json()
    else:
        print_error(
            "Failed to load existing teams for organization "
            + orgname
            + "! "
            + team_response.text
        )

    return existing_teams


def get_team_members(gitea_api: pygitea, teamid: int) -> []:
    existing_members = []
    member_response: requests.Response = gitea_api.get(
        "/teams/" + str(teamid) + "/members"
    )
    if member_response.ok:
        existing_members = [member["username"] for member in member_response.json()]
    else:
        print_error(
            "Failed to load existing members for team "
            + str(teamid)
            + "! "
            + member_response.text
        )

    return existing_members


def get_collaborators(gitea_api: pygitea, owner: string, repo: string) -> []:
    existing_collaborators = []
    collaborator_response: requests.Response = gitea_api.get(
        "/repos/" + owner + "/" + repo + "/collaborators"
    )
    if collaborator_response.ok:
        existing_collaborators = collaborator_response.json()
    else:
        print_error(
            "Failed to load existing collaborators for project "
            + repo
            + "! "
            + collaborator_response.text
        )

    return existing_collaborators


def get_user_or_group(gitea_api: pygitea, project: gitlab.v4.objects.Project) -> {}:
    result = None
    response: requests.Response = gitea_api.get(
        "/users/" + name_clean(project.namespace["name"])
    )
    if response.ok:
        result = response.json()

    # The api may return a 200 response, even if it's not a user but an org, let's try again!
    if result is None or result["id"] == 0:
        response: requests.Response = gitea_api.get(
            "/orgs/" + name_clean(project.namespace["name"])
        )
        if response.ok:
            result = response.json()
        else:
            print_error(
                "Failed to load user or group "
                + name_clean(project.namespace["name"])
                + "! "
                + response.text
            )

    return result


def get_user_keys(gitea_api: pygitea, username: string) -> []:
    existing_keys = []
    key_response: requests.Response = gitea_api.get("/users/" + username + "/keys")
    if key_response.ok:
        existing_keys = [key["title"] for key in key_response.json()]
    else:
        print_error(
            "Failed to load user keys for user " + username + "! " + key_response.text
        )

    return existing_keys


def user_exists(gitea_api: pygitea, username: string) -> bool:
    print("Looking for " + "/users/" + username + "/keys" + " in Gitea!")
    user_response: requests.Response = gitea_api.get("/users/" + username)
    if user_response.ok:
        print_warning("User " + username + " does already exist in Gitea, skipping!")
    else:
        print("User " + username + " not found in Gitea, importing!")

    return user_response.ok


def user_key_exists(gitea_api: pygitea, username: string, keyname: string) -> bool:
    print("Looking for " + "/users/" + username + "/keys" + " in Gitea!")
    existing_keys = get_user_keys(gitea_api, username)
    if existing_keys:
        if keyname in existing_keys:
            print_warning(
                "Public key "
                + keyname
                + " already exists for user "
                + username
                + ", skipping!"
            )
            return True
        else:
            print(
                "Public key "
                + keyname
                + " does not exists for user "
                + username
                + ", importing!"
            )
            return False
    else:
        print("No public keys for user " + username + ", importing!")
        return False


def organization_exists(gitea_api: pygitea, orgname: string) -> bool:
    print("Looking for " + "/orgs/" + orgname + " in Gitea!")
    group_response: requests.Response = gitea_api.get("/orgs/" + orgname)
    if group_response.ok:
        print_warning("Group " + orgname + " does already exist in Gitea, skipping!")
    else:
        print("Group " + orgname + " not found in Gitea, importing!")

    return group_response.ok


def member_exists(gitea_api: pygitea, username: string, teamid: int) -> bool:
    print("Looking for " + "/teams/" + str(teamid) + "/members" + " in Gitea!")
    existing_members = get_team_members(gitea_api, teamid)
    if existing_members:
        if username in existing_members:
            print_warning(
                "Member "
                + username
                + " is already in team "
                + str(teamid)
                + ", skipping!"
            )
            return True
        else:
            print(
                "Member " + username + " is not in team " + str(teamid) + ", importing!"
            )
            return False
    else:
        print("No members in team " + str(teamid) + ", importing!")
        return False


def collaborator_exists(
    gitea_api: pygitea, owner: string, repo: string, username: string
) -> bool:
    print(
        "Looking for "
        + "/repos/"
        + owner
        + "/"
        + repo
        + "/collaborators/"
        + username
        + " in Gitea!"
    )
    collaborator_response: requests.Response = gitea_api.get(
        "/repos/" + owner + "/" + repo + "/collaborators/" + username
    )
    if collaborator_response.ok:
        print_warning(
            "Collaborator " + username + " does already exist in Gitea, skipping!"
        )
    else:
        print("Collaborator " + username + " not found in Gitea, importing!")

    return collaborator_response.ok


def repo_exists(gitea_api: pygitea, owner: string, repo: string) -> bool:
    print("Looking for " + "/repos/" + owner + "/" + repo + " in Gitea!")
    repo_response: requests.Response = gitea_api.get("/repos/" + owner + "/" + repo)
    if repo_response.ok:
        print_warning("Project " + repo + " does already exist in Gitea, skipping!")
    else:
        print("Project " + repo + " not found in Gitea, importing!")

    return repo_response.ok


def project_label_exists(
    gitea_api: pygitea, owner: string, repo: string, labelname: string
) -> bool:
    print("Looking for " + "/repos/" + owner + "/" + repo + "/labels in Gitea!")
    existing_labels = [
        label["name"] for label in get_project_labels(gitea_api, owner, repo)
    ]
    if existing_labels:
        if labelname in existing_labels:
            print_warning(
                "Label "
                + labelname
                + " already exists in project "
                + repo
                + " of owner "
                + owner
            )
            return True
        else:
            print(
                "Label "
                + labelname
                + " does not exists in project "
                + repo
                + " of owner "
                + owner
            )
            return False
    else:
        print("No labels in project " + repo + " of owner " + owner)
        return False


def group_label_exists(gitea_api: pygitea, group: string, labelname: string) -> bool:
    print("Looking for " + "/orgs/" + group + "/labels in Gitea!")
    existing_labels = [label["name"] for label in get_group_labels(gitea_api, group)]
    if existing_labels:
        if labelname in existing_labels:
            print_warning("Label " + labelname + " already exists in group " + group)
            return True
        else:
            print("Label " + labelname + " does not exists in group " + group)
            return False
    else:
        print("No labels in group " + group)
        return False


def milestone_exists(
    gitea_api: pygitea, owner: string, repo: string, milestone: string
) -> bool:
    print(
        "Looking for " + "/repos/" + owner + "/" + repo + "/milestones" + " in Gitea!"
    )
    existing_milestones = [m["title"] for m in get_milestones(gitea_api, owner, repo)]
    if existing_milestones:
        if milestone in existing_milestones:
            print_warning(
                "Milestone "
                + milestone
                + " already exists in project "
                + repo
                + " of owner "
                + owner
            )
            return True
        else:
            print(
                "Milestone "
                + milestone
                + " does not exists in project "
                + repo
                + " of owner "
                + owner
            )
            return False
    else:
        print("No milestones in project " + repo + " of owner " + owner)
        return False


def get_issue(
    gitea_api: pygitea,
    owner: string,
    repo: string,
    issue_title: string = None,
    issue_id: int = None,
) -> {}:
    if issue_title is not None:
        print(
            "Looking for " + "/repos/" + owner + "/" + repo + "/issues" + " in Gitea!"
        )
        existing_issues = get_issues(gitea_api, owner, repo)
        if existing_issues:
            existing_issue = next(
                (item for item in existing_issues if item["title"] == issue_title), None
            )
            if existing_issue is not None:
                print("Issue " + issue_title + " already exists in project " + repo)
                return existing_issue
            else:
                print("Issue " + issue_title + " does not exists in project " + repo)
                return None
        else:
            print("No issues in project " + repo)
            return None
    elif issue_id is not None:
        print(
            "Looking for "
            + "/repos/"
            + owner
            + "/"
            + repo
            + "/issues/"
            + str(issue_id)
            + " in Gitea!"
        )
        issue_response: requests.Response = gitea_api.get(
            "/repos/" + owner + "/" + repo + "/issues/" + str(issue_id)
        )
        if issue_response.ok:
            print("Issue " + str(issue_id) + " already exists in project " + repo)
            return issue_response.json()
        else:
            print("Issue " + str(issue_id) + " does not exists in project " + repo)
            return None
    else:
        print_error("No issue title or id provided!")


def get_issue_comment(
    gitea_api: pygitea,
    owner: string,
    repo: string,
    issue_url: string,
    comment_body: string,
):
    print(
        "Looking for "
        + "/repos/"
        + owner
        + "/"
        + repo
        + "/issues/comments"
        + " in Gitea!"
    )
    existing_issue_comments = get_issue_comments(gitea_api, owner, repo)
    if existing_issue_comments:
        existing_issue_comment = next(
            (
                item
                for item in existing_issue_comments
                if (item["body"] == comment_body) and (issue_url == item["issue_url"])
            ),
            None,
        )

        short_comment_body = (
            (comment_body[0:10] + "...") if len(comment_body) > 10 else comment_body
        )
        if existing_issue_comment is not None:
            print(
                "Issue comment "
                + short_comment_body
                + " already exists in project "
                + repo
            )
            return existing_issue_comment
        else:
            print(
                "Issue comment "
                + short_comment_body
                + " does not exists in project "
                + repo
            )
            return None
    else:
        print("No issue comments in project " + repo)
        return None


#
# Import helper functions
#


def _import_project_labels(
    gitea_api: pygitea,
    project: gitlab.v4.objects.Project,
    labels: [gitlab.v4.objects.ProjectLabel],
    owner: string,
    repo: string,
):
    is_group = project.namespace.get("kind") == "group"
    merged_labels = [
        label["name"] for label in get_merged_labels(gitea_api, owner, repo, is_group)
    ]
    for label in labels:
        if not label.name in merged_labels:
            import_response: requests.Response = gitea_api.post(
                "/repos/" + owner + "/" + repo + "/labels",
                json={
                    "name": label.name,
                    "color": label.color,
                    "description": label.description,  # currently not supported
                },
            )
            if import_response.ok:
                print_info("Label " + label.name + " imported!")
            else:
                print_error(
                    "Label " + label.name + " import failed: " + import_response.text
                )


def _import_project_milestones(
    gitea_api: pygitea,
    milestones: [gitlab.v4.objects.ProjectMilestone],
    owner: string,
    repo: string,
):
    for milestone in milestones:
        print(
            "_import_project_milestones, "
            + milestone.title
            + " with owner: "
            + owner
            + ", repo: "
            + repo
        )
        if not milestone_exists(gitea_api, owner, repo, milestone.title):
            due_date = None
            if milestone.due_date is not None and milestone.due_date != "":
                due_date = dateutil.parser.parse(milestone.due_date).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )

            import_response: requests.Response = gitea_api.post(
                "/repos/" + owner + "/" + repo + "/milestones",
                json={
                    "description": milestone.description,
                    "due_on": due_date,
                    "title": milestone.title,
                },
            )
            if import_response.ok:
                print_info("Milestone " + milestone.title + " imported!")
                existing_milestone = import_response.json()

                if existing_milestone:
                    # update milestone state, this cannot be done in the initial import :(
                    # TODO: gitea api ignores the closed state...
                    update_response: requests.Response = gitea_api.patch(
                        "/repos/"
                        + owner
                        + "/"
                        + repo
                        + "/milestones/"
                        + str(existing_milestone["id"]),
                        json={
                            "description": milestone.description,
                            "due_on": due_date,
                            "title": milestone.title,
                            "state": milestone.state,
                        },
                    )
                    if update_response.ok:
                        print_info("Milestone " + milestone.title + " updated!")
                    else:
                        print_error(
                            "Milestone "
                            + milestone.title
                            + " update failed: "
                            + update_response.text
                        )
            else:
                print_error(
                    "Milestone "
                    + milestone.title
                    + " import failed: "
                    + import_response.text
                )


def _import_project_issues(
    gitea_api: pygitea,
    project: gitlab.v4.objects.Project,
    issues: [gitlab.v4.objects.ProjectIssue],
    owner: string,
    repo: string,
):
    # reload all existing milestones and labels, needed for assignment in issues
    is_group = project.namespace.get("kind") == "group"
    existing_milestones = get_milestones(gitea_api, owner, repo)
    existing_labels = get_merged_labels(gitea_api, owner, repo, is_group)

    org_members = []
    # project.namespace['kind'] indicates if it's a 'user' or 'group'
    if is_group:
        org_members_response = gitea_api.get(f"/orgs/{owner}/members")
        if org_members_response.ok:
            org_members = [
                member["login"] for member in json.loads(org_members_response.text)
            ]
    else:
        # If owner is a user rather than an organization, fallback to owner and collaborators
        org_members = [owner]
        collaborators = get_collaborators(gitea_api, owner, repo)
        if collaborators:
            org_members.extend(
                [c.get("login", c.get("username")) for c in collaborators]
            )

    is_public = getattr(project, "visibility", "private") == "public"

    for issue in issues:
        print(
            "_import_project_issues"
            + issue.title
            + " with owner: "
            + owner
            + ", repo: "
            + repo
        )
        notes: List[gitlab.v4.objects.ProjectIssueNote] = sorted(
            issue.notes.list(all=True), key=lambda x: x.created_at
        )

        gitea_issue = get_issue(gitea_api, owner, repo, issue.title)
        if not gitea_issue:
            due_date = ""
            if issue.due_date is not None:
                due_date = dateutil.parser.parse(issue.due_date).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )

            # assignee = None
            # if issue.assignee is not None:
            #     assignee = issue.assignee['username']

            assignees = []
            if issue.assignee is not None:
                assignees.append(issue.assignee["username"])
            for tmp_assignee in issue.assignees:
                assignees.append(tmp_assignee["username"])

            milestone = None
            if issue.milestone is not None:
                for em in existing_milestones:
                    if em["title"] == issue.milestone["title"]:
                        milestone = em["id"]
                        break

            labels = [
                label["id"]
                for label in existing_labels
                if label["name"] in issue.labels
            ]

            created_at_utc = dateutil.parser.parse(issue.created_at)
            created_at_local = created_at_utc.astimezone(
                pytz.timezone("Asia/Taipei")
            ).strftime("%d.%m.%Y %H:%M")
            body = f"Created at: {created_at_local}\n\n{issue.description}"
            body = replace_issue_links(body, GITLAB_URL, GITEA_URL)

            params = {}
            if is_public or issue.author["username"] in org_members:
                params["sudo"] = issue.author["username"]
            else:
                body = f"Autor: {issue.author['name']}\n\n{body}"

            import_response: requests.Response = gitea_api.post(
                "/repos/" + owner + "/" + repo + "/issues",
                json={
                    "assignees": assignees,
                    "body": body,
                    "closed": issue.state == "closed",
                    "due_on": due_date,
                    "labels": labels,
                    "milestone": milestone,
                    "title": issue.title,
                },
                params=params,
            )
            if import_response.ok:
                print_info("Issue " + issue.title + " imported!")
                gitea_issue = json.loads(import_response.text)
            else:
                print_error(
                    "Issue " + issue.title + " import failed: " + import_response.text
                )
                continue

            # Find and handle markdown image links in the issue description
            description = body
            description_old = description
            description = replace_issue_links(description, GITLAB_URL, GITEA_URL)

            image_links = re.findall(
                r"\[.*?\]\((/uploads/.*?)\)", issue.description or ""
            )
            for image_link in image_links:
                attachment_url = (
                    GITLAB_API_BASEURL + "/projects/" + str(project.id) + image_link
                )
                attachment_response = requests.get(
                    attachment_url, headers={"PRIVATE-TOKEN": GITLAB_TOKEN}
                )
                if attachment_response.ok:
                    tmp_path = f"/tmp/gitlab_to_gitea/{os.path.basename(image_link)}"
                    with open(tmp_path, "wb") as file:
                        file.write(attachment_response.content)
                    print("Image downloaded successfully!")
                    url = f'{GITEA_API_BASEURL}/repos/{owner}/{repo}/issues/{str(gitea_issue["number"])}/assets'
                    headers = {"Authorization": f"token {GITEA_TOKEN}"}
                    files = {"attachment": open(tmp_path, "rb")}
                    upload_response = requests.post(url, headers=headers, files=files)
                    os.remove(tmp_path)
                    if upload_response.ok:
                        print_info(
                            "Attachment " + os.path.basename(image_link) + " uploaded!"
                        )
                        # Replace the image link in the description with the new link
                        new_image_link = upload_response.json()["browser_download_url"]
                        description = description.replace(image_link, new_image_link)
                    else:
                        print_error(
                            "Attachment "
                            + os.path.basename(image_link)
                            + " upload failed: "
                            + upload_response.text
                        )
                else:
                    print_error(
                        "Failed to download attachment "
                        + attachment_url
                        + " for issue "
                        + issue.title
                        + "!"
                    )

            if description != description_old:
                update_response: requests.Response = gitea_api.patch(
                    "/repos/"
                    + owner
                    + "/"
                    + repo
                    + "/issues/"
                    + str(gitea_issue["number"]),
                    json={"body": description},
                    params=params,
                )
                if update_response.ok:
                    print_info("Issue " + issue.title + " updated!")
                else:
                    print_error(
                        "Issue "
                        + issue.title
                        + " update failed: "
                        + update_response.text
                    )

        # import the comments for the issue
        _import_issue_comments(
            gitea_api,
            project.id,
            gitea_issue,
            owner,
            repo,
            notes,
            org_members,
            is_public,
        )


def _import_issue_comments(
    gitea_api: pygitea,
    project_id,
    issue,
    owner: string,
    repo: string,
    notes: List[gitlab.v4.objects.ProjectIssueNote],
    org_members: List[str],
    is_public: bool = False,
):
    for note in notes:
        short_comment_body = (
            (note.body[0:10] + "...") if len(note.body) > 10 else note.body
        )

        existing_comment = get_issue_comment(
            gitea_api, owner, repo, issue["url"], note.body
        )
        comment_id = existing_comment["id"] if existing_comment else None
        body = note.body

        if not existing_comment:
            created_at_utc = dateutil.parser.parse(note.created_at)
            created_at_local = created_at_utc.astimezone(
                pytz.timezone("Asia/Taipei")
            ).strftime("%d.%m.%Y %H:%M")
            body = f"{note.body}\n\n{created_at_local}"
            body = replace_issue_links(body, GITLAB_URL, GITEA_URL)

            params = {}
            if is_public or note.author["username"] in org_members:
                params["sudo"] = note.author["username"]
            else:
                body = f"Autor: {note.author['name']}\n\n{body}"

            import_response: requests.Response = gitea_api.post(
                "/repos/"
                + owner
                + "/"
                + repo
                + "/issues/"
                + str(issue["number"])
                + "/comments",
                json={
                    "body": body,
                },
                params=params,
            )
            if import_response.ok:
                comment_id = json.loads(import_response.text)["id"]
                print_info("Issue comment " + short_comment_body + " imported!")
            else:
                print_error(
                    "Issue comment "
                    + short_comment_body
                    + " import failed: "
                    + import_response.text
                )

        if not comment_id:
            print_warning(
                "Failed to load comment id for comment " + short_comment_body + "!"
            )
            continue

        # Find and handle markdown image links in the comment body
        comment_body = body
        comment_body_old = comment_body
        comment_body = replace_issue_links(comment_body, GITLAB_URL, GITEA_URL)

        image_links = re.findall(r"\[.*?\]\((/uploads/.*?)\)", note.body or "")
        for image_link in image_links:
            attachment_url = (
                GITLAB_API_BASEURL + "/projects/" + str(project_id) + image_link
            )
            attachment_response = requests.get(
                attachment_url, headers={"PRIVATE-TOKEN": GITLAB_TOKEN}
            )
            if attachment_response.ok:
                tmp_path = f"/tmp/gitlab_to_gitea/{os.path.basename(image_link)}"
                with open(tmp_path, "wb") as file:
                    file.write(attachment_response.content)
                print("Image downloaded successfully!")
                url = f"{GITEA_API_BASEURL}/repos/{owner}/{repo}/issues/comments/{comment_id}/assets"
                headers = {"Authorization": f"token {GITEA_TOKEN}"}
                files = {"attachment": open(tmp_path, "rb")}
                upload_response = requests.post(url, headers=headers, files=files)
                os.remove(tmp_path)
                if upload_response.ok:
                    print_info(
                        "Attachment " + os.path.basename(image_link) + " uploaded!"
                    )
                    # Replace the image link in the comment body with the new link
                    new_image_link = upload_response.json()["browser_download_url"]
                    comment_body = comment_body.replace(image_link, new_image_link)
                else:
                    print_error(
                        "Attachment "
                        + os.path.basename(image_link)
                        + " upload failed: "
                        + upload_response.text
                    )
            else:
                print_error(
                    "Failed to download attachment "
                    + attachment_url
                    + " for comment "
                    + note.body
                    + "!"
                )

        if comment_body != comment_body_old:
            update_response: requests.Response = gitea_api.patch(
                "/repos/" + owner + "/" + repo + "/issues/comments/" + str(comment_id),
                json={"body": comment_body},
                params=params,
            )
            if update_response.ok:
                print_info("Comment " + short_comment_body + " updated!")
            else:
                print_error(
                    "Comment "
                    + short_comment_body
                    + " update failed: "
                    + update_response.text
                )


def _import_project_repo(gitea_api: pygitea, project: gitlab.v4.objects.Project):
    if not repo_exists(
        gitea_api, name_clean(project.namespace["name"]), name_clean(project.name)
    ):
        clone_url = project.http_url_to_repo
        if GITLAB_ADMIN_PASS == "" and GITLAB_ADMIN_USER == "":
            clone_url = project.ssh_url_to_repo
        private = project.visibility == "private" or project.visibility == "limited"

        # Load the owner (users and groups can both be fetched using the /users/ endpoint)
        owner = get_user_or_group(gitea_api, project)
        if owner:
            description = project.description

            if description is not None and len(description) > 255:
                description = description[:255]
                print_warning(
                    f"Description of {name_clean(project.name)} had to be truncated to 255 characters!"
                )

            import_response: requests.Response = gitea_api.post(
                "/repos/migrate",
                json={
                    "auth_password": GITLAB_ADMIN_PASS,
                    "auth_token": GITLAB_TOKEN,
                    "auth_username": GITLAB_ADMIN_USER,
                    "clone_addr": clone_url,
                    "description": description,
                    "mirror": REPOSITORY_MIRROR,
                    "private": private,
                    "repo_name": name_clean(project.name),
                    "uid": owner["id"],
                    "issues": True,
                    "labels": True,
                    "lfs": True,
                    "milestones": True,
                    "pull_requests": True,
                    "service": "gitlab",
                    "wiki": True,
                },
            )
            if import_response.ok:
                print_info("Project " + name_clean(project.name) + " imported!")

                # Archive the repository if it's archived in GitLab and REPOSITORY_MIRROR is False
                if getattr(project, "archived", False) and not REPOSITORY_MIRROR:
                    archive_response: requests.Response = gitea_api.patch(
                        "/repos/"
                        + name_clean(project.namespace["name"])
                        + "/"
                        + name_clean(project.name),
                        json={"archived": True},
                    )
                    if archive_response.ok:
                        print_info(
                            "Project "
                            + name_clean(project.name)
                            + " archived in Gitea!"
                        )
                    else:
                        print_error(
                            "Project "
                            + name_clean(project.name)
                            + " archiving failed: "
                            + archive_response.text
                        )
            else:
                print_error(
                    "Project "
                    + name_clean(project.name)
                    + " import failed: "
                    + import_response.text
                )
        else:
            print_error(
                "Failed to load project owner for project " + name_clean(project.name)
            )


def _import_project_repo_collaborators(
    gitea_api: pygitea,
    collaborators: [gitlab.v4.objects.ProjectMember],
    project: gitlab.v4.objects.Project,
):
    for collaborator in collaborators:

        if not collaborator_exists(
            gitea_api,
            name_clean(project.namespace["name"]),
            name_clean(project.name),
            collaborator.username,
        ):
            permission = "read"

            if collaborator.access_level == 10:  # guest access
                permission = "read"
            elif collaborator.access_level == 20:  # reporter access
                permission = "read"
            elif collaborator.access_level == 30:  # developer access
                permission = "write"
            elif collaborator.access_level == 40:  # maintainer access
                permission = "admin"
            elif collaborator.access_level == 50:  # owner access (only for groups)
                print_error("Groupmembers are currently not supported!")
                continue  # groups are not supported
            else:
                print_warning(
                    "Unsupported access level "
                    + str(collaborator.access_level)
                    + ", setting permissions to 'read'!"
                )

            import_response: requests.Response = gitea_api.put(
                "/repos/"
                + name_clean(project.namespace["name"])
                + "/"
                + name_clean(project.name)
                + "/collaborators/"
                + collaborator.username,
                json={"permission": permission},
            )
            if import_response.ok:
                print_info("Collaborator " + collaborator.username + " imported!")
            else:
                print_error(
                    "Collaborator "
                    + collaborator.username
                    + " import failed: "
                    + import_response.text
                )


def _import_users(
    gitea_api: pygitea, users: [gitlab.v4.objects.User], notify: bool = False
):
    with open("created_users.txt", "a") as f:
        for user in users:
            keys: [gitlab.v4.objects.UserKey] = user.keys.list(all=True)

            print("Importing user " + user.username + "...")
            print("Found " + str(len(keys)) + " public keys for user " + user.username)

            if not user_exists(gitea_api, user.username):
                tmp_password = "Tmp1!" + "".join(
                    random.choices(string.ascii_uppercase + string.digits, k=10)
                )

                tmp_email = (
                    user.username + "@noemail-git.local"
                )  # Some gitlab instances do not publish user emails
                try:
                    tmp_email = user.email
                except AttributeError:
                    pass
                import_response: requests.Response = gitea_api.post(
                    "/admin/users",
                    json={
                        "email": tmp_email,
                        "full_name": user.name,
                        "login_name": user.username,
                        "password": tmp_password,
                        "send_notify": notify,
                        "source_id": 0,  # local user
                        "username": user.username,
                        "visibility": "limited",
                    },
                )
                if import_response.ok:
                    print_info(
                        "User "
                        + user.username
                        + " imported, temporary password: "
                        + tmp_password
                    )
                    f.write(f"{user.username},{tmp_password}\n")
                else:
                    print_error(
                        "User "
                        + user.username
                        + " import failed: "
                        + import_response.text
                    )

                # Download and upload user avatar
                if user.avatar_url:
                    avatar_response = requests.get(user.avatar_url)
                    if avatar_response.ok:
                        avatar_base64 = base64.b64encode(
                            avatar_response.content
                        ).decode("utf-8")
                        import_response: requests.Response = gitea_api.post(
                            "/user/avatar",
                            json={"image": avatar_base64},
                            params={"sudo": user.username},
                        )
                        if import_response.ok:
                            print_info(
                                "Avatar for user " + user.username + " uploaded!"
                            )
                        else:
                            print_error(
                                "Avatar for user "
                                + user.username
                                + " upload failed: "
                                + import_response.text
                            )
                    else:
                        print_error(
                            "Failed to download avatar for user " + user.username + "!"
                        )

            # import public keys
            _import_user_keys(gitea_api, keys, user)


def _import_user_keys(
    gitea_api: pygitea, keys: [gitlab.v4.objects.UserKey], user: gitlab.v4.objects.User
):
    for key in keys:
        if not user_key_exists(gitea_api, user.username, key.title):
            import_response: requests.Response = gitea_api.post(
                "/admin/users/" + user.username + "/keys",
                json={
                    "key": key.key,
                    "read_only": True,
                    "title": key.title,
                },
            )
            if import_response.ok:
                print_info("Public key " + key.title + " imported!")
            else:
                print_error(
                    "Public key "
                    + key.title
                    + " import failed: "
                    + import_response.text
                )


def _import_groups(gitea_api: pygitea, groups: [gitlab.v4.objects.Group]):
    for group in groups:
        try:
            members: [gitlab.v4.objects.GroupMember] = group.members_all.list(all=True)
            labels: [gitlab.v4.objects.GroupLabel] = group.labels.list(all=True)
        except Exception as e:
            print(
                "Skipping group member import for group "
                + group.full_path
                + " due to error: "
                + str(e)
            )
            continue
        print("Importing group " + name_clean(group.name) + "...")
        print(
            "Found "
            + str(len(members))
            + " gitlab members for group "
            + name_clean(group.name)
        )

        if not organization_exists(gitea_api, name_clean(group.name)):
            import_response: requests.Response = gitea_api.post(
                "/orgs",
                json={
                    "description": group.description,
                    "full_name": group.full_name,
                    "location": "",
                    "username": name_clean(group.name),
                    "website": "",
                    "visibility": "limited",
                },
            )
            if import_response.ok:
                print_info("Group " + name_clean(group.name) + " imported!")
            else:
                print_error(
                    "Group "
                    + name_clean(group.name)
                    + " import failed: "
                    + import_response.text
                )

        # import group members
        _import_group_members(gitea_api, members, group)

        _import_group_labels(gitea_api, labels, group)


def _import_group_members(
    gitea_api: pygitea,
    members: [gitlab.v4.objects.GroupMember],
    group: gitlab.v4.objects.Group,
):
    # TODO: create teams based on gitlab permissions (access_level of group member)
    existing_teams = get_teams(gitea_api, name_clean(group.name))
    if existing_teams:
        first_team = existing_teams[0]
        print(
            "Organization teams fetched, importing users to first team: "
            + first_team["name"]
        )

        # if members empty just add the fallback user
        if len(members) == 0:
            members = [{"username": GITEA_FALLBACK_GROUP_MEMBER}]
        # add members to teams
        for member in members:
            if not member_exists(gitea_api, member.username, first_team["id"]):
                import_response: requests.Response = gitea_api.put(
                    "/teams/" + str(first_team["id"]) + "/members/" + member.username
                )
                if import_response.ok:
                    print_info(
                        "Member "
                        + member.username
                        + " added to group "
                        + name_clean(group.name)
                        + "!"
                    )
                else:
                    print_error(
                        "Failed to add member "
                        + member.username
                        + " to group "
                        + name_clean(group.name)
                        + "!"
                    )
    else:
        print_error(
            "Failed to import members to group "
            + name_clean(group.name)
            + ": no teams found!"
        )


def _import_group_labels(
    gitea_api: pygitea,
    labels: [gitlab.v4.objects.GroupLabel],
    group: gitlab.v4.objects.Group,
):
    group_labels = get_group_labels(gitea_api, name_clean(group.name))
    for label in labels:
        if label.name not in group_labels:
            import_response: requests.Response = gitea_api.post(
                "/orgs/" + name_clean(group.name) + "/labels",
                json={
                    "color": label.color,
                    "description": label.description,
                    "name": label.name,
                },
            )
            if import_response.ok:
                print_info("Label " + label.name + " imported!")
            else:
                print_error(
                    "Label " + label.name + " import failed: " + import_response.text
                )


#
# Import functions
#


def import_users_groups(
    gitlab_api: gitlab.Gitlab,
    gitea_api: pygitea,
    users: List[gitlab.v4.objects.User],
    groups: List[gitlab.v4.objects.Group],
    notify=False,
):
    print(
        "Found " + str(len(users)) + " gitlab users as user " + gitlab_api.user.username
    )
    print(
        "Found "
        + str(len(groups))
        + " gitlab groups as user "
        + gitlab_api.user.username
    )

    # import all non existing users
    _import_users(gitea_api, users, notify)

    # import all non existing groups
    _import_groups(gitea_api, groups)


def import_projects(
    gitlab_api: gitlab.Gitlab,
    gitea_api: pygitea,
    projects: List[gitlab.v4.objects.Project],
):
    print(
        "Found "
        + str(len(projects))
        + " gitlab projects as user "
        + gitlab_api.user.username
    )

    for project in projects:
        if GITLAB_ARCHIVE_MIGRATED_PROJECTS:
            try:
                project.archive()
            except Exception as e:
                print(
                    "WARNING: Failed to archive project '{}', reason: {}".format(
                        project.name, e
                    )
                )

        try:
            collaborators: [gitlab.v4.objects.ProjectMember] = project.members.list(
                get_all=True
            )
            labels: [gitlab.v4.objects.ProjectLabel] = project.labels.list(get_all=True)
            milestones: [gitlab.v4.objects.ProjectMilestone] = project.milestones.list(
                get_all=True
            )
            issues: [gitlab.v4.objects.ProjectIssue] = sorted(
                project.issues.list(get_all=True), key=lambda x: x.iid
            )

            print(
                "Importing project "
                + name_clean(project.name)
                + " from owner "
                + name_clean(project.namespace["name"])
            )
            print(
                "Found "
                + str(len(collaborators))
                + " collaborators for project "
                + name_clean(project.name)
            )
            print(
                "Found "
                + str(len(labels))
                + " labels for project "
                + name_clean(project.name)
            )
            print(
                "Found "
                + str(len(milestones))
                + " milestones for project "
                + name_clean(project.name)
            )
            print(
                "Found "
                + str(len(issues))
                + " issues for project "
                + name_clean(project.name)
            )

        except Exception as e:
            print("This project failed: \n {}, \n reason {}: ".format(project.name, e))

        else:
            projectOwner = name_clean(project.namespace["name"])
            projectName = name_clean(project.name)

            # import project repo
            _import_project_repo(gitea_api, project)

            # import collaborators
            _import_project_repo_collaborators(gitea_api, collaborators, project)

            # import labels
            _import_project_labels(
                gitea_api, project, labels, projectOwner, projectName
            )

            # import milestones
            _import_project_milestones(gitea_api, milestones, projectOwner, projectName)

            # import issues
            # _import_project_issues(gitea_api, project, issues, projectOwner, projectName)


def get_gitea_users(cursor):
    """
    從 Gitea 資料庫取得所有使用者
    回傳 dict: {lower_name: user_id}
    """
    cursor.execute(
        'SELECT id, lower_name FROM "user" WHERE type = 0;'
    )  # type=0 通常是一般使用者
    gitea_users = {}
    for row in cursor.fetchall():
        user_id, lower_name = row
        if lower_name:
            gitea_users[lower_name] = user_id
    return gitea_users


def get_existing_mappings(cursor):
    """
    取得已經存在 external_login_user 表中的對應關係
    回傳 set: {(external_id, login_source_id)}
    """
    cursor.execute(
        "SELECT external_id, login_source_id FROM external_login_user WHERE provider = %s;",
        (PROVIDER_NAME,),
    )
    return set(cursor.fetchall())


def map_users():
    print("---=== GitLab to Gitea User Mapping ===---")

    # 1. 連線到 GitLab
    try:
        gl = gitlab.Gitlab(GITLAB_URL, private_token=GITLAB_TOKEN)
        gl.auth()
        print(f"Connected to GitLab: {GITLAB_URL}")
    except Exception as e:
        print(f"Failed to connect to GitLab: {e}")
        raise e

    # 2. 連線到 Gitea 資料庫
    try:
        conn = psycopg2.connect(
            host=PG_HOST, port=PG_PORT, dbname=PG_NAME, user=PG_USER, password=PG_PASS
        )
        cursor = conn.cursor()
        print(f"Connected to Gitea Database: {PG_NAME}")
    except Exception as e:
        print(f"Failed to connect to Gitea Database: {e}")
        raise e

    # 3. 取得兩邊的資料
    print("Fetching users from Gitea database...")
    gitea_users_by_username = get_gitea_users(cursor)
    print(f"Found {len(gitea_users_by_username)} users in Gitea.")

    print("Fetching existing mappings...")
    existing_mappings = get_existing_mappings(cursor)

    print("Fetching users from GitLab...")
    # 注意：如果 GitLab 人數非常多，請考慮分頁或使用 iterator
    gitlab_users = gl.users.list(all=True)
    print(f"Found {len(gitlab_users)} users in GitLab.")

    # 4. 進行比對與寫入
    insert_query = """
        INSERT INTO external_login_user 
        (external_id, user_id, login_source_id, provider, email, name, first_name, last_name, nick_name, description, avatar_url, location, access_token, access_token_secret, refresh_token) 
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """

    mapped_count = 0
    for gl_user in gitlab_users:
        if not gl_user.username:
            continue

        username = gl_user.username.lower()
        external_id = str(gl_user.id)

        # 檢查是否已經 mapping 過了
        if (external_id, LOGIN_SOURCE_ID) in existing_mappings:
            print(
                f"User {gl_user.username} (GitLab ID: {external_id}) is already mapped. Skipping."
            )
            continue

        # 檢查 Gitea 是否有這個 username
        if username in gitea_users_by_username:
            gitea_user_id = gitea_users_by_username[username]

            try:
                # 寫入資料庫
                # 注意：這裡補上 Gitea external_login_user 表所需的欄位，空值給空字串
                cursor.execute(
                    insert_query,
                    (
                        external_id,
                        gitea_user_id,
                        LOGIN_SOURCE_ID,
                        PROVIDER_NAME,
                        gl_user.email or "",
                        gl_user.username,
                        "",  # first_name
                        "",  # last_name
                        gl_user.name,  # nick_name
                        "",  # description
                        gl_user.avatar_url or "",
                        "",  # location
                        "",  # access_token
                        "",  # access_token_secret
                        "",  # refresh_token
                    ),
                )
                mapped_count += 1
                print(
                    f"Mapped GitLab user {gl_user.username} (ID: {external_id}) to Gitea user ID: {gitea_user_id}"
                )
            except Exception as e:
                print(f"Error inserting mapping for {gl_user.username}: {e}")
                conn.rollback()  # 發生錯誤時 rollback
                continue
        else:
            print(f"GitLab user {gl_user.username} not found in Gitea. Skipping.")

    # 5. 提交並關閉連線
    if mapped_count > 0:
        conn.commit()
        print(f"Successfully mapped {mapped_count} users.")
    else:
        print("No new users were mapped.")

    cursor.close()
    conn.close()


def truncate_all(gitea_api: pygitea):
    print("Truncate all projects, organizations, and users!")

    # Get all users
    users_response = gitea_api.get("/admin/users")
    users = json.loads(users_response.text)
    for user in users:
        # Delete user packages
        packages_response = gitea_api.get(f'/packages/{user["login"]}')
        if packages_response.ok:
            packages = json.loads(packages_response.text)
            for pkg in packages:
                # API format: DELETE /api/v1/packages/{owner}/{type}/{name}/{version}
                pkg_delete_response = gitea_api.delete(
                    f'/packages/{user["login"]}/{pkg["type"]}/{pkg["name"]}/{pkg["version"]}'
                )
                if pkg_delete_response.ok:
                    print_info(
                        f'Package {user["login"]}/{pkg["type"]}/{pkg["name"]}/{pkg["version"]} deleted!'
                    )
                else:
                    print_error(
                        f'Package {user["login"]}/{pkg["type"]}/{pkg["name"]}/{pkg["version"]} deletion failed: {pkg_delete_response.text}'
                    )

        # Delete user repositories
        user_repos_response = gitea_api.get(f'/users/{user["login"]}/repos')
        user_repos = json.loads(user_repos_response.text)
        for repo in user_repos:
            repo_delete_response = gitea_api.delete(
                f'/repos/{repo["owner"]["login"]}/{repo["name"]}'
            )
            if repo_delete_response.ok:
                print_info(
                    "Repository "
                    + repo["owner"]["login"]
                    + "/"
                    + repo["name"]
                    + " deleted!"
                )
            else:
                print_error(
                    "Repository "
                    + repo["owner"]["login"]
                    + "/"
                    + repo["name"]
                    + " deletion failed: "
                    + repo_delete_response.text
                )

    # Get all organizations
    organizations_response = gitea_api.get("/orgs")
    organizations = json.loads(organizations_response.text)
    for org in organizations:
        # Delete organization packages
        packages_response = gitea_api.get(f'/packages/{org["username"]}')
        if packages_response.ok:
            packages = json.loads(packages_response.text)
            for pkg in packages:
                # API format: DELETE /api/v1/packages/{owner}/{type}/{name}/{version}
                pkg_delete_response = gitea_api.delete(
                    f'/packages/{org["username"]}/{pkg["type"]}/{pkg["name"]}/{pkg["version"]}'
                )
                if pkg_delete_response.ok:
                    print_info(
                        f'Package {org["username"]}/{pkg["type"]}/{pkg["name"]}/{pkg["version"]} deleted!'
                    )
                else:
                    print_error(
                        f'Package {org["username"]}/{pkg["type"]}/{pkg["name"]}/{pkg["version"]} deletion failed: {pkg_delete_response.text}'
                    )

        # Delete organization repositories
        org_repos_response = gitea_api.get(f'/orgs/{org["username"]}/repos')
        org_repos = json.loads(org_repos_response.text)
        for repo in org_repos:
            repo_delete_response = gitea_api.delete(
                f'/repos/{repo["owner"]["login"]}/{repo["name"]}'
            )
            if repo_delete_response.ok:
                print_info(
                    "Repository "
                    + repo["owner"]["login"]
                    + "/"
                    + repo["name"]
                    + " deleted!"
                )
            else:
                print_error(
                    "Repository "
                    + repo["owner"]["login"]
                    + "/"
                    + repo["name"]
                    + " deletion failed: "
                    + repo_delete_response.text
                )
        # Delete organization
        orga_delete_response = gitea_api.delete(f'/orgs/{org["username"]}')
        if orga_delete_response.ok:
            print_info("Organization " + org["username"] + " deleted!")
        else:
            print_error(
                "Organization "
                + org["username"]
                + " deletion failed: "
                + orga_delete_response.text
            )

    for user in users:
        # Delete user
        user_delete_response = gitea_api.delete(f'/admin/users/{user["login"]}')
        if user_delete_response.ok:
            print_info("User " + user["login"] + " deleted!")
        else:
            print_error(
                "User "
                + user["login"]
                + " deletion failed: "
                + user_delete_response.text
            )


#
# Helper functions
#


class bcolors:
    HEADER = "\033[95m"
    OKBLUE = "\033[94m"
    OKGREEN = "\033[92m"
    WARNING = "\033[93m"
    FAIL = "\033[91m"
    ENDC = "\033[0m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"


def color_message(color, message, colorend=bcolors.ENDC, bold=False):
    if bold:
        return bcolors.BOLD + color_message(color, message, colorend, False)

    return color + message + colorend


def print_color(color, message, colorend=bcolors.ENDC, bold=False):
    print(color_message(color, message, colorend))


def print_info(message):
    print_color(bcolors.OKBLUE, message)


def print_success(message):
    print_color(bcolors.OKGREEN, message)


def print_warning(message):
    print_color(bcolors.WARNING, message)


def print_error(message):
    global GLOBAL_ERROR_COUNT
    GLOBAL_ERROR_COUNT += 1
    print_color(bcolors.FAIL, message)


def name_clean(name):
    newName = name.replace(" ", "")
    newName = newName.replace("ä", "ae")
    newName = newName.replace("ö", "oe")
    newName = newName.replace("ü", "ue")
    newName = newName.replace("Ä", "Ae")
    newName = newName.replace("Ö", "Oe")
    newName = newName.replace("Ü", "Ue")
    newName = re.sub(r"[^a-zA-Z0-9_\.-]", "-", newName)

    if newName.lower() == "plugins":
        return newName + "-user"

    return newName


def replace_issue_links(text: str, gitlab_url: str, gitea_url: str) -> str:
    pattern = re.escape(gitlab_url) + r"/([^/]+)/([^/]+)/([^/]+)/-/issues/(\d+)"
    replacement = gitea_url + r"/\2/\3/issues/\4"
    text = re.sub(pattern, replacement, text or "")
    pattern = re.escape(gitlab_url) + r"/([^/]+)/([^/]+)/-/issues/(\d+)"
    replacement = gitea_url + r"/\1/\2/issues/\3"
    text = re.sub(pattern, replacement, text or "")
    return text


if __name__ == "__main__":
    main()
