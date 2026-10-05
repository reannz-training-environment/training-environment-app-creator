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

# The cards the GPU emulator knows, by the name --gpus-per-node asks for, in
# the order the NeSI GPU app lists them. label tells cards apart on its own;
# model is the card without its memory; memory is the real board's.
GPU_CARDS = {
    "l4": {"label": "NVIDIA L4", "model": "NVIDIA L4", "memory": "24 GB"},
    "a100_40": {"label": "NVIDIA A100 40 GB", "model": "NVIDIA A100", "memory": "40 GB"},
    "a100": {"label": "NVIDIA A100 80 GB", "model": "NVIDIA A100", "memory": "80 GB"},
    "h100": {"label": "NVIDIA H100 NVL", "model": "NVIDIA H100 NVL", "memory": "94 GB"},
    "pro_6000": {"label": "NVIDIA RTX PRO 6000", "model": "NVIDIA RTX PRO 6000", "memory": "96 GB"},
}

VRAM_OPTIONS = ["100MiB", "200MiB", "512MiB", "1GiB", "2GiB", "4GiB", "full"]

# The Ubuntu each rocker image of R is built on, and its Python: rocker moved
# from 20.04 to 22.04 at R 4.2.2, and to 24.04 at R 4.4.2. Its R 3 images are
# Debian releases too old to build these apps on, so R 3 comes from Posit's
# builds of R (https://github.com/rstudio/r-builds) instead, on rocker's
# oldest Ubuntu image, R3_BASE.
UBUNTU_RELEASES = {"focal": "20.04", "jammy": "22.04", "noble": "24.04"}
UBUNTU_PYTHON = {"focal": "3.8", "jammy": "3.10", "noble": "3.12"}
R3_BASE = "4.0.1"

# The CRAN snapshot an R 3 app's packages come from: the day before the R
# release that followed it, so they are the versions that worked with it.
# Posit's snapshots start in October 2017.
R3_SNAPSHOTS = {
    "3.4.2": "2017-11-29",
    "3.4.3": "2018-03-14",
    "3.4.4": "2018-04-22",
    "3.5.0": "2018-07-01",
    "3.5.1": "2018-12-19",
    "3.5.2": "2019-03-10",
    "3.5.3": "2019-04-25",
    "3.6.0": "2019-07-04",
    "3.6.1": "2019-12-11",
    "3.6.2": "2020-02-28",
    "3.6.3": "2020-04-23",
}

# JupyterLab 3.6, the oldest still installed by pip, needs Python 3.7; the
# Slurm and GPU emulators need Python 3.10
OLDEST_PYTHON = (3, 7)
EMULATOR_PYTHON = (3, 10)

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


def version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def rstudio_base(r_version: str) -> dict[str, Any]:
    """The rocker image tag an RStudio app with this R is built from, its
    Ubuntu, and, for R 3, the CRAN snapshot of its packages."""
    version = version_tuple(r_version)
    if version < (4, 0, 0):
        return {"tag": R3_BASE, "ubuntu": "focal", "posit_r": True, "snapshot": R3_SNAPSHOTS.get(r_version, "")}
    ubuntu = "focal" if version < (4, 2, 2) else "jammy" if version < (4, 4, 2) else "noble"
    return {"tag": r_version, "ubuntu": ubuntu, "posit_r": False, "snapshot": ""}


def _version_errors(app: dict[str, Any]) -> list[str]:
    """Python and R versions the app cannot have, with the features it has."""
    errors = []
    advanced = app["advanced"]
    python = advanced["python_version"]
    # the GPU emulator always brings the Slurm one
    emulators = app["features"]["slurm"]["enabled"]
    if python and version_tuple(python) < OLDEST_PYTHON:
        errors.append(f"advanced.python_version: Python {python} is too old; JupyterLab and pip need Python 3.7 or newer")
    elif python and emulators and version_tuple(python) < EMULATOR_PYTHON:
        errors.append(f"advanced.python_version: the Slurm and GPU emulators need Python 3.10 or newer, not {python}")
    if "rstudio" not in app["interfaces"]:
        return errors
    r = advanced["r_version"]
    base = rstudio_base(r)
    if base["posit_r"] and not base["snapshot"]:
        errors.append(f"advanced.r_version: the app creator cannot make R {r}; of R 3, it can make {', '.join(R3_SNAPSHOTS)}")
    if base["posit_r"] and advanced["rstudio_image"] != "rocker/rstudio":
        errors.append(
            f"advanced.rstudio_image: {advanced['rstudio_image']} comes with packages built for R 4, so it cannot "
            f"have R {r}; use rocker/rstudio, and list the R packages the app needs"
        )
    if emulators and not python and base["ubuntu"] == "focal":
        errors.append(
            f"advanced.python_version: with R {r}, the RStudio image is Ubuntu 20.04, whose Python "
            f"{UBUNTU_PYTHON['focal']} is too old for the Slurm and GPU emulators; choose Python 3.10 or newer"
        )
    return errors


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
        "mode": gpu_raw.get("mode", "all"),
        "cards": [c for c in GPU_CARDS if c in gpu_raw.get("cards", list(GPU_CARDS))],
        "vram": gpu_raw.get("vram", "200MiB"),
        "pytorch": bool(gpu_raw.get("pytorch", True)),
        "numba": bool(gpu_raw.get("numba", True)),
        "session_form": bool(gpu_raw.get("session_form", True)),
    }
    slurm_raw = features.get("slurm", {})
    # Mahuika-like names; NeSI's svisit recognises node names like c001
    slurm = {
        "enabled": bool(slurm_raw.get("enabled", False)),
        "partition": slurm_raw.get("partition", "milan"),
        "node_name": slurm_raw.get("node_name", "c001"),
    }
    if gpu["enabled"] and gpu["mode"] == "choose" and len(gpu["cards"]) == 1:
        warnings.append(
            "features.gpu.mode is 'choose' but there is only one card to choose from; the launch "
            "form will show a menu with one entry"
        )
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
        # empty: the image's own python3
        "python_version": advanced.get("python_version", ""),
        "dockerfile": (advanced.get("dockerfile") or "").strip(),
        "startup": (advanced.get("startup") or "").strip(),
    }
    errors = _version_errors(app)
    if errors:
        raise SpecError("", errors)
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
