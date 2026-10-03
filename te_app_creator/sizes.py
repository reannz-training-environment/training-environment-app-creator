"""How much space a built app takes, and what takes it.

Every data source, and every kind of software an app spec asks for, is its own
step in the generated Dockerfile, so the image's layer history says how much
space each one adds. Two numbers matter when planning a workshop:

* the image, which every worker node that runs the app stores once
* the data, which is copied into the home directory of every learner
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any

from . import render
from .spec import INTERFACES, Spec

# fetch-data github "owner/repo" "ref" "path" "dest" / fetch-data url "url" yes "-" "dest"
DATA_STEP = re.compile(r'fetch-data (github|url) "([^"]*)" .*"([^"]*)"')

# (key, label, words that mark a layer as belonging to it), checked in order:
# Slurm comes first because the Slurm-only step also unpacks the emulator into
# /opt/gpuemu
PARTS = [
    ("slurm", "Slurm emulator, seff and svisit", ("slurm-cpu-node", "slurm-nesi-tools", "opt-nesi-bin", "slurm-emulator")),
    ("gpu", "Emulated GPUs (emulator, nvtop, Python GPU stack)",
     ("Syllo/nvtop", "nvidia-ml-py", "download.pytorch.org", "/opt/gpuemu", "gpuemu_autostart", "gpu-banner")),
    ("lmod", "Lmod", ("TACC/Lmod",)),
    ("conda", "Conda packages", ("micromamba",)),
    ("pip", "Python packages (pip)", ("install -r /opt/app-creator/requirements.txt",)),
    ("r", "R packages", ("install-r-packages.R",)),
    ("apt", "System packages (apt)", ("apt-packages.txt",)),
    ("vscode", "VS Code extensions", ("--install-extension",)),
]

BASE_LABEL = {
    "jupyter": "JupyterLab and the base system",
    "rstudio": "R, RStudio and the base system",
    "codeserver": "VS Code and the base system",
}


def human(n: float) -> str:
    """Bytes as GB or MB, the way Docker counts them (1 GB = 1000^3 bytes)."""
    if n >= 1e9:
        return f"{n / 1e9:.1f} GB"
    if n >= 1e6:
        return f"{n / 1e6:.0f} MB"
    if n >= 1e3:
        return f"{n / 1e3:.0f} kB"
    return f"{int(n)} B"


def _docker(*args: str) -> str:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True).stdout


def history(image: str) -> list[tuple[int, str]]:
    """(size in bytes, the instruction that made it) for every layer."""
    layers = []
    for line in _docker("history", "--no-trunc", "--human=false", "--format", "{{json .}}", image).splitlines():
        entry = json.loads(line)
        layers.append((int(entry.get("Size") or 0), entry.get("CreatedBy") or ""))
    return layers


def download_size(image: str) -> int:
    """About what a worker node downloads: the image's layers, gzip compressed."""
    out = subprocess.run(
        ["sh", "-c", 'docker save "$1" | gzip -1 | wc -c', "sh", image],
        check=True, capture_output=True, text=True,
    ).stdout
    return int(out.strip())


def instruction(created_by: str) -> str:
    """The command of a layer, without the build arguments docker lists first.

    `RUN |2 A=1 B=https://... /bin/sh -c <command>`: every ARG in scope is
    listed on every later RUN, so matching on them would give a layer to
    whichever feature declared an ARG before it.
    """
    if created_by.startswith("RUN |") and "/bin/sh -c " in created_by:
        return created_by.split("/bin/sh -c ", 1)[1]
    return created_by


def attribute(layers: list[tuple[int, str]], interface: str) -> dict[str, Any]:
    """Split layer sizes into data destinations and kinds of software."""
    data: dict[str, int] = {}
    parts = {key: 0 for key, _, _ in PARTS}
    base = 0
    for size, created_by in layers:
        created_by = instruction(created_by)
        match = DATA_STEP.search(created_by)
        if match:
            dest = match.group(3)
            data[dest] = data.get(dest, 0) + size
            continue
        for key, _, words in PARTS:
            if any(word in created_by for word in words):
                parts[key] += size
                break
        else:
            base += size
    labels = {key: label for key, label, _ in PARTS}
    return {
        "data": [{"dest": dest, "bytes": size} for dest, size in data.items()],
        "data_bytes": sum(data.values()),
        "parts": [{"key": key, "label": labels[key], "bytes": size} for key, size in parts.items() if size]
        + [{"key": "base", "label": BASE_LABEL[interface], "bytes": base}],
    }


def measure(image: str, spec: Spec, interface: str, defaults: dict[str, Any], download: bool = True) -> dict[str, Any]:
    layers = history(image)
    report = {
        "app": spec.name,
        "interface": interface,
        "repo": render.repo_name(defaults, interface, spec.name),
        "image_bytes": int(_docker("image", "inspect", "--format", "{{.Size}}", image).strip()),
        "download_bytes": download_size(image) if download else None,
    }
    report.update(attribute(layers, interface))
    return report


def markdown(report: dict[str, Any]) -> str:
    """One app's sizes, as the pull request comment shows them."""
    label = INTERFACES[report["interface"]]["label"]
    lines = [
        f"**{label}** (`{report['repo']}`)",
        "",
        "| | Size |",
        "| --- | --- |",
    ]
    if report.get("download_bytes"):
        lines.append(f"| Image download, once per worker node | about {human(report['download_bytes'])} |")
    lines.append(f"| Image on disk, once per worker node | {human(report['image_bytes'])} |")
    if report["data"]:
        lines.append(f"| Data copied into **each learner's** home directory | {human(report['data_bytes'])} |")
    for item in report["data"]:
        lines.append(f"| &nbsp;&nbsp;data: `~/{item['dest']}` | {human(item['bytes'])} |")
    for part in report["parts"]:
        lines.append(f"| &nbsp;&nbsp;{part['label']} | {human(part['bytes'])} |")
    lines.append("")
    return "\n".join(lines)
