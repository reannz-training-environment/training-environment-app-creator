"""Load, check and fill in app specs."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schema" / "app.schema.json"
DEFAULTS_PATH = ROOT / "config" / "defaults.yml"

# label: shown on the dashboard and in docs; short: used in k8s container names
INTERFACES = {
    "jupyter": {"label": "JupyterLab", "short": "jupyter"},
    "rstudio": {"label": "RStudio", "short": "rstudio"},
    "codeserver": {"label": "VS Code", "short": "vscode"},
}

# The cards the GPU emulator knows, in the order the NeSI GPU app lists them.
# slurm: what --gpus-per-node asks for; memory: the real board's size.
GPU_CARDS = {
    "l4": {"label": "NVIDIA L4", "memory": "24 GB"},
    "a100_40": {"label": "NVIDIA A100 40 GB", "memory": "40 GB"},
    "a100": {"label": "NVIDIA A100 80 GB", "memory": "80 GB"},
    "h100": {"label": "NVIDIA H100 NVL", "memory": "94 GB"},
    "pro_6000": {"label": "NVIDIA RTX PRO 6000", "memory": "96 GB"},
}

VRAM_OPTIONS = ["100MiB", "200MiB", "512MiB", "1GiB", "2GiB", "4GiB", "full"]

ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".tar", ".zip")


class SpecError(Exception):
    """A spec that cannot be used. ``errors`` lists every problem found."""

    def __init__(self, path: str, errors: list[str]):
        self.path = path
        self.errors = errors
        super().__init__(f"{path}: " + "; ".join(errors))


@dataclass
class Spec:
    """A checked, filled-in app spec."""

    path: str
    raw: dict[str, Any]
    app: dict[str, Any]
    warnings: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.app["name"]


def load_defaults(path: Path = DEFAULTS_PATH) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_schema(path: Path = SCHEMA_PATH) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def read_yaml(path: str | Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _schema_errors(raw: Any, schema: dict[str, Any]) -> list[str]:
    validator = Draft202012Validator(schema)
    errors = []
    for err in sorted(validator.iter_errors(raw), key=lambda e: list(e.absolute_path)):
        where = "/".join(str(p) for p in err.absolute_path) or "(top level)"
        errors.append(f"{where}: {err.message}")
    return errors


def _bad_relative_path(value: str) -> bool:
    parts = PurePosixPath(value).parts
    return any(p in ("..", ".") for p in parts) or ".git" in parts


def _url_basename(url: str) -> str:
    return PurePosixPath(urlparse(url).path).name


def _archive_suffix(filename: str) -> str:
    lower = filename.lower()
    for suffix in ARCHIVE_SUFFIXES:
        if lower.endswith(suffix):
            return suffix
    return ""


def _default_dest(item: dict[str, Any]) -> str:
    if item["type"] == "github":
        return item["repo"].split("/", 1)[1]
    filename = _url_basename(item["url"])
    suffix = _archive_suffix(filename)
    stem = filename[: -len(suffix)] if suffix else filename.rsplit(".", 1)[0]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip(".-")
    return stem or "data"


def conda_package_name(spec: str) -> str:
    """'bioconda::samtools=1.21' -> 'samtools', 'python >=3.10' -> 'python'."""
    spec = spec.split("::", 1)[-1]
    return re.split(r"[ =<>!~]", spec, maxsplit=1)[0].strip().lower()


def normalise(raw: dict[str, Any], defaults: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Fill in every optional field, so templates never have to guess.

    Returns the filled-in app and a list of warnings: things that are allowed
    but probably not what the author meant.
    """
    warnings: list[str] = []
    pins = defaults["pins"]
    app: dict[str, Any] = {
        "schema_version": raw["schema_version"],
        "name": raw["name"],
        "title": raw["title"].strip(),
        "version": raw.get("version", "0.1.0"),
        "visibility": raw.get("visibility", "public"),
        "maintainers": list(raw.get("maintainers", [])),
        "workshop_url": raw.get("workshop_url", ""),
    }
    # keep the order of the interface list stable, whatever order it was given in
    app["interfaces"] = [i for i in INTERFACES if i in raw["interfaces"]]
    app["description"] = (raw.get("description") or "").strip() or (
        f"Training environment for {app['title']}."
    )

    features = raw.get("features", {})
    gpu_raw = features.get("gpu", {})
    gpu = {
        "enabled": bool(gpu_raw.get("enabled", False)),
        "cards": [c for c in GPU_CARDS if c in gpu_raw.get("cards", list(GPU_CARDS))],
        "vram": gpu_raw.get("vram", "200MiB"),
        "pytorch": bool(gpu_raw.get("pytorch", True)),
        "numba": bool(gpu_raw.get("numba", True)),
        "session_form": bool(gpu_raw.get("session_form", True)),
    }
    slurm_raw = features.get("slurm", {})
    slurm = {
        "enabled": bool(slurm_raw.get("enabled", False)),
        "partition": slurm_raw.get("partition", "compute"),
        "node_name": slurm_raw.get("node_name", "node001"),
    }
    if gpu["enabled"]:
        if slurm_raw.get("enabled") is False:
            warnings.append(
                "features.slurm.enabled is false, but the GPU emulator always comes with "
                "its Slurm emulator (it is how a GPU is requested), so Slurm is on"
            )
        slurm["enabled"] = True
        if "partition" in slurm_raw or "node_name" in slurm_raw:
            warnings.append(
                "features.slurm.partition and node_name only apply without GPUs; the GPU "
                "emulator's node is gpunode001 in partition gpu"
            )
        # the names the GPU emulator uses, so docs and smoke tests say the same
        slurm["partition"] = "gpu"
        slurm["node_name"] = "gpunode001"
    lmod = {"enabled": bool(features.get("lmod", {}).get("enabled", False))}
    app["features"] = {"gpu": gpu, "slurm": slurm, "lmod": lmod}

    software = raw.get("software", {})
    conda = software.get("conda", {})
    r = software.get("r", {})
    app["software"] = {
        "apt": list(software.get("apt", [])),
        "pip": list(software.get("pip", [])),
        "conda": {
            "channels": list(conda.get("channels", ["conda-forge", "bioconda"])),
            "packages": list(conda.get("packages", [])),
        },
        "r": {
            "cran": list(r.get("cran", [])),
            "bioconductor": list(r.get("bioconductor", [])),
            "github": list(r.get("github", [])),
        },
        "vscode_extensions": list(software.get("vscode_extensions", [])),
    }
    if app["software"]["conda"]["packages"] and not app["software"]["conda"]["channels"]:
        app["software"]["conda"]["channels"] = ["conda-forge"]

    names = [conda_package_name(p) for p in app["software"]["conda"]["packages"]]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise SpecError("", [f"software.conda.packages lists {', '.join(duplicates)} more than once"])

    has_r = any(app["software"]["r"].values())
    if has_r and "rstudio" not in app["interfaces"]:
        warnings.append("software.r is only installed in the RStudio app, and this app has no RStudio interface")
    if app["software"]["vscode_extensions"] and "codeserver" not in app["interfaces"]:
        warnings.append("software.vscode_extensions is only installed in the VS Code app, and this app has no VS Code interface")
    if lmod["enabled"] and not app["software"]["conda"]["packages"]:
        warnings.append(
            "features.lmod is on but there are no conda packages, so `module avail` will "
            "only list Lmod's own modules"
        )

    resources = raw.get("resources", {})
    wall = resources.get("wall_time_hours", {})
    app["resources"] = {
        "cpu": resources.get("cpu", 4 if gpu["enabled"] else 2),
        "memory_gb": resources.get("memory_gb", 8 if gpu["enabled"] else 4),
        "wall_time_hours": {
            "default": wall.get("default", 8),
            "min": wall.get("min", min(4, wall.get("default", 8))),
            "max": wall.get("max", max(12, wall.get("default", 8))),
        },
    }
    if gpu["enabled"] and app["resources"]["cpu"] < 4:
        warnings.append(
            "emulated GPU utilisation is derived from CPU use, so with fewer than 4 CPUs "
            "every small job pins the GPU meter at 100%"
        )

    data = []
    for i, item in enumerate(raw.get("data", [])):
        entry = copy.deepcopy(item)
        if entry["type"] == "github":
            entry.setdefault("ref", "HEAD")
            entry.setdefault("path", "")
            if entry["ref"] == "HEAD" or not re.fullmatch(r"[0-9a-f]{40}", entry["ref"]):
                warnings.append(
                    f"data[{i}] ({entry['repo']}@{entry['ref']}) is not pinned to a commit, so "
                    "rebuilding the image may pick up different files"
                )
        else:
            filename = _url_basename(entry["url"])
            entry.setdefault("extract", bool(_archive_suffix(filename)))
            entry.setdefault("sha256", "")
            entry["filename"] = filename or "download"
        entry.setdefault("dest", _default_dest(entry))
        data.append(entry)
    app["data"] = data

    advanced = raw.get("advanced", {})
    app["advanced"] = {
        "start_dir": advanced.get("start_dir", ""),
        "rstudio_image": advanced.get("rstudio_image", pins["rstudio_image"]),
        "r_version": advanced.get("r_version", pins["r_version"]),
        "dockerfile": (advanced.get("dockerfile") or "").strip(),
        "startup": (advanced.get("startup") or "").strip(),
    }
    return app, warnings


def check(raw: Any, path: str, schema: dict[str, Any], defaults: dict[str, Any]) -> Spec:
    """Validate a parsed spec and fill it in. Raises SpecError listing every problem."""
    errors = _schema_errors(raw, schema)
    if errors:
        raise SpecError(path, errors)

    errors = []
    wall = raw.get("resources", {}).get("wall_time_hours", {})
    lo, default, hi = wall.get("min"), wall.get("default"), wall.get("max")
    if lo is not None and hi is not None and lo > hi:
        errors.append("resources.wall_time_hours: min is larger than max")
    if default is not None:
        if lo is not None and default < lo:
            errors.append("resources.wall_time_hours: default is below min")
        if hi is not None and default > hi:
            errors.append("resources.wall_time_hours: default is above max")
    for i, item in enumerate(raw.get("data", [])):
        for key in ("path", "dest"):
            if key in item and _bad_relative_path(item[key]):
                errors.append(f"data[{i}].{key}: must not contain '.', '..' or '.git' parts")
        if item["type"] == "url" and not _url_basename(item["url"]):
            errors.append(f"data[{i}].url: must end in a file name")
    start_dir = raw.get("advanced", {}).get("start_dir")
    if start_dir and _bad_relative_path(start_dir):
        errors.append("advanced.start_dir: must not contain '.', '..' or '.git' parts")
    if errors:
        raise SpecError(path, errors)

    try:
        app, warnings = normalise(raw, defaults)
    except SpecError as exc:
        raise SpecError(path, exc.errors) from None
    return Spec(path=path, raw=raw, app=app, warnings=warnings)


def load(path: str | Path, schema: dict[str, Any] | None = None, defaults: dict[str, Any] | None = None) -> Spec:
    """Read, validate and fill in the spec at ``path``."""
    path = Path(path)
    try:
        raw = read_yaml(path)
    except yaml.YAMLError as exc:
        raise SpecError(str(path), [f"not valid YAML: {exc}"]) from None
    spec = check(raw, str(path), schema or load_schema(), defaults or load_defaults())
    if path.parent.name == "apps" and path.stem != spec.name:
        raise SpecError(str(path), [f"file name must match the app name: apps/{spec.name}.yml"])
    return spec
