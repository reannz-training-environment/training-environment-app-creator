"""Turn an app request into a pull request, and hand it over for approval.

The app creator website files each request as an issue made with the
"Request an app" form, whose one field is the app spec. The Request workflow
runs ``handle`` on it: the spec is checked, committed as apps/<name>.yml on the
branch app-request/<issue number>, and a pull request is opened. The Validate
workflow test-builds that pull request's images, then runs ``handoff``: when
they all build, the reviewers are asked to approve the request, which they do
by merging it. The Publish workflow takes it from there.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from . import render, sizes
from .publish import GitHub
from .spec import INTERFACES, SpecError, check

HEADING = "### App spec"
BRANCH_PREFIX = "app-request/"
MARKER = "<!-- app-creator-request -->"
APPROVED_LABEL = "request approved"
# GitHub's author_association values for people in the organisation
TRUSTED = {"OWNER", "MEMBER", "COLLABORATOR"}
# the first line of a request's pull request, which later steps read back
REQUESTED_BY = re.compile(r"^Requested by @([A-Za-z0-9-]+) in #(\d+)", re.M)
NAME = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
VERSION_LINE = re.compile(r"^version:[^\n]*$", re.M)
NAME_LINE = re.compile(r"^(name:[^\n]*\n)", re.M)


class RequestError(Exception):
    """A request that cannot become a pull request, saying why, for the requester."""


class NothingToDo(RequestError):
    pass


@dataclass
class Plan:
    """What a request asks for: the file to commit, and what it changes."""

    name: str
    title: str
    text: str
    version: str
    interfaces: list[str]
    previous: str | None = None  # the version on main, when the app exists
    bumped: bool = False
    warnings: list[str] = field(default_factory=list)
    review: list[str] = field(default_factory=list)  # things a reviewer must read

    @property
    def path(self) -> str:
        return f"apps/{self.name}.yml"


# ------------------------------------------------------------------ reading


def extract_spec(body: str | None) -> str:
    """The YAML in the issue form's App spec field."""
    lines = (body or "").replace("\r\n", "\n").split("\n")
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == HEADING) + 1
    except StopIteration:
        raise RequestError("the request has no *App spec* section. Make requests with the app creator website.") from None
    block = lines[start:]
    while block and not block[0].strip():
        block.pop(0)
    if block and block[0].lstrip().startswith("```"):
        # the form puts the field in a fenced block. It is the form's only
        # field, so the block ends at the last fence, whatever is inside it.
        closing = [i for i, line in enumerate(block) if i > 0 and line.strip() == "```"]
        block = block[1 : closing[-1]] if closing else block[1:]
    else:
        end = next((i for i, line in enumerate(block) if line.startswith("### ")), len(block))
        block = block[:end]
    text = "\n".join(block).strip("\n")
    if not text.strip() or text.strip() == "_No response_":
        raise RequestError("the *App spec* section is empty")
    return text + "\n"


def _version(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def next_version(version: str) -> str:
    major, minor, patch = _version(version)
    return f"{major}.{minor}.{patch + 1}"


def set_version(text: str, version: str) -> str:
    """The spec with its version changed, keeping its comments and layout."""
    if VERSION_LINE.search(text):
        return VERSION_LINE.sub(f"version: {version}", text, count=1)
    if NAME_LINE.search(text):
        return NAME_LINE.sub(lambda m: f"{m.group(1)}version: {version}\n", text, count=1)
    raw = yaml.safe_load(text)
    raw["version"] = version
    return yaml.safe_dump(raw, sort_keys=False)


def _without_version(raw: Any) -> Any:
    if isinstance(raw, dict):
        return {k: v for k, v in raw.items() if k != "version"}
    return raw


def parse(text: str) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RequestError(f"the spec is not valid YAML:\n\n```\n{exc}\n```") from None
    if not isinstance(raw, dict):
        raise RequestError("the spec is not an app spec")
    name = raw.get("name")
    if not isinstance(name, str) or not NAME.match(name):
        raise RequestError(
            "the app needs a name of lower case letters, digits and single hyphens, such as `intro-python`"
        )
    return raw


def plan(text: str, raw: dict[str, Any], existing: str | None, schema: dict, defaults: dict) -> Plan:
    """Check a requested spec against the app as it is on main, if it exists.

    A change to an existing app has to be released under a new version, or
    sessions would keep running the old image, so a request that does not raise
    the version gets the next patch version."""
    path = f"apps/{raw['name']}.yml"
    try:
        spec = check(raw, path, schema, defaults)
    except SpecError as exc:
        raise RequestError("the spec has problems:\n\n" + "\n".join(f"* {m}" for m in exc.errors)) from None
    app = spec.app
    result = Plan(
        name=spec.name,
        title=app["title"],
        text=text,
        version=app["version"],
        interfaces=list(app["interfaces"]),
        warnings=list(spec.warnings),
    )
    if app["advanced"]["dockerfile"]:
        result.review.append("it adds its own Dockerfile instructions (`advanced.dockerfile`)")
    if app["advanced"]["startup"]:
        result.review.append("it runs its own commands when a session starts (`advanced.startup`)")
    if existing is None:
        return result

    old = yaml.safe_load(existing) or {}
    previous = str(old.get("version", "0.1.0"))
    result.previous = previous
    newer = _version(result.version) > _version(previous)
    if not newer and _without_version(old) == _without_version(raw):
        raise NothingToDo(f"`{path}` already says exactly this, so there is nothing to change")
    if not newer:
        result.version = next_version(previous)
        result.text = set_version(text, result.version)
        result.bumped = True
    return result


# ----------------------------------------------------------------- writing


def space_summary(reports: list[dict], learners: int = 30) -> list[str]:
    """The space the app needs, from the test builds' `sizes --json` reports:
    each image, and the data every learner gets."""
    reports = sorted(
        (r for r in reports if r.get("interface") in INTERFACES),
        key=lambda r: list(INTERFACES).index(r["interface"]),
    )
    if not reports:
        return []
    columns = " | ".join(INTERFACES[r["interface"]]["label"] for r in reports)
    on_disk = " | ".join(sizes.human(r["image_bytes"]) for r in reports)
    download = " | ".join(f"about {sizes.human(r['download_bytes'])}" if r.get("download_bytes") else "not measured" for r in reports)
    lines = [
        "**Space needed**, measured by the test builds:",
        "",
        f"| | {columns} |",
        "|---|" + "---|" * len(reports),
        f"| Image on disk, on each worker node that runs it | {on_disk} |",
        f"| Image download, to each of those nodes | {download} |",
        "",
    ]
    data = max(r.get("data_bytes") or 0 for r in reports)
    if data:
        lines.append(
            f"Each learner's home directory gets {sizes.human(data)} of data: "
            f"{sizes.human(learners * data)} for {learners} learners."
        )
    else:
        lines.append("No data is copied into the learners' home directories.")
    return lines


def _status(
    pr: dict | None,
    requester: str,
    stage: str,
    defaults: dict,
    reviewers: list[str] = (),
    run_url: str = "",
    space: list[str] = (),
) -> str:
    """The request's status comment: where it is up to, and what happens next."""
    website = defaults.get("website", "")
    checks = f"{pr['html_url']}/checks"
    if stage == "opened":
        build = f"- [ ] Every image test-builds: under way in [the checks]({checks}), which take 10 to 30 minutes"
        approve = "- [ ] A maintainer approves it, by merging the pull request"
    elif stage == "built":
        build = "- [x] Every image test-builds"
        approve = "- [ ] A maintainer approves it, by merging the pull request"
        if reviewers:
            asked = ", ".join(f"@{r}" for r in reviewers)
            approve += f": {asked} {'has' if len(reviewers) == 1 else 'have'} been asked to review it"
    else:
        build = f"- [ ] :x: An image did not build: see [the checks]({run_url or checks})"
        approve = "- [ ] A maintainer approves it, by merging the pull request"
    lines = [
        f"Thanks @{requester}: this request is pull request #{pr['number']}.",
        "",
        "- [x] The app spec is valid",
        build,
        approve,
        "- [ ] The app's repositories are made, and their images built and released",
        "",
    ]
    if space:
        lines += [*space, "", f"What each part of the app adds is on [the pull request]({pr['html_url']}).", ""]
    if stage == "failed":
        lines.append(
            "A maintainer will look at what went wrong. You can also change the request "
            + (f"on the [website]({website}) " if website else "")
            + "and make it again: that replaces this one."
        )
    else:
        lines.append(
            "Nothing else is needed from you: GitHub tells you as it goes, and the pull request says when the app is ready. "
            "To change the request, make it again"
            + (f" on the [website]({website})" if website else "")
            + "; that replaces this one."
        )
    return "\n".join(lines) + "\n"


def pr_body(plan: Plan, requester: str, issue: int, defaults: dict, others: list[int]) -> str:
    repos = [
        f"* [`{render.repo_name(defaults, i, plan.name)}`](https://github.com/{defaults['github_org']}/"
        f"{render.repo_name(defaults, i, plan.name)}) ({INTERFACES[i]['label']})"
        for i in plan.interfaces
    ]
    lines = [f"Requested by @{requester} in #{issue}, with the app creator website.", ""]
    if plan.previous is None:
        lines += [
            f"Adds the app **{plan.title}** (`{plan.name}`, v{plan.version}). Merging this pull request is the "
            "approval: the app creator then makes these repositories, and releases and builds their images:",
        ]
    else:
        lines += [
            f"Changes the app **{plan.title}** (`{plan.name}`), from v{plan.previous} to v{plan.version}. "
            "Merging this pull request is the approval: the app creator then updates these repositories, "
            f"and releases and builds v{plan.version} of their images:",
        ]
    lines += ["", *repos, ""]
    if plan.bumped:
        lines += [
            f"The request did not raise the version, so the app creator made it v{plan.version}: a change "
            "is only released under a new version.",
            "",
        ]
    lines += [
        "The checks test-build every image and comment with what each needs. Once they all build, the "
        "reviewers are asked to approve it.",
        "",
    ]
    if plan.review:
        lines += [":warning: **Read the spec before approving:** " + "; ".join(plan.review) + ".", ""]
    for warning in plan.warnings:
        lines.append(f"* :warning: {warning}")
    if plan.warnings:
        lines.append("")
    for number in others:
        lines.append(f"Another open request, #{number}, also changes `{plan.path}`.")
    if others:
        lines.append("")
    lines.append(f"Closes #{issue}")
    return "\n".join(lines) + "\n"


def _content(gh: GitHub, repo: str, path: str, ref: str) -> tuple[str | None, str | None]:
    """A file's text and blob sha at ref, or (None, None)."""
    status, payload = gh.request("GET", f"/repos/{repo}/contents/{path}?ref={ref}")
    if status != 200 or not isinstance(payload, dict):
        return None, None
    return base64.b64decode(payload["content"]).decode("utf-8"), payload["sha"]


def trusted(gh: GitHub, repo: str, issue: dict, defaults: dict, members: GitHub | None = None) -> bool:
    """Whether the requester may have their request built without a maintainer
    looking at it first: anyone in the organisation, or anyone at all once a
    maintainer has labelled the request.

    GitHub hides private organisation membership from the event and from an
    App that cannot read the organisation's members: a private member's
    author_association is CONTRIBUTOR or NONE. So this also asks: is the
    requester a reviewer, can they write to the repository, and, with
    `members` (a token that can read members), are they in the organisation."""
    login = issue["user"]["login"]
    if issue.get("author_association") in TRUSTED:
        return True
    if APPROVED_LABEL in {label["name"] for label in issue.get("labels", [])}:
        return True
    if login.lower() in {r.lower() for r in defaults.get("reviewers") or []}:
        return True
    if members is not None:
        status, _ = members.request("GET", f"/orgs/{repo.split('/')[0]}/members/{login}")
        if status == 204:
            return True
    status, payload = gh.request("GET", f"/repos/{repo}/collaborators/{login}/permission")
    if status == 200 and isinstance(payload, dict) and payload.get("permission") in ("admin", "maintain", "write"):
        return True
    status, _ = gh.request("GET", f"/repos/{repo}/collaborators/{login}")
    return status == 204


def handle(
    event: dict,
    repo: str,
    gh: GitHub,
    schema: dict,
    defaults: dict,
    log=print,
    members: GitHub | None = None,
    approved: bool = False,
) -> str:
    """Turn the request in an `issues` event into a pull request, or say why not.

    `approved` skips asking whether to trust the requester: for when a
    maintainer asked for it, by running the Request workflow by hand."""
    issue = event["issue"]
    number = issue["number"]
    requester = issue["user"]["login"]
    if issue.get("state") != "open":
        return "the request is closed"
    if event.get("action") == "labeled" and (event.get("label") or {}).get("name") != APPROVED_LABEL:
        return "only the approval label matters"

    if not approved and not trusted(gh, repo, issue, defaults, members):
        gh.comment(
            repo,
            number,
            f"Thanks @{requester}! The app creator cannot tell that you are in the organisation (GitHub "
            "hides private memberships from it), so a maintainer checks this request first: adding the "
            f"`{APPROVED_LABEL}` label to this issue turns it into a pull request.\n",
            MARKER,
        )
        return "waiting for a maintainer to approve the request"

    owner = repo.split("/")[0]
    base = gh.call("GET", f"/repos/{repo}")["default_branch"]
    base_sha = gh.call("GET", f"/repos/{repo}/git/ref/heads/{base}")["object"]["sha"]
    try:
        text = extract_spec(issue.get("body"))
        raw = parse(text)
        existing, existing_sha = _content(gh, repo, f"apps/{raw['name']}.yml", base_sha)
        request = plan(text, raw, existing, schema, defaults)
    except NothingToDo as exc:
        gh.comment(repo, number, f"@{requester}: {exc}.\n", MARKER)
        return str(exc)
    except RequestError as exc:
        gh.comment(
            repo,
            number,
            f"@{requester}: this request cannot be built yet, because {exc}\n\n"
            "Fix it on the website and make the request again, or edit the spec in this issue.\n",
            MARKER,
        )
        return f"not opened: {exc}"

    branch = f"{BRANCH_PREFIX}{number}"
    verb = "Update" if request.previous else "Add"
    message = f"{verb} the {request.title} app, v{request.version}\n\nRequested by @{requester} in #{number}.\n"
    status, _ = gh.request("GET", f"/repos/{repo}/git/ref/heads/{branch}")
    current, _ = _content(gh, repo, request.path, branch) if status == 200 else (None, None)
    if current == request.text:
        log(f"{branch} already has this spec")
    else:
        # start from main every time, so a renamed app leaves no stray file behind
        if status == 200:
            gh.call("PATCH", f"/repos/{repo}/git/refs/heads/{branch}", {"sha": base_sha, "force": True})
        else:
            gh.call("POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": base_sha})
        put = {"message": message, "content": base64.b64encode(request.text.encode()).decode(), "branch": branch}
        if existing_sha:
            put["sha"] = existing_sha
        gh.call("PUT", f"/repos/{repo}/contents/{request.path}", put)
        log(f"committed {request.path} to {branch}")

    others = _other_requests(gh, repo, request.path, branch)
    replaced = [o for o in others if o["requester"] == requester.lower()]
    still_open = [o["number"] for o in others if o not in replaced]
    title = f"{verb} app: {request.title} ({request.name})"
    body = pr_body(request, requester, number, defaults, still_open)
    prs = gh.call("GET", f"/repos/{repo}/pulls?state=open&head={owner}:{branch}")
    if prs:
        pr = gh.call("PATCH", f"/repos/{repo}/pulls/{prs[0]['number']}", {"title": title, "body": body})
        log(f"updated {pr['html_url']}")
    else:
        pr = gh.call("POST", f"/repos/{repo}/pulls", {"title": title, "head": branch, "base": base, "body": body})
        log(f"opened {pr['html_url']}")

    # the same person asking again for the same app replaces their earlier request
    for old in replaced:
        note = f"Replaced by #{pr['number']}, a newer request for the same app.\n"
        gh.comment(repo, old["number"], note)
        gh.call("PATCH", f"/repos/{repo}/pulls/{old['number']}", {"state": "closed"})
        gh.request("DELETE", f"/repos/{repo}/git/refs/heads/{old['ref']}")
        if old["issue"]:
            gh.comment(repo, old["issue"], f"Replaced by #{number}, a newer request for the same app.\n")
            gh.call("PATCH", f"/repos/{repo}/issues/{old['issue']}", {"state": "closed", "state_reason": "not_planned"})
        log(f"closed #{old['number']}, which this replaces")

    gh.comment(repo, number, _status(pr, requester, "opened", defaults), MARKER)
    return f"pull request {pr['html_url']}"


def _other_requests(gh: GitHub, repo: str, path: str, branch: str) -> list[dict]:
    """Other open request pull requests that change the same app spec."""
    found = []
    for pr in gh.call("GET", f"/repos/{repo}/pulls?state=open&per_page=100"):
        ref = pr["head"]["ref"]
        if not ref.startswith(BRANCH_PREFIX) or ref == branch:
            continue
        files = gh.call("GET", f"/repos/{repo}/pulls/{pr['number']}/files?per_page=100")
        if path not in {f["filename"] for f in files}:
            continue
        match = REQUESTED_BY.search(pr.get("body") or "")
        found.append(
            {
                "number": pr["number"],
                "ref": ref,
                "requester": match.group(1).lower() if match else "",
                "issue": int(match.group(2)) if match else None,
            }
        )
    return found


def handoff(
    gh: GitHub,
    repo: str,
    number: int,
    built: str,
    run_url: str,
    defaults: dict,
    log=print,
    space_reports: list[dict] = (),
) -> str:
    """After a request's images are test-built: ask the reviewers to approve it,
    or tell the requester an image did not build. `space_reports` are the
    test builds' size reports, to tell the requester how much space it needs."""
    pr = gh.call("GET", f"/repos/{repo}/pulls/{number}")
    ref = pr["head"]["ref"]
    match = REQUESTED_BY.search(pr.get("body") or "")
    if pr["state"] != "open" or not ref.startswith(BRANCH_PREFIX) or not match:
        return "not an open app request"
    if built not in ("success", "failure"):
        return f"the build was {built}; nothing to say"
    requester, issue = match.group(1), int(match.group(2))
    reviewers = [r for r in defaults.get("reviewers") or [] if r.lower() != pr["user"]["login"].lower()]

    if built == "success":
        if reviewers:
            status, payload = gh.request("POST", f"/repos/{repo}/pulls/{number}/requested_reviewers", {"reviewers": reviewers})
            if status not in (200, 201):
                log(f"could not ask {', '.join(reviewers)} to review: HTTP {status}: {payload}")
        space = space_summary(list(space_reports))
        gh.comment(repo, issue, _status(pr, requester, "built", defaults, reviewers, space=space), MARKER)
        return "asked for approval"

    gh.comment(repo, issue, _status(pr, requester, "failed", defaults, reviewers, run_url), MARKER)
    # a new comment, so the requester hears about it
    gh.comment(
        repo,
        issue,
        f"@{requester}: an image of this app did not build, so it cannot be approved yet. "
        f"[The checks]({run_url}) say why.\n",
    )
    return "told the requester the build failed"
