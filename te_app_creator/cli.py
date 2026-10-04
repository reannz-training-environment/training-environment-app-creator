"""Command line: python -m te_app_creator <command> ...

  validate [SPEC ...]       check specs (default: every apps/*.yml)
  render SPEC [-o DIR]      write the generated apps to DIR/<repository name>/
  matrix [SPEC ...]         JSON list of (spec, interface, repository), for CI
  summary [SPEC ...]        Markdown summary of what merging the specs does
  request --event FILE      turn an app request (an issue) into a pull request
  handoff --pr N ...        after a request's test builds: ask for approval
  publish SPEC [...]        create the repositories, or update them
  ready REPORT              wait for the released images, and say if they are ready
  delete --app NAME ...     delete an app's repositories, images and spec
  setup-app                 make the GitHub App the workflows act as
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from . import publish as publisher
from . import ready, render, request, sizes
from .spec import INTERFACES, ROOT, Spec, SpecError, load, load_defaults, load_schema


def _spec_paths(paths: list[str]) -> list[Path]:
    if paths:
        return [Path(p) for p in paths]
    return sorted((ROOT / "apps").glob("*.yml"))


def _load_all(paths: list[str]) -> tuple[list[Spec], list[SpecError]]:
    schema, defaults = load_schema(), load_defaults()
    specs, errors = [], []
    for path in _spec_paths(paths):
        try:
            specs.append(load(path, schema, defaults))
        except SpecError as exc:
            errors.append(exc)
    names = {}
    for spec in specs:
        if spec.name in names:
            errors.append(SpecError(spec.path, [f"the app name {spec.name} is also used by {names[spec.name]}"]))
        names[spec.name] = spec.path
    return specs, errors


def _token() -> str | None:
    for var in ("APP_CREATOR_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    return None


def cmd_validate(args) -> int:
    specs, errors = _load_all(args.specs)
    for spec in specs:
        print(f"ok: {spec.path} ({', '.join(spec.app['interfaces'])})")
        for warning in spec.warnings:
            print(f"  warning: {warning}")
            if os.environ.get("GITHUB_ACTIONS"):
                print(f"::warning file={spec.path}::{warning}")
    for exc in errors:
        print(f"ERROR: {exc.path}")
        for message in exc.errors:
            print(f"  {message}")
            if os.environ.get("GITHUB_ACTIONS"):
                print(f"::error file={exc.path}::{message}")
    return 1 if errors else 0


def cmd_render(args) -> int:
    defaults = load_defaults()
    spec = load(args.spec, load_schema(), defaults)
    interfaces = [args.interface] if args.interface else spec.app["interfaces"]
    for interface in interfaces:
        if interface not in spec.app["interfaces"]:
            print(f"{spec.path} has no {interface} interface", file=sys.stderr)
            return 1
        out = Path(args.out) / render.repo_name(defaults, interface, spec.name)
        if out.exists() and any(out.iterdir()) and not args.force:
            print(f"{out} is not empty; use --force to write into it", file=sys.stderr)
            return 1
        render.write(render.render(spec, interface, defaults), out)
        print(out)
    return 0


def cmd_matrix(args) -> int:
    specs, errors = _load_all(args.specs)
    if errors:
        for exc in errors:
            print(exc, file=sys.stderr)
        return 1
    defaults = load_defaults()
    rows = [
        {
            "spec": str(Path(spec.path).relative_to(ROOT)) if Path(spec.path).is_absolute() else spec.path,
            "name": spec.name,
            "interface": interface,
            "repo": render.repo_name(defaults, interface, spec.name),
        }
        for spec in specs
        for interface in spec.app["interfaces"]
    ]
    print(json.dumps(rows))
    return 0


def _features(app: dict) -> str:
    gpu, slurm, lmod = app["features"]["gpu"], app["features"]["slurm"], app["features"]["lmod"]
    parts = []
    if gpu["enabled"]:
        cards = ", ".join(f"`{c}`" for c in gpu["cards"])
        torch = ", PyTorch" if gpu["pytorch"] else ""
        if gpu["mode"] == "choose":
            parts.append(f"one emulated GPU per session, chosen from {cards} ({gpu['vram']}{torch})")
        else:
            parts.append(f"emulated GPUs ({cards}, {gpu['vram']} each{torch})")
    if slurm["enabled"]:
        parts.append(f"Slurm emulator (partition `{slurm['partition']}`)")
    if lmod["enabled"]:
        parts.append("Lmod")
    return ", ".join(parts) or "none"


def _software(app: dict) -> list[str]:
    sw = app["software"]
    lines = []
    for label, items in (
        ("conda", sw["conda"]["packages"]),
        ("pip", sw["pip"]),
        ("apt", sw["apt"]),
        ("R (CRAN)", sw["r"]["cran"]),
        ("R (Bioconductor)", sw["r"]["bioconductor"]),
        ("R (GitHub)", sw["r"]["github"]),
        ("VS Code extensions", sw["vscode_extensions"]),
    ):
        if items:
            lines.append(f"  * {label}: " + ", ".join(f"`{i}`" for i in items))
    return lines


def cmd_summary(args) -> int:
    specs, errors = _load_all(args.specs)
    defaults = load_defaults()
    token = _token()
    gh = publisher.GitHub(token) if token else None
    out = []
    for spec in specs:
        app = spec.app
        out.append(f"### {app['title']} (`{spec.name}`, v{app['version']})")
        out.append("")
        out.append("| Interface | Repository | |")
        out.append("|---|---|---|")
        for interface in app["interfaces"]:
            name = render.repo_name(defaults, interface, spec.name)
            full = f"{defaults['github_org']}/{name}"
            state = "will be created"
            if gh:
                try:
                    if gh.repo(full):
                        state = "exists: will be updated to match"
                except publisher.PublishError:
                    state = "unknown"
            out.append(f"| {INTERFACES[interface]['label']} | [`{name}`](https://github.com/{full}) | {state} |")
        out.append("")
        r = app["resources"]
        out.append(f"* Session: {r['cpu']} CPUs, {r['memory_gb']} GB, {r['wall_time_hours']['default']} h")
        out.append(f"* HPC features: {_features(app)}")
        software = _software(app)
        out.append("* Software:" + ("" if software else " nothing extra"))
        out.extend(software)
        if app["data"]:
            out.append("* Data:")
            for item in app["data"]:
                source = f"`{item['repo']}@{item['ref']}`" if item["type"] == "github" else f"<{item['url']}>"
                out.append(f"  * {source} -> `~/{item['dest']}`")
        if app["advanced"]["dockerfile"]:
            out.append("* :warning: Adds its own Dockerfile instructions (`advanced.dockerfile`); review them.")
        if app["advanced"]["startup"]:
            out.append("* :warning: Runs its own startup commands (`advanced.startup`); review them.")
        for warning in spec.warnings:
            out.append(f"* :warning: {warning}")
        if args.base:
            stale = _unreleased_change(spec, args.base)
            if stale:
                out.append(f"* :warning: {stale}")
        out.append("")
        if args.sizes:
            out.extend(_sizes_section(spec, defaults, Path(args.sizes)))
    for exc in errors:
        out.append(f"### :x: `{exc.path}`")
        out.extend(f"* {m}" for m in exc.errors)
        out.append("")
    print("\n".join(out))
    return 1 if errors else 0


def _unreleased_change(spec: Spec, base: str) -> str:
    """A warning when a spec that exists at `base` changes without a new version:
    the repositories would change, but sessions would keep the old image."""
    path = Path(spec.path)
    rel = path.relative_to(ROOT) if path.is_absolute() else path
    proc = subprocess.run(["git", "show", f"{base}:{rel.as_posix()}"], cwd=ROOT, capture_output=True, text=True)
    if proc.returncode != 0:
        return ""
    try:
        old = yaml.safe_load(proc.stdout) or {}
    except yaml.YAMLError:
        return ""
    before = str(old.get("version", "0.1.0"))
    after = spec.app["version"]
    if request._version(after) > request._version(before):
        return ""
    return (
        f"This changes an existing app without raising its version (still v{after}), so no new image is "
        f"released: sessions keep running v{before}. Make the version {request.next_version(before)} to release it."
    )


def _sizes_section(spec: Spec, defaults: dict, directory: Path) -> list[str]:
    """The measured sizes of each of the app's images, from `sizes --json` files."""
    out = ["#### Space needed", ""]
    data_bytes = 0
    for interface in spec.app["interfaces"]:
        name = render.repo_name(defaults, interface, spec.name)
        path = directory / f"{name}.json"
        if not path.is_file():
            out += [f"**{INTERFACES[interface]['label']}**: not measured, the image did not build", ""]
            continue
        report = json.loads(path.read_text(encoding="utf-8"))
        data_bytes = max(data_bytes, report["data_bytes"])
        out.append(sizes.markdown(report))
    if data_bytes:
        out += [
            f"Every learner gets their own copy of the data, so the home directories need "
            f"{sizes.human(data_bytes)} for each learner: {sizes.human(30 * data_bytes)} for 30.",
            "",
        ]
    return out


def cmd_sizes(args) -> int:
    defaults = load_defaults()
    spec = load(args.spec, load_schema(), defaults)
    report = sizes.measure(args.image, spec, args.interface, defaults, download=not args.no_download)
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(sizes.markdown(report))
    return 0


MARKER = "<!-- app-creator-summary -->"


def cmd_comment(args) -> int:
    """Create the pull request's summary comment, or replace the one already there."""
    token = _token()
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        print("comment needs GH_TOKEN (or GITHUB_TOKEN) and GITHUB_REPOSITORY", file=sys.stderr)
        return 1
    body = Path(args.file).read_text(encoding="utf-8")
    if body.startswith(MARKER):
        body = body[len(MARKER) :].lstrip("\n")
    publisher.GitHub(token).comment(repo, args.pr, body, MARKER)
    return 0


def cmd_request(args) -> int:
    """Turn an app request into a pull request: the one in an `issues` event,
    or, to try one again, the issue with a given number."""
    repo = os.environ.get("GITHUB_REPOSITORY")
    app_token = os.environ.get("APP_CREATOR_TOKEN")
    fallback = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not repo or not (app_token or fallback) or not (args.event or args.issue):
        print("request needs --event or --issue, GITHUB_REPOSITORY, and APP_CREATOR_TOKEN or GH_TOKEN", file=sys.stderr)
        return 1
    if args.event:
        event = json.loads(Path(args.event).read_text(encoding="utf-8"))
    else:
        issue = publisher.GitHub(app_token or fallback).call("GET", f"/repos/{repo}/issues/{args.issue}")
        event = {"action": "retried", "issue": issue}
    if not app_token:
        # GITHUB_TOKEN can answer, but a pull request it opened would start no checks
        issue = event["issue"]
        run = os.environ.get("GITHUB_RUN_ID")
        retry = "re-running this request"
        if run:
            server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
            retry = f"re-running [this run]({server}/{repo}/actions/runs/{run})"
        publisher.GitHub(fallback).comment(
            repo,
            issue["number"],
            f"Thanks @{issue['user']['login']}. The app creator cannot act on requests yet: it needs its GitHub "
            "App, which a maintainer sets up once (see *Setting up* in the README). Once it is set up, "
            f"{retry} picks this request up.\n",
            request.MARKER,
        )
        print("::error::No GitHub App token: run `python -m te_app_creator setup-app` (see the README).")
        return 1
    members = os.environ.get("MEMBERS_TOKEN")
    outcome = request.handle(
        event,
        repo,
        publisher.GitHub(app_token),
        load_schema(),
        load_defaults(),
        log=lambda m: print(m, file=sys.stderr),
        members=publisher.GitHub(members) if members else None,
        approved=args.approved,
    )
    print(outcome)
    return 0


def cmd_handoff(args) -> int:
    """After a request's images are test-built: ask for approval, or report the failure."""
    token = _token()
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        print("handoff needs GH_TOKEN (or GITHUB_TOKEN) and GITHUB_REPOSITORY", file=sys.stderr)
        return 1
    print(request.handoff(publisher.GitHub(token), repo, args.pr, args.built, args.run_url, load_defaults()))
    return 0


def cmd_ready(args) -> int:
    """Wait for the images the publish report says were released, then report."""
    token = _token()
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token:
        print("ready needs GH_TOKEN (or GITHUB_TOKEN), to follow the builds", file=sys.stderr)
        return 1
    defaults = load_defaults()
    results = json.loads(Path(args.report).read_text(encoding="utf-8"))
    items = [
        {
            "repo": r["repo"],
            "interface": r["interface"],
            "tag": r["tag"],
            "sha": r["sha"],
            "image": ready.image_name(defaults, r["repo"]),
            "state": "waiting",
        }
        for r in results
        if r.get("tag")
    ]
    if not items:
        print("Nothing new was released, so there are no images to wait for.")
        return 0
    gh = publisher.GitHub(token)
    mentions = []
    if args.pr and repo:
        pr = gh.call("GET", f"/repos/{repo}/pulls/{args.pr}")
        match = request.REQUESTED_BY.search(pr.get("body") or "")
        if match:
            mentions.append(match.group(1))
    for name in args.mention or []:
        if name and "[bot]" not in name and name.lower() not in {m.lower() for m in mentions}:
            mentions.append(name)
    ready.wait(gh, items, timeout=args.timeout_minutes * 60, log=lambda m: print(m, file=sys.stderr))
    text = ready.markdown(items, mentions)
    print(text)
    if args.markdown:
        Path(args.markdown).write_text(text, encoding="utf-8")
    if args.pr and repo:
        gh.comment(repo, args.pr, text)
    return 0 if all(i["state"] == "built" for i in items) else 1


def cmd_delete(args) -> int:
    """Delete an app's repositories, their images and its spec."""
    from . import delete

    if args.app != args.confirm:
        print(f"The names do not match ({args.app!r} and {args.confirm!r}): type the app's name twice.", file=sys.stderr)
        return 1
    if not request.NAME.match(args.app):
        print(f"{args.app!r} is not an app name: lower case letters, digits and single hyphens.", file=sys.stderr)
        return 1
    token = _token()
    if not token:
        print("delete needs APP_CREATOR_TOKEN (or GH_TOKEN): a token that can delete repositories", file=sys.stderr)
        return 1
    defaults = load_defaults()
    creator = os.environ.get("GITHUB_REPOSITORY") or f"{defaults['github_org']}/training-environment-app-creator"
    packages = os.environ.get("PACKAGES_TOKEN")
    # reads the app repositories' workflow runs, which the GitHub App may not
    runs = os.environ.get("RUNS_TOKEN") or token
    removals = delete.delete_app(
        args.app,
        defaults,
        creator,
        publisher.GitHub(token),
        packages=publisher.GitHub(packages) if packages else None,
        runs=publisher.GitHub(runs),
        dry_run=args.dry_run,
        log=lambda m: print(m, file=sys.stderr),
    )
    text = delete.markdown(args.app, removals, args.dry_run)
    print(text)
    if args.markdown:
        Path(args.markdown).write_text(text, encoding="utf-8")
    return 1 if any(r.result == "failed" for r in removals) else 0


def cmd_setup_app(args) -> int:
    from . import setup_app

    defaults = load_defaults()
    return setup_app.run(args.org or defaults["github_org"], args.repo, args.name, defaults.get("website", ""))


def cmd_publish(args) -> int:
    token = _token()
    if not token:
        print(
            "publish needs a token that can create repositories in the organisation: "
            "set APP_CREATOR_TOKEN (see the README)",
            file=sys.stderr,
        )
        return 1
    specs, errors = _load_all(args.specs)
    if errors:
        for exc in errors:
            print(exc, file=sys.stderr)
        return 1
    defaults = load_defaults()
    results, failed = [], False
    for spec in specs:
        interfaces = [args.interface] if args.interface else spec.app["interfaces"]
        for interface in interfaces:
            try:
                results.append(
                    publisher.publish_interface(
                        spec,
                        interface,
                        defaults,
                        token,
                        dry_run=args.dry_run,
                        merge=args.merge_updates,
                        creator_sha=os.environ.get("GITHUB_SHA"),
                        git_protocol=args.git_protocol,
                        log=lambda m: print(m, file=sys.stderr),
                    )
                )
            except publisher.PublishError as exc:
                failed = True
                name = render.repo_name(defaults, interface, spec.name)
                results.append(publisher.Result(interface, f"{defaults['github_org']}/{name}", "failed", "", str(exc)))
    for r in results:
        print(f"{r.action:>13}  {r.repo}  {r.url}  {r.detail}")
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump([r.__dict__ for r in results], fh, indent=2)
    if args.markdown:
        with open(args.markdown, "w", encoding="utf-8") as fh:
            fh.write(_report_markdown(results, args.dry_run))
    return 1 if failed else 0


_ACTIONS = {
    "created": "created",
    "updated": "update pull request opened",
    "merged": "updated",
    "unchanged": "already up to date",
    "would-create": "would be created",
    "would-update": "exists, would be updated",
    "failed": ":x: failed",
}


def _report_markdown(results: list[publisher.Result], dry_run: bool) -> str:
    lines = [
        "## App creator: " + ("dry run" if dry_run else "published"),
        "",
        "| Repository | Interface | Result | |",
        "|---|---|---|---|",
    ]
    for r in results:
        link = f"[`{r.repo}`](https://github.com/{r.repo})"
        result = _ACTIONS.get(r.action, r.action)
        if r.action in ("updated", "merged"):
            result = f"[{result}]({r.url})"
        lines.append(f"| {link} | {INTERFACES[r.interface]['label']} | {result} | {r.detail} |")
    if any(r.tag for r in results):
        lines += [
            "",
            "Each repository is now releasing that version and building its image, with its own workflows; "
            "that takes 10 to 30 minutes. The app creator follows the builds, and says here when the "
            "images are ready.",
        ]
    if any(r.action == "updated" for r in results):
        lines += [
            "",
            "Merging an update pull request releases it, if the app's version is new.",
        ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="te_app_creator", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", help="check app specs")
    p.add_argument("specs", nargs="*")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("render", help="write the generated apps of a spec")
    p.add_argument("spec")
    p.add_argument("-i", "--interface", choices=list(INTERFACES))
    p.add_argument("-o", "--out", default="build")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("matrix", help="JSON build matrix for CI")
    p.add_argument("specs", nargs="*")
    p.set_defaults(func=cmd_matrix)

    p = sub.add_parser("summary", help="Markdown summary of specs")
    p.add_argument("specs", nargs="*")
    p.add_argument("--sizes", help="directory of `sizes --json` reports to include")
    p.add_argument("--base", help="git commit to compare versions with, to warn of changes that would not be released")
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser("sizes", help="measure a built image of an app")
    p.add_argument("spec")
    p.add_argument("-i", "--interface", required=True, choices=list(INTERFACES))
    p.add_argument("--image", required=True, help="the built image, in the local docker")
    p.add_argument("--json", help="also write the report as JSON to this file")
    p.add_argument("--no-download", action="store_true", help="skip estimating the compressed download size")
    p.set_defaults(func=cmd_sizes)

    p = sub.add_parser("comment", help="create or update the pull request's summary comment")
    p.add_argument("--pr", required=True, type=int)
    p.add_argument("--file", required=True)
    p.set_defaults(func=cmd_comment)

    p = sub.add_parser("request", help="turn an app request into a pull request")
    p.add_argument("--event", help="the `issues` event, as JSON (GITHUB_EVENT_PATH)")
    p.add_argument("--issue", type=int, help="or: the request's issue number, to try it again")
    p.add_argument("--approved", action="store_true", help="a maintainer asked for it: do not check the requester")
    p.set_defaults(func=cmd_request)

    p = sub.add_parser("handoff", help="after a request's test builds: ask for approval, or report the failure")
    p.add_argument("--pr", required=True, type=int)
    p.add_argument("--built", required=True, help="how the test builds went: success, failure, cancelled, ...")
    p.add_argument("--run-url", default="", help="the run that built them")
    p.set_defaults(func=cmd_handoff)

    p = sub.add_parser("publish", help="create or update the app repositories")
    p.add_argument("specs", nargs="+")
    p.add_argument("-i", "--interface", choices=list(INTERFACES))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--merge-updates", action="store_true", help="merge the update pull requests, releasing them")
    p.add_argument("--git-protocol", choices=["https", "ssh"], default="https")
    p.add_argument("--report", help="write the results as JSON to this file")
    p.add_argument("--markdown", help="write the results as Markdown to this file")
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("ready", help="wait for released images, then say whether they are ready")
    p.add_argument("report", help="the JSON report of publish")
    p.add_argument("--pr", type=int, help="the merged pull request to comment on")
    p.add_argument("--mention", action="append", help="someone to tell (repeatable)")
    p.add_argument("--timeout-minutes", type=float, default=120)
    p.add_argument("--markdown", help="also write the report to this file")
    p.set_defaults(func=cmd_ready)

    p = sub.add_parser("delete", help="delete an app: its repositories, their images and its spec")
    p.add_argument("--app", required=True, help="the app's name")
    p.add_argument("--confirm", required=True, help="the app's name again")
    p.add_argument("--dry-run", action="store_true", help="only say what would be deleted")
    p.add_argument("--markdown", help="also write the report to this file")
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser("setup-app", help="make the GitHub App the workflows act as (once, as an organisation owner)")
    p.add_argument("--org", help="default: github_org in config/defaults.yml")
    p.add_argument("--repo", default="training-environment-app-creator")
    p.add_argument("--name", default="REANNZ training app creator", help="the App's name, unique across GitHub")
    p.set_defaults(func=cmd_setup_app)

    args = ap.parse_args(argv)
    return args.func(args)
