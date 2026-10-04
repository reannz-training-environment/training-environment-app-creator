"""Delete an app: its repositories, their images, and its spec in apps/.

Only repositories the app creator made for this app are deleted: each records
its app in .app-creator.json, and a repository without one, or made for
another app, is left alone, with its image.

GitHub's API for images does not accept a GitHub App's token, but it does
accept a repository's own workflow token for the image that repository's
workflows published. So before deleting a repository, the app creator adds a
workflow to it that deletes its image, runs it and waits for it. A personal
access token (classic) given as `packages` is used directly instead, and can
also delete the image of a repository that is already gone.

GitHub keeps a deleted repository for 90 days and a deleted image for 30, and
both can be restored from the organisation's settings in that time.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Any

from . import render
from .publish import GitHub
from .spec import INTERFACES

GENERATOR = "training-environment-app-creator"

# the workflow each repository deletes its own image with, started by a
# repository_dispatch event
IMAGE_WORKFLOW = ".github/workflows/delete-image.yml"
IMAGE_EVENT = "delete-image"
IMAGE_STEP = "Delete the image"
IMAGE_WORKFLOW_TEXT = """\
# Added by the app creator's "Delete an app" workflow just before it deletes
# this repository. GitHub lets a repository's own workflows delete the image
# they published, which the app creator's GitHub App may not do itself.
name: Delete the image

on:
  repository_dispatch:
    types: [delete-image]

permissions:
  packages: write

jobs:
  delete-image:
    runs-on: ubuntu-latest
    steps:
      - name: Find the image
        id: find
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          package="${GITHUB_REPOSITORY#*/}"
          package="${package,,}"
          echo "package=${package}" >> "$GITHUB_OUTPUT"
          if out=$(gh api "orgs/${GITHUB_REPOSITORY_OWNER}/packages/container/${package}" 2>&1); then
            echo "exists=true" >> "$GITHUB_OUTPUT"
          elif echo "${out}" | grep -q "HTTP 404"; then
            echo "There is no image to delete."
          else
            echo "${out}"
            exit 1
          fi
      - name: Delete the image
        if: steps.find.outputs.exists == 'true'
        env:
          GH_TOKEN: ${{ github.token }}
          PACKAGE: ${{ steps.find.outputs.package }}
        run: gh api -X DELETE "orgs/${GITHUB_REPOSITORY_OWNER}/packages/container/${PACKAGE}"
"""


@dataclass
class Removal:
    what: str  # repository | image | spec
    name: str
    result: str  # deleted | would delete | not found | kept | by hand | failed
    detail: str = ""


def _manifest(gh: GitHub, full_name: str, branch: str) -> dict | None:
    status, payload = gh.request("GET", f"/repos/{full_name}/contents/{render.MANIFEST}?ref={branch}")
    if status != 200 or not isinstance(payload, dict):
        return None
    try:
        return json.loads(base64.b64decode(payload["content"]))
    except ValueError:
        return None


def _names(org: str, package: str) -> tuple[str, str]:
    return f"ghcr.io/{org}/{package}", f"https://github.com/orgs/{org}/packages/container/{package}/settings"


def _image_with_token(packages: GitHub, org: str, package: str, dry_run: bool) -> Removal:
    """Delete an image with a personal access token (classic)."""
    image, settings = _names(org, package)
    status, payload = packages.request("GET" if dry_run else "DELETE", f"/orgs/{org}/packages/container/{package}")
    if status == (200 if dry_run else 204):
        return Removal("image", image, "would delete" if dry_run else "deleted")
    if status == 404:
        return Removal("image", image, "not found")
    if status in (401, 403):
        return Removal("image", image, "by hand", f"from [its settings]({settings}), under *Danger Zone*")
    message = payload.get("message") if isinstance(payload, dict) else payload
    return Removal("image", image, "failed", f"{status}: {message}; delete it from [its settings]({settings})")


def _image_by_repository(
    gh: GitHub,
    runs: GitHub | None,
    full: str,
    branch: str,
    dry_run: bool,
    log=print,
    clock=time.monotonic,
    sleep=time.sleep,
) -> Removal:
    """Have a repository delete its own image, with a workflow added to it.

    `runs` reads the repository's workflow runs, which a public repository
    shows to any token (the app creator's GitHub App has no Actions
    permission)."""
    org, name = full.split("/")
    image, settings = _names(org, name.lower())

    def by_hand(why: str) -> Removal:
        return Removal("image", image, "by hand", f"{why}; delete it from [its settings]({settings}), under *Danger Zone*")

    if dry_run:
        return Removal("image", image, "would delete", "by its repository's own workflow")
    if runs is None:
        return by_hand("its repository's workflows could not be followed")

    # [skip ci]: the push must not start the repository's image build, which
    # would put the image straight back
    put = {
        "message": "Add a workflow that deletes this app's image [skip ci]\n\n"
        "The app creator's Delete an app workflow runs it, then deletes this repository.\n",
        "content": base64.b64encode(IMAGE_WORKFLOW_TEXT.encode()).decode(),
        "branch": branch,
    }
    status, existing = gh.request("GET", f"/repos/{full}/contents/{IMAGE_WORKFLOW}?ref={branch}")
    if status == 200 and isinstance(existing, dict):
        put["sha"] = existing["sha"]
    status, _ = gh.request("PUT", f"/repos/{full}/contents/{IMAGE_WORKFLOW}", put)
    if status not in (200, 201):
        return by_hand(f"the workflow to delete it could not be added ({status})")

    def ours() -> list[dict]:
        status, payload = runs.request("GET", f"/repos/{full}/actions/runs?event=repository_dispatch&per_page=20")
        if status != 200 or not isinstance(payload, dict):
            return []
        return [r for r in payload.get("workflow_runs", []) if (r.get("path") or "").endswith(IMAGE_WORKFLOW)]

    seen = {r["id"] for r in ours()}
    run = None
    # GitHub can take a moment to notice a new workflow, and drops an event
    # sent before then, so the event is sent again until a run starts
    for _ in range(4):
        sleep(5)
        status, _ = gh.request("POST", f"/repos/{full}/dispatches", {"event_type": IMAGE_EVENT})
        if status != 204:
            return by_hand(f"its workflow could not be started ({status})")
        deadline = clock() + 30
        while run is None and clock() < deadline:
            new = [r for r in ours() if r["id"] not in seen]
            if new:
                run = min(new, key=lambda r: r["id"])
            else:
                sleep(3)
        if run is not None:
            break
    if run is None:
        return by_hand("its workflow did not start")
    log(f"{full}: deleting its image, {run.get('html_url', '')}")

    deadline = clock() + 600
    while run.get("status") != "completed" and clock() < deadline:
        sleep(5)
        status, payload = runs.request("GET", f"/repos/{full}/actions/runs/{run['id']}")
        if status == 200 and isinstance(payload, dict):
            run = payload
    if run.get("status") != "completed":
        return by_hand("its workflow was still running after 10 minutes")
    if run.get("conclusion") != "success":
        return by_hand("its workflow could not delete it")
    status, payload = runs.request("GET", f"/repos/{full}/actions/runs/{run['id']}/jobs")
    jobs = payload.get("jobs", []) if status == 200 and isinstance(payload, dict) else []
    step = next((s for job in jobs for s in job.get("steps", []) if s.get("name") == IMAGE_STEP), None)
    if step is not None and step.get("conclusion") == "skipped":
        return Removal("image", image, "not found")
    return Removal("image", image, "deleted")


def delete_app(
    name: str,
    defaults: dict[str, Any],
    creator_repo: str,
    gh: GitHub,
    packages: GitHub | None = None,
    runs: GitHub | None = None,
    dry_run: bool = False,
    log=print,
    clock=None,
    sleep=None,
) -> list[Removal]:
    """Delete the app's repositories, their images and its spec, or with
    dry_run, say what would be deleted.

    Each repository deletes its own image first, with a workflow the app
    creator adds to it; `runs` is a token that can read its workflow runs.
    With `packages`, a personal access token (classic), images are deleted
    with it instead, including those of repositories already gone."""
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    org = defaults["github_org"]
    removals = []
    for interface in INTERFACES:
        repo_name = render.repo_name(defaults, interface, name)
        full = f"{org}/{repo_name}"
        repo = gh.repo(full)
        if repo is None:
            # an image can outlive its repository; only a token for images can tell
            if packages is not None:
                removals.append(_image_with_token(packages, org, repo_name.lower(), dry_run))
            continue
        branch = repo.get("default_branch") or "main"
        manifest = _manifest(gh, full, branch)
        if not manifest or manifest.get("generator") != GENERATOR or manifest.get("app") != name:
            removals.append(Removal("repository", full, "kept", f"the app creator did not make it for {name}"))
            log(f"kept {full}: the app creator did not make it for {name}")
            continue

        # the image first: once the repository is gone, its workflows cannot delete it
        if packages is not None:
            image = _image_with_token(packages, org, repo_name.lower(), dry_run)
        else:
            image = _image_by_repository(gh, runs, full, branch, dry_run, log, clock, sleep)
        log(f"{image.result}: {image.name}")

        if dry_run:
            repository = Removal("repository", full, "would delete")
        else:
            status, payload = gh.request("DELETE", f"/repos/{full}")
            if status == 204:
                repository = Removal("repository", full, "deleted")
            else:
                message = payload.get("message") if isinstance(payload, dict) else payload
                repository = Removal("repository", full, "failed", f"{status}: {message}")
        log(f"{repository.result}: {full}")
        removals += [repository, image]

    path = f"apps/{name}.yml"
    base = gh.call("GET", f"/repos/{creator_repo}")["default_branch"]
    status, payload = gh.request("GET", f"/repos/{creator_repo}/contents/{path}?ref={base}")
    if status != 200:
        removals.append(Removal("spec", path, "not found"))
    elif dry_run:
        removals.append(Removal("spec", path, "would delete"))
    else:
        gh.call(
            "DELETE",
            f"/repos/{creator_repo}/contents/{path}",
            {
                "message": f"Delete the {name} app\n\nIts repositories and images were deleted by the Delete an app workflow.\n",
                "sha": payload["sha"],
                "branch": base,
            },
        )
        removals.append(Removal("spec", path, "deleted"))
        log(f"deleted {path}")
    return removals


_RESULTS = {
    "deleted": ":wastebasket: deleted",
    "would delete": "would be deleted",
    "not found": "not found",
    "kept": ":shield: kept",
    "by hand": ":point_right: delete it by hand",
    "failed": ":x: not deleted",
}


def markdown(name: str, removals: list[Removal], dry_run: bool) -> str:
    by_hand = [r for r in removals if r.result == "by hand"]
    if dry_run:
        head = f"## Dry run: what deleting the {name} app would do"
    elif any(r.result == "failed" for r in removals):
        head = f"## :warning: The {name} app is partly deleted"
    elif by_hand:
        head = f"## Deleted the {name} app, apart from some images"
    else:
        head = f"## Deleted the {name} app"
    lines = [head, "", "| | | |", "|---|---|---|"]
    for r in removals:
        thing = f"`{r.name}`"
        if r.what == "repository" and r.result in ("kept", "would delete", "failed"):
            thing = f"[`{r.name}`](https://github.com/{r.name})"
        result = _RESULTS.get(r.result, r.result) + (f": {r.detail}" if r.detail else "")
        lines.append(f"| {r.what.capitalize()} | {thing} | {result} |")
    if not dry_run and any(r.result == "deleted" for r in removals):
        lines += [
            "",
            "GitHub keeps deleted repositories for 90 days and deleted images for 30: an organisation owner "
            "can restore them from the organisation's settings (*Repositories*, *Deleted repositories*, and "
            "*Packages*).",
        ]
    return "\n".join(lines) + "\n"
