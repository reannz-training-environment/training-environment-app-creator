"""Wait for the images of a release to build, and check the training
environment can pull them.

Publishing pushes to each app repository's main branch. Its Release workflow
then tags the version that submit.yml.erb pins and starts its image build on
that tag. This follows those runs, then pulls each image's manifest the way the
training environment does: without credentials.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from .spec import INTERFACES

# the workflows every app repository has, by file and by name
BUILD = ("build_container.yml", "Create and publish a Docker image")
RELEASE = ("release.yml", "Release")
FINAL = {"built", "failed", "release failed", "not started"}
# how long a release may have finished without its build appearing
GRACE = 300

MANIFEST_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)


def image_name(defaults: dict[str, Any], repo: str) -> str:
    return f"{defaults['registry']}/{repo.lower()}"


def pullable(image: str, tag: str, urlopen=urllib.request.urlopen) -> bool:
    """Whether anyone can pull image:tag without credentials."""
    registry, path = image.split("/", 1)
    try:
        with urlopen(f"https://{registry}/token?scope=repository:{path}:pull&service={registry}", timeout=30) as resp:
            token = json.load(resp).get("token")
    except (urllib.error.URLError, ValueError):
        return False
    if not token:
        return False
    req = urllib.request.Request(f"https://{registry}/v2/{path}/manifests/{tag}", method="HEAD")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", ", ".join(MANIFEST_TYPES))
    try:
        with urlopen(req, timeout=30) as resp:
            return resp.status == 200
    except urllib.error.URLError:
        return False


def _is(run: dict, workflow: tuple[str, str]) -> bool:
    path = run.get("path") or ""
    return path.endswith("/" + workflow[0]) or (not path and run.get("name") == workflow[1])


def poll(gh, item: dict, now: float) -> None:
    """Bring one release's state up to date from its repository's workflow runs."""
    status, payload = gh.request("GET", f"/repos/{item['repo']}/actions/runs?per_page=50")
    runs = payload.get("workflow_runs", []) if status == 200 and isinstance(payload, dict) else []
    build = next((r for r in runs if _is(r, BUILD) and r.get("head_branch") == item["tag"]), None)
    if build:
        item["run_url"] = build["html_url"]
        if build["status"] != "completed":
            item["state"] = "building"
        else:
            item["state"] = "built" if build["conclusion"] == "success" else "failed"
        return
    release = next((r for r in runs if _is(r, RELEASE) and r.get("head_sha") == item["sha"]), None)
    if release is None:
        return
    item["run_url"] = release["html_url"]
    if release["status"] != "completed":
        item["state"] = "releasing"
    elif release["conclusion"] != "success":
        item["state"] = "release failed"
    else:
        # tagged; the build it started should show up within moments
        item.setdefault("released_at", now)
        if now - item["released_at"] > GRACE:
            item["state"] = "not started"


def wait(gh, items: list[dict], timeout: float, interval: float = 45, log=print, clock=time.monotonic, sleep=time.sleep) -> list[dict]:
    """Follow every release until its image is built, it fails, or time runs out."""
    deadline = clock() + timeout
    pending = list(items)
    while True:
        for item in pending:
            poll(gh, item, clock())
        pending = [item for item in pending if item.get("state") not in FINAL]
        if not pending or clock() >= deadline:
            break
        log("waiting for: " + ", ".join(f"{i['repo']} ({i.get('state', 'waiting')})" for i in pending))
        sleep(interval)
    for item in pending:
        item["state"] = "timed out"
    for item in items:
        if item["state"] == "built":
            item["public"] = pullable(item["image"], item["tag"])
    return items


def _status(item: dict) -> str:
    run = item.get("run_url", "")
    org, name = item["repo"].split("/")
    settings = f"https://github.com/orgs/{org}/packages/container/{name}/settings"
    if item["state"] == "built":
        if item.get("public"):
            return ":white_check_mark: built, and public"
        return f":warning: built, but private: [make it public]({settings})"
    if item["state"] == "failed":
        return f":x: [the build failed]({run})"
    if item["state"] == "release failed":
        return f":x: [the release failed]({run})"
    if item["state"] == "not started":
        return f":x: tagged, but [no build started]({run})"
    return f":hourglass: [still building]({run}) when the app creator stopped waiting" if run else ":hourglass: no build seen"


def markdown(items: list[dict], mentions: list[str]) -> str:
    built = [i for i in items if i["state"] == "built"]
    private = [i for i in built if not i.get("public")]
    trouble = [i for i in items if i["state"] != "built"]
    if trouble:
        lines = ["## :x: Not every image was built", ""]
    elif private:
        lines = ["## :warning: Built: one step left", ""]
    else:
        lines = [
            "## :white_check_mark: The app is ready",
            "",
            "Every image is built and public, so the training environment can pull it.",
            "",
        ]
    lines += ["| Repository | Image | |", "|---|---|---|"]
    for item in items:
        label = INTERFACES[item["interface"]]["label"]
        repo = f"[`{item['repo'].split('/')[1]}`](https://github.com/{item['repo']}) ({label})"
        lines.append(f"| {repo} | `{item['image']}:{item['tag']}` | {_status(item)} |")
    if private:
        lines += [
            "",
            "The training environment only pulls public images. A new image starts private when the "
            "organisation does not allow public packages (*Settings*, *Packages*, *Package creation*), or "
            "when its repository is private, and GitHub cannot change that automatically. For each image "
            "marked private: open *make it public*, choose **Change visibility** under *Danger Zone*, then "
            "**Public**, and type the image's name to confirm. Later versions of the app stay public.",
        ]
    if trouble:
        lines += [
            "",
            "Open each failed run to see why. Re-running it (*Re-run jobs*) carries on from there: a release "
            "that passes builds its image, and a build that passes pushes it.",
        ]
    lines += [
        "",
        "To use the app in a workshop, add it to the training environment's `ood_apps`; each repository's "
        "README has the lines to add.",
    ]
    if mentions:
        lines += ["", " ".join(f"@{m}" for m in mentions)]
    return "\n".join(lines) + "\n"
