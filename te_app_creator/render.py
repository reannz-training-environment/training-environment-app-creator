"""Turn a checked app spec into the files of one app per interface."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jinja2
import yaml

from . import __version__
from .spec import GPU_CARDS, INTERFACES, VRAM_OPTIONS, Spec, conda_package_name

TEMPLATES = Path(__file__).resolve().parent / "templates"
MANIFEST = ".app-creator.json"

# Files Open OnDemand runs, which must keep their executable bit
EXECUTABLE = 0o755
REGULAR = 0o644


@dataclass
class OutputFile:
    path: str
    content: bytes
    mode: int = REGULAR


def repo_name(defaults: dict[str, Any], interface: str, name: str) -> str:
    return f"{defaults['repo_prefix']}-{interface}-{name}-app"


def _yaml_str(value: Any) -> str:
    """A YAML scalar that cannot be misread: JSON strings are valid YAML."""
    return json.dumps(str(value), ensure_ascii=False)


def _r_list(values: list[str]) -> str:
    return ", ".join(json.dumps(v) for v in values)


def _vram_label(option: str, default: str) -> str:
    if option == "full":
        label = "Full size (the real board)"
    else:
        label = option.replace("MiB", " MiB").replace("GiB", " GiB")
    return f"{label} (default)" if option == default else label


def _environment(interface: str) -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader([TEMPLATES / interface, TEMPLATES / "common"]),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        # bash has ${#array[@]}; keep Jinja's comment syntax out of its way
        comment_start_string="{##",
        comment_end_string="##}",
        autoescape=False,
    )
    env.filters["yaml_str"] = _yaml_str
    env.filters["r_list"] = _r_list
    env.globals["vram_label"] = _vram_label
    return env


def context(spec: Spec, interface: str, defaults: dict[str, Any]) -> dict[str, Any]:
    """Everything the templates of one interface may refer to."""
    app = spec.app
    pins = defaults["pins"]
    info = INTERFACES[interface]
    repo = repo_name(defaults, interface, app["name"])
    org = defaults["github_org"]
    image = f"{defaults['registry']}/{org}/{repo}".lower()
    gpu = app["features"]["gpu"]
    slurm = app["features"]["slurm"]
    lmod = app["features"]["lmod"]["enabled"]
    rstudio = interface == "rstudio"
    software = app["software"]
    conda_packages = software["conda"]["packages"]
    r_packages = software["r"] if rstudio else {"cran": [], "bioconductor": [], "github": []}
    tz_area, _, tz_zone = defaults["timezone"].partition("/")

    if rstudio:
        # rocker images are Ubuntu 24.04, where Python packages go in the
        # /opt/venv virtual environment (see partials/system-rocker)
        pip = "/opt/venv/bin/pip --no-cache-dir"
        os_codename = "noble"
    else:
        pip = "pip3 --no-cache-dir"
        os_codename = "jammy"

    fleet = ",".join(f"{card}:{gpu['vram']}" for card in gpu["cards"])

    return {
        "app": app,
        "defaults": defaults,
        "pins": pins,
        "iface": {"key": interface, "label": info["label"]},
        "repo": repo,
        "repo_url": f"https://github.com/{org}/{repo}",
        "org": org,
        "image": image,
        "image_tag": f"v{app['version']}",
        "container_name": f"{app['name']}-{info['short']}"[:63].rstrip("-"),
        "ood_key": f"{interface}_{app['name']}".replace("-", "_"),
        "dashboard_title": f"{app['title']} ({info['label']})",
        "spec_path": f"apps/{app['name']}.yml",
        "creator_repo": f"{org}/training-environment-app-creator",
        "other_interfaces": [
            {"key": i, "label": INTERFACES[i]["label"], "repo": repo_name(defaults, i, app["name"])}
            for i in app["interfaces"]
            if i != interface
        ],
        "gpu": gpu,
        "gpu_cards": GPU_CARDS,
        "vram_options": [v for v in VRAM_OPTIONS],
        "fleet": fleet,
        "slurm": slurm,
        "slurm_only": slurm["enabled"] and not gpu["enabled"],
        "lmod": lmod,
        "conda_packages": conda_packages,
        "conda_names": [conda_package_name(p) for p in conda_packages],
        "pip_packages": software["pip"],
        "apt_packages": software["apt"],
        "r_packages": r_packages,
        "has_r_packages": any(r_packages.values()),
        "extensions": software["vscode_extensions"] if interface == "codeserver" else [],
        "data": app["data"],
        "start_dir": app["advanced"]["start_dir"],
        "advanced": app["advanced"],
        "pip": pip,
        "os_codename": os_codename,
        "tz_area": tz_area,
        "tz_zone": tz_zone,
        "rsession_env": rstudio and (gpu["enabled"] or slurm["enabled"] or lmod or bool(conda_packages)),
        "generator_version": __version__,
    }


def _plan(ctx: dict[str, Any]) -> list[tuple[str, str, int]]:
    """(template, output path, mode) for every file this app has."""
    interface = ctx["iface"]["key"]
    files = [
        ("form.yml.j2", "form.yml", REGULAR),
        ("manifest.yml.j2", "manifest.yml", REGULAR),
        ("submit.yml.erb.j2", "submit.yml.erb", REGULAR),
        ("view.html.erb.j2", "view.html.erb", REGULAR),
        ("template/before.sh.erb.j2", "template/before.sh.erb", EXECUTABLE),
        ("template/script.sh.erb.j2", "template/script.sh.erb", EXECUTABLE),
        ("icon.png", "icon.png", REGULAR),
        ("README.md.j2", "README.md", REGULAR),
        ("LICENSE", "LICENSE", REGULAR),
        ("gitignore", ".gitignore", REGULAR),
        ("github/build_container.yml", ".github/workflows/build_container.yml", REGULAR),
        ("github/release.yml", ".github/workflows/release.yml", REGULAR),
        ("docker/Dockerfile.j2", "docker/Dockerfile", REGULAR),
        ("docker/dockerignore", "docker/.dockerignore", REGULAR),
        ("docker/scripts/smoke-test.sh.j2", "docker/scripts/smoke-test.sh", EXECUTABLE),
    ]
    if interface == "codeserver":
        files.append(("template/after.sh.erb.j2", "template/after.sh.erb", EXECUTABLE))
    if interface == "rstudio":
        files.append(("template/bin/auth", "template/bin/auth", EXECUTABLE))
        if ctx["has_r_packages"]:
            files.append(("docker/scripts/install-r-packages.R.j2", "docker/scripts/install-r-packages.R", REGULAR))
    if ctx["conda_packages"]:
        files.append(("docker/scripts/make-conda-wrappers.py", "docker/scripts/make-conda-wrappers.py", EXECUTABLE))
    if ctx["gpu"]["enabled"]:
        files.append(("docker/scripts/gpu-banner.sh.j2", "docker/scripts/gpu-banner.sh", REGULAR))
    if ctx["slurm_only"]:
        files.append(("docker/scripts/slurm-emulator", "docker/scripts/slurm-emulator", EXECUTABLE))
        files.append(("docker/scripts/slurm-cpu-node.py", "docker/scripts/slurm-cpu-node.py", REGULAR))
    if ctx["data"]:
        files.append(("docker/scripts/fetch-data", "docker/scripts/fetch-data", EXECUTABLE))
        files.append(("docker/scripts/stage-training-data", "docker/scripts/stage-training-data", EXECUTABLE))
    return files


def _generated(ctx: dict[str, Any]) -> list[OutputFile]:
    """Files written straight from the spec rather than from a template."""
    out = []
    if ctx["pip_packages"]:
        text = "# Python packages requested in the app spec\n" + "\n".join(ctx["pip_packages"]) + "\n"
        out.append(OutputFile("docker/requirements.txt", text.encode()))
    if ctx["conda_packages"]:
        env = {
            "name": "apps",
            "channels": ctx["app"]["software"]["conda"]["channels"],
            "dependencies": ctx["conda_packages"],
        }
        text = (
            "# Conda packages requested in the app spec, installed into /opt/conda/envs/apps\n"
            + yaml.safe_dump(env, sort_keys=False, default_flow_style=False)
        )
        out.append(OutputFile("docker/conda/environment.yml", text.encode()))
        names = "\n".join(ctx["conda_names"]) + "\n"
        out.append(OutputFile("docker/conda/packages.txt", names.encode()))
    return out


def render(spec: Spec, interface: str, defaults: dict[str, Any]) -> list[OutputFile]:
    """All files of the app for ``interface``, including the app-creator manifest."""
    ctx = context(spec, interface, defaults)
    env = _environment(interface)
    files: list[OutputFile] = []
    for template, path, mode in _plan(ctx):
        if template.endswith(".j2"):
            content = env.get_template(template).render(**ctx).encode()
        else:
            source = _find_static(interface, template)
            content = source.read_bytes()
        files.append(OutputFile(path, content, mode))
    files.extend(_generated(ctx))
    files.sort(key=lambda f: f.path)

    manifest = {
        "generator": "training-environment-app-creator",
        "generator_version": __version__,
        "creator_repo": ctx["creator_repo"],
        "spec": ctx["spec_path"],
        "spec_sha256": hashlib.sha256(
            json.dumps(spec.raw, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
        "app": ctx["app"]["name"],
        "interface": interface,
        "version": ctx["app"]["version"],
        "image": f"{ctx['image']}:{ctx['image_tag']}",
        "files": [f.path for f in files],
    }
    files.append(OutputFile(MANIFEST, (json.dumps(manifest, indent=2) + "\n").encode()))
    return files


def _find_static(interface: str, name: str) -> Path:
    for base in (TEMPLATES / interface, TEMPLATES / "common"):
        path = base / name
        if path.is_file():
            return path
    raise FileNotFoundError(f"no template {name} for {interface}")


def write(files: list[OutputFile], outdir: str | Path) -> None:
    """Write ``files`` under ``outdir``, with their modes."""
    outdir = Path(outdir)
    for f in files:
        target = outdir / f.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f.content)
        os.chmod(target, f.mode)


def read_manifest(repo_dir: str | Path) -> dict[str, Any] | None:
    path = Path(repo_dir) / MANIFEST
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def is_executable(path: Path) -> bool:
    return bool(path.stat().st_mode & stat.S_IXUSR)
