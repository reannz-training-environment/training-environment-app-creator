"""Delete an app: its repositories, their images, and its spec in apps/.

Only repositories the app creator made for this app are deleted: each records
its app in .app-creator.json, and a repository without one, or made for
another app, is left alone, with its image.

GitHub keeps a deleted repository for 90 days and a deleted image for 30, and
both can be restored from the organisation's settings in that time.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

from . import render
from .publish import GitHub
from .spec import INTERFACES

GENERATOR = "training-environment-app-creator"


@dataclass
class Removal:
    what: str  # repository | image | spec
    name: str
    result: str  # deleted | would delete | not found | kept | failed
    detail: str = ""


def _manifest(gh: GitHub, full_name: str, branch: str) -> dict | None:
    status, payload = gh.request("GET", f"/repos/{full_name}/contents/{render.MANIFEST}?ref={branch}")
    if status != 200 or not isinstance(payload, dict):
        return None
    try:
        return json.loads(base64.b64decode(payload["content"]))
    except ValueError:
        return None


def _image(gh: GitHub, org: str, package: str, dry_run: bool) -> Removal:
    """Delete the image of a repository, which outlives the repository."""
    image = f"ghcr.io/{org}/{package}"
    settings = f"https://github.com/orgs/{org}/packages/container/{package}/settings"
    if dry_run:
        status, _ = gh.request("GET", f"/orgs/{org}/packages/container/{package}")
        if status == 200:
            return Removal("image", image, "would delete")
        if status == 404:
            return Removal("image", image, "not found")
        return Removal("image", image, "kept", f"the app creator cannot see images; check [its settings]({settings})")
    status, payload = gh.request("DELETE", f"/orgs/{org}/packages/container/{package}")
    if status == 204:
        return Removal("image", image, "deleted")
    if status == 404:
        return Removal("image", image, "not found")
    message = payload.get("message") if isinstance(payload, dict) else payload
    return Removal(
        "image",
        image,
        "failed",
        f"GitHub would not let the app creator delete it ({status}: {message}); delete it from "
        f"[its settings]({settings}), under *Danger Zone*",
    )


def delete_app(
    name: str,
    defaults: dict[str, Any],
    creator_repo: str,
    gh: GitHub,
    packages: GitHub | None = None,
    dry_run: bool = False,
    log=print,
) -> list[Removal]:
    """Delete the app's repositories, their images and its spec, or with
    dry_run, say what would be deleted."""
    org = defaults["github_org"]
    removals = []
    for interface in INTERFACES:
        repo_name = render.repo_name(defaults, interface, name)
        full = f"{org}/{repo_name}"
        repo = gh.repo(full)
        if repo is not None:
            manifest = _manifest(gh, full, repo.get("default_branch") or "main")
            if not manifest or manifest.get("generator") != GENERATOR or manifest.get("app") != name:
                removals.append(Removal("repository", full, "kept", f"the app creator did not make it for {name}"))
                log(f"kept {full}: the app creator did not make it for {name}")
                continue
            if dry_run:
                removals.append(Removal("repository", full, "would delete"))
            else:
                status, payload = gh.request("DELETE", f"/repos/{full}")
                if status == 204:
                    removals.append(Removal("repository", full, "deleted"))
                    log(f"deleted {full}")
                else:
                    message = payload.get("message") if isinstance(payload, dict) else payload
                    removals.append(Removal("repository", full, "failed", f"{status}: {message}"))
                    log(f"could not delete {full}: {status}: {message}")
        # the image, even when its repository is already gone
        removal = _image(packages or gh, org, repo_name.lower(), dry_run)
        removals.append(removal)
        log(f"{removal.result}: {removal.name}")

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
    "failed": ":x: not deleted",
}


def markdown(name: str, removals: list[Removal], dry_run: bool) -> str:
    if dry_run:
        head = f"## Dry run: what deleting the {name} app would do"
    elif any(r.result == "failed" for r in removals):
        head = f"## :warning: The {name} app is partly deleted"
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
