"""Attributing an image's layers to data and software, and reporting them."""

from __future__ import annotations

import json
import subprocess
import sys

from conftest import ROOT
from te_app_creator import sizes

MB = 1_000_000

# What `docker history` says about a kitchen-sink-like JupyterLab image, newest first
LAYERS = [
    (4 * MB, 'RUN /bin/sh -c bash /opt/app-creator/smoke-test.sh # buildkit'),
    (0, 'COPY scripts/smoke-test.sh /opt/app-creator/smoke-test.sh # buildkit'),
    (118 * MB, 'RUN /bin/sh -c fetch-data url "https://example.org/shell_data.zip" yes "-" "shell_data" # buildkit'),
    (2 * MB, 'RUN /bin/sh -c fetch-data github "nesi/intro-python" "v0.5.0" "data" "intro-python/data" # buildkit'),
    (90 * MB, 'RUN /bin/sh -c pip3 --no-cache-dir install -r /opt/app-creator/requirements.txt # buildkit'),
    (150 * MB, 'RUN /bin/sh -c pip3 --no-cache-dir install jupyterlab # buildkit'),
    (400 * MB, 'RUN /bin/sh -c micromamba create -y -p /opt/conda/envs/apps -f /opt/app-creator/conda/environment.yml && ... # buildkit'),
    (12 * MB, 'RUN /bin/sh -c curl -fsSL -o /usr/local/bin/micromamba "https://github.com/mamba-org/micromamba-releases/..." # buildkit'),
    (3 * MB, 'RUN /bin/sh -c SLURM_PY="$(python3 -c \'import gpuemu.slurm as s\')" ... opt-nesi-bin ... # buildkit'),
    (1 * MB, 'COPY scripts/gpu-banner.sh /etc/profile.d/gpuemu.sh # buildkit'),
    (1800 * MB, 'RUN /bin/sh -c pip3 --no-cache-dir install nvidia-ml-py matplotlib numba && pip3 install --index-url https://download.pytorch.org/whl/cpu torch # buildkit'),
    (20 * MB, 'RUN /bin/sh -c mkdir -p /tmp/gpu-app && ... make PREFIX=/opt/gpuemu install ... # buildkit'),
    (30 * MB, 'RUN /bin/sh -c apt-get update && ... git clone --depth 1 --branch "${NVTOP_VERSION}" https://github.com/Syllo/nvtop.git /tmp/nvtop # buildkit'),
    (15 * MB, 'RUN /bin/sh -c apt-get update && ... curl -fsSL "https://github.com/TACC/Lmod/archive/refs/tags/${LMOD_VERSION}.tar.gz" # buildkit'),
    (5 * MB, 'RUN /bin/sh -c apt-get update && xargs -a /opt/app-creator/apt-packages.txt env DEBIAN_FRONTEND=noninteractive apt-get install -y # buildkit'),
    (600 * MB, 'RUN /bin/sh -c echo tzdata ... apt-get install -y bash-completion ... # buildkit'),
    (78 * MB, '/bin/sh -c #(nop) ADD file:abc in / '),
]


def test_layers_are_attributed_to_what_the_spec_asked_for():
    report = sizes.attribute(LAYERS, "jupyter")
    data = {item["dest"]: item["bytes"] for item in report["data"]}
    assert data == {"shell_data": 118 * MB, "intro-python/data": 2 * MB}
    assert report["data_bytes"] == 120 * MB
    parts = {part["key"]: part["bytes"] for part in report["parts"]}
    assert parts["gpu"] == (1 + 1800 + 20 + 30) * MB
    assert parts["slurm"] == 3 * MB
    assert parts["conda"] == 412 * MB
    assert parts["pip"] == 90 * MB
    assert parts["lmod"] == 15 * MB
    assert parts["apt"] == 5 * MB
    assert parts["base"] == (4 + 150 + 600 + 78) * MB
    assert report["parts"][-1]["label"] == "JupyterLab and the base system"
    assert "r" not in parts and "vscode" not in parts


def test_slurm_only_emulator_is_not_counted_as_gpu():
    layers = [(9 * MB, 'RUN /bin/sh -c mv docker/gpuemu /opt/gpuemu/src && cat /opt/app-creator/slurm-cpu-node.py >> x # buildkit')]
    parts = {p["key"]: p["bytes"] for p in sizes.attribute(layers, "jupyter")["parts"]}
    assert parts == {"slurm": 9 * MB, "base": 0}


def test_human_sizes():
    assert sizes.human(0) == "0 B"
    assert sizes.human(1_500) == "2 kB"
    assert sizes.human(118 * MB) == "118 MB"
    assert sizes.human(2_345 * MB) == "2.3 GB"


def test_markdown_and_summary_report_the_sizes(tmp_path):
    report = {
        "app": "kitchen-sink",
        "interface": "jupyter",
        "repo": "training-environment-jupyter-kitchen-sink-app",
        "image_bytes": 3_300 * MB,
        "download_bytes": 1_200 * MB,
    }
    report.update(sizes.attribute(LAYERS, "jupyter"))
    text = sizes.markdown(report)
    assert "| Image download, once per worker node | about 1.2 GB |" in text
    assert "| Image on disk, once per worker node | 3.3 GB |" in text
    assert "| Data copied into **each learner's** home directory | 120 MB |" in text
    assert "data: `~/shell_data` | 118 MB |" in text

    (tmp_path / f"{report['repo']}.json").write_text(json.dumps(report))
    proc = subprocess.run(
        [sys.executable, "-m", "te_app_creator", "summary", "examples/kitchen-sink.yml", "--sizes", str(tmp_path)],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert "#### Space needed" in proc.stdout
    assert "Image on disk, once per worker node | 3.3 GB" in proc.stdout
    # the other interfaces have no report in the directory
    assert "**RStudio**: not measured" in proc.stdout
    assert "120 MB for each learner: 3.6 GB for 30" in proc.stdout
