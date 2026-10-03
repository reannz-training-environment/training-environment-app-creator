"""Command line: python -m te_app_creator <command> ...

  validate [SPEC ...]       check specs (default: every apps/*.yml)
  render SPEC [-o DIR]      write the generated apps to DIR/<repository name>/
  matrix [SPEC ...]         JSON list of (spec, interface, repository), for CI
  summary [SPEC ...]        Markdown summary of what merging the specs does
  publish SPEC [...]        create the repositories, or open update pull requests
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import publish as publisher
from . import render
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
        parts.append(f"emulated GPUs ({cards}, {gpu['vram']} each{', PyTorch' if gpu['pytorch'] else ''})")
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
                        state = "exists: an update pull request will be opened if anything changed"
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
        out.append("")
    for exc in errors:
        out.append(f"### :x: `{exc.path}`")
        out.extend(f"* {m}" for m in exc.errors)
        out.append("")
    print("\n".join(out))
    return 1 if errors else 0


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
        if r.action == "updated":
            result = f"[{result}]({r.url})"
        lines.append(f"| {link} | {INTERFACES[r.interface]['label']} | {result} | {r.detail} |")
    if any(r.action == "created" for r in results):
        lines += [
            "",
            "Each new repository releases its first version and pushes its image to ghcr.io from its "
            "own workflows, which takes a few minutes. Its README has the `ood_apps` block that adds "
            "it to the training environment.",
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
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser("publish", help="create or update the app repositories")
    p.add_argument("specs", nargs="+")
    p.add_argument("-i", "--interface", choices=list(INTERFACES))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--git-protocol", choices=["https", "ssh"], default="https")
    p.add_argument("--report", help="write the results as JSON to this file")
    p.add_argument("--markdown", help="write the results as Markdown to this file")
    p.set_defaults(func=cmd_publish)

    args = ap.parse_args(argv)
    return args.func(args)
