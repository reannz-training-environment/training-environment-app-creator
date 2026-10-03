"""Create an app's repository, or open a pull request that updates it."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import render
from .spec import INTERFACES, Spec

API = "https://api.github.com"
UPDATE_BRANCH = "app-creator/update"
BOT_NAME = "training-environment-app-creator[bot]"
BOT_EMAIL = "training-environment-app-creator@users.noreply.github.com"


class PublishError(Exception):
    pass


@dataclass
class Result:
    interface: str
    repo: str
    action: str  # created | updated | merged | unchanged | would-create | would-update | failed
    url: str
    detail: str = ""
    # the release this starts: the repository's Release workflow tags `tag` on
    # commit `sha` of main and builds its image. Empty when nothing is released.
    tag: str = ""
    sha: str = ""


class GitHub:
    """The few REST calls publishing needs."""

    def __init__(self, token: str):
        self.token = token

    def request(self, method: str, path: str, data: Any = None) -> tuple[int, Any]:
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(API + path, data=body, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                payload = json.loads(raw) if raw else None
            except ValueError:
                payload = raw.decode(errors="replace")
            return exc.code, payload

    def call(self, method: str, path: str, data: Any = None, ok: tuple[int, ...] = (200, 201, 204)) -> Any:
        status, payload = self.request(method, path, data)
        if status not in ok:
            message = payload.get("message") if isinstance(payload, dict) else payload
            raise PublishError(f"GitHub API {method} {path} returned {status}: {message}")
        return payload

    def repo(self, full_name: str) -> dict | None:
        status, payload = self.request("GET", f"/repos/{full_name}")
        if status == 404:
            return None
        if status != 200:
            raise PublishError(f"cannot look up {full_name}: HTTP {status}")
        return payload

    def comment(self, full_name: str, number: int, body: str, marker: str | None = None) -> dict:
        """Comment on an issue or pull request. With a marker, replace the
        comment that starts with it instead of adding another."""
        if marker:
            body = f"{marker}\n{body}"
            comments = self.call("GET", f"/repos/{full_name}/issues/{number}/comments?per_page=100")
            mine = [c for c in comments if (c.get("body") or "").startswith(marker)]
            if mine:
                return self.call("PATCH", f"/repos/{full_name}/issues/comments/{mine[0]['id']}", {"body": body})
        return self.call("POST", f"/repos/{full_name}/issues/{number}/comments", {"body": body})


class Git:
    """git in one working directory, authenticated by header rather than URL."""

    def __init__(self, workdir: Path, token: str | None, protocol: str = "https"):
        self.workdir = workdir
        self.token = token
        self.protocol = protocol

    def remote(self, full_name: str) -> str:
        if self.protocol == "ssh":
            return f"git@github.com:{full_name}.git"
        return f"https://github.com/{full_name}.git"

    def run(self, *args: str, check: bool = True, capture: bool = False) -> str:
        cmd = ["git"]
        if self.token and self.protocol == "https":
            basic = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
            cmd += ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {basic}"]
        cmd += list(args)
        env = dict(os.environ)
        env.setdefault("GIT_AUTHOR_NAME", BOT_NAME)
        env.setdefault("GIT_AUTHOR_EMAIL", BOT_EMAIL)
        env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
        env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])
        env["GIT_TERMINAL_PROMPT"] = "0"
        proc = subprocess.run(cmd, cwd=self.workdir, env=env, text=True, capture_output=True)
        if check and proc.returncode != 0:
            # never echo the command line: it carries the token
            raise PublishError(f"git {args[0]} failed: {proc.stderr.strip() or proc.stdout.strip()}")
        return proc.stdout if capture else ""


def _commit_message(spec: Spec, interface: str, creator_sha: str | None, first: bool) -> str:
    verb = "Create" if first else "Update"
    title = f"{verb} {spec.app['title']} ({INTERFACES[interface]['label']}) v{spec.app['version']}"
    source = f"apps/{spec.name}.yml"
    if creator_sha:
        source += f" at {creator_sha[:12]}"
    return f"{title}\n\nGenerated by training-environment-app-creator from {source}.\n"


def _pr_body(
    spec: Spec,
    interface: str,
    defaults: dict[str, Any],
    creator_sha: str | None,
    changed: list[str],
    released: bool,
    merge: bool = False,
) -> str:
    org = defaults["github_org"]
    creator = f"https://github.com/{org}/training-environment-app-creator"
    source = f"[`apps/{spec.name}.yml`]({creator}/blob/{creator_sha or 'main'}/apps/{spec.name}.yml)"
    lines = [f"The app creator regenerated this app from {source}."]
    if merge:
        lines.append(
            "That change was approved when it was merged into the app creator, so the app creator "
            "merges this pull request itself; it is here as a record of what changed."
        )
    lines += [
        "",
        "Changed files:",
        "",
        *[f"* `{path}`" for path in changed],
        "",
    ]
    if released:
        lines += [
            f"The spec's version is new, so merging this releases **v{spec.app['version']}**: "
            "the *Release* workflow tags the merge commit and builds that image. Then update "
            "`k8s_container` and `version` for this app in the training-environment config.",
        ]
    else:
        lines += [
            f"`submit.yml.erb` still pins **v{spec.app['version']}**, which is already released, so "
            "sessions keep running the old image after this is merged. To release these changes, "
            "bump `version` in the app spec.",
        ]
    lines += [
        "",
        "Edits made to this branch are overwritten by the next update from the app creator; "
        "change the app spec instead.",
    ]
    return "\n".join(lines) + "\n"


def _tag_exists(gh: GitHub, full_name: str, tag: str) -> bool:
    status, _ = gh.request("GET", f"/repos/{full_name}/git/ref/tags/{tag}")
    return status == 200


def _merge(gh: GitHub, full_name: str, number: int, title: str, log, sleep=time.sleep) -> str:
    """Squash-merge a pull request and return the merge commit. A pull request
    that was only just pushed can briefly be unmergeable while GitHub works out
    whether it is, so this tries a few times."""
    for attempt in range(6):
        status, payload = gh.request(
            "PUT",
            f"/repos/{full_name}/pulls/{number}/merge",
            {"merge_method": "squash", "commit_title": f"{title} (#{number})"},
        )
        if status == 200:
            log(f"merged {full_name}#{number}")
            return payload.get("sha", "")
        if status in (405, 409) and attempt < 5:
            sleep(2 * (attempt + 1))
            continue
        message = payload.get("message") if isinstance(payload, dict) else payload
        raise PublishError(f"could not merge {full_name}#{number}: HTTP {status}: {message}")
    raise AssertionError("unreachable")


def publish_interface(
    spec: Spec,
    interface: str,
    defaults: dict[str, Any],
    token: str,
    *,
    dry_run: bool = False,
    merge: bool = False,
    creator_sha: str | None = None,
    git_protocol: str = "https",
    log=print,
) -> Result:
    """Create the interface's repository, or bring it up to date.

    An existing repository gets a pull request with the regenerated files;
    with ``merge``, the app creator merges it too, for when the change was
    already approved in the app creator."""
    org = defaults["github_org"]
    name = render.repo_name(defaults, interface, spec.name)
    full_name = f"{org}/{name}"
    url = f"https://github.com/{full_name}"
    gh = GitHub(token)
    files = render.render(spec, interface, defaults)
    existing = gh.repo(full_name)

    if existing is None:
        if dry_run:
            return Result(interface, full_name, "would-create", url, f"{len(files)} files")
        return _create(gh, spec, interface, defaults, files, full_name, url, token, creator_sha, git_protocol, log)
    if dry_run:
        return Result(interface, full_name, "would-update", url, "exists; an update pull request would be opened if anything changed")
    return _update(gh, spec, interface, defaults, files, existing, full_name, url, token, creator_sha, git_protocol, log, merge)


def _create(gh, spec, interface, defaults, files, full_name, url, token, creator_sha, git_protocol, log) -> Result:
    org, name = full_name.split("/")
    label = INTERFACES[interface]["label"]
    log(f"creating {full_name}")
    settings = {
        "name": name,
        "description": f"{spec.app['title']} ({label}) - REANNZ training environment app"[:350],
        "visibility": spec.app["visibility"],
        "has_wiki": False,
        "has_projects": False,
        "auto_init": False,
    }
    if spec.app.get("workshop_url"):
        settings["homepage"] = spec.app["workshop_url"]
    gh.call("POST", f"/orgs/{org}/repos", settings)
    gh.request(
        "PUT",
        f"/repos/{full_name}/topics",
        {"names": ["training-environment", "open-ondemand", interface, "app-creator"]},
    )
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        render.write(files, workdir)
        git = Git(workdir, token, git_protocol)
        git.run("init", "-q", "-b", "main")
        git.run("add", "-A")
        git.run("commit", "-q", "-m", _commit_message(spec, interface, creator_sha, first=True))
        sha = git.run("rev-parse", "HEAD", capture=True).strip()
        git.run("push", "-q", git.remote(full_name), "HEAD:refs/heads/main")
    log(f"created {url}")
    tag = f"v{spec.app['version']}"
    return Result(interface, full_name, "created", url, f"{tag} is released by its Release workflow", tag=tag, sha=sha)


def _update(gh, spec, interface, defaults, files, existing, full_name, url, token, creator_sha, git_protocol, log, merge=False) -> Result:
    base = existing.get("default_branch") or "main"
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        git = Git(workdir, token, git_protocol)
        git.run("init", "-q")
        git.run("fetch", "-q", "--depth", "1", git.remote(full_name), base)
        git.run("checkout", "-q", "-B", UPDATE_BRANCH, "FETCH_HEAD")

        old = render.read_manifest(workdir)
        if old is None:
            raise PublishError(
                f"{full_name} exists but was not made by the app creator (it has no "
                f"{render.MANIFEST}); rename the app, or delete or rename that repository"
            )
        new_paths = {f.path for f in files}
        for path in old.get("files", []):
            if path not in new_paths and (workdir / path).is_file():
                (workdir / path).unlink()
        render.write(files, workdir)
        git.run("add", "-A")
        changed = [line[3:] for line in git.run("status", "--porcelain", capture=True).splitlines() if line.strip()]
        if not changed:
            log(f"{full_name} is up to date")
            return Result(interface, full_name, "unchanged", url, "already matches the spec")

        git.run("commit", "-q", "-m", _commit_message(spec, interface, creator_sha, first=False))
        git.run("push", "-q", "--force", git.remote(full_name), f"HEAD:refs/heads/{UPDATE_BRANCH}")

    tag = f"v{spec.app['version']}"
    released = not _tag_exists(gh, full_name, tag)
    title = f"Update from the app creator: {spec.app['title']} v{spec.app['version']}"
    body = _pr_body(spec, interface, defaults, creator_sha, sorted(changed), released, merge)
    org = full_name.split("/")[0]
    open_prs = gh.call("GET", f"/repos/{full_name}/pulls?state=open&head={org}:{UPDATE_BRANCH}")
    if open_prs:
        number = open_prs[0]["number"]
        gh.call("PATCH", f"/repos/{full_name}/pulls/{number}", {"title": title, "body": body})
        pr_url = open_prs[0]["html_url"]
        log(f"updated pull request {pr_url}")
    else:
        pr = gh.call(
            "POST",
            f"/repos/{full_name}/pulls",
            {"title": title, "head": UPDATE_BRANCH, "base": base, "body": body},
        )
        number = pr["number"]
        pr_url = pr["html_url"]
        log(f"opened pull request {pr_url}")
    if not merge:
        return Result(interface, full_name, "updated", pr_url, f"{len(changed)} files changed")

    sha = _merge(gh, full_name, number, title, log)
    # the next update pushes the branch afresh
    gh.request("DELETE", f"/repos/{full_name}/git/refs/heads/{UPDATE_BRANCH}")
    detail = f"{len(changed)} files changed; " + (
        f"{tag} is released by its Release workflow" if released else f"{tag} was already released, so no new image"
    )
    return Result(interface, full_name, "merged", pr_url, detail, tag=tag if released else "", sha=sha if released else "")


def publish(spec: Spec, defaults: dict[str, Any], token: str, **kwargs) -> list[Result]:
    return [publish_interface(spec, interface, defaults, token, **kwargs) for interface in spec.app["interfaces"]]
