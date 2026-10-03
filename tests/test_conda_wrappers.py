"""make-conda-wrappers.py, run against a fake conda environment."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

from conftest import ROOT

SCRIPT = ROOT / "te_app_creator" / "templates" / "common" / "docker" / "scripts" / "make-conda-wrappers.py"


@pytest.fixture
def wrappers(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("make_conda_wrappers", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "SOFTWARE", tmp_path / "software")
    monkeypatch.setattr(module, "MODULEFILES", tmp_path / "modulefiles")
    return module


def make_package(prefix: Path, name: str, version: str, commands: list[str], channel: str = "bioconda"):
    (prefix / "conda-meta").mkdir(parents=True, exist_ok=True)
    (prefix / "bin").mkdir(parents=True, exist_ok=True)
    for command in commands:
        exe = prefix / "bin" / command
        exe.write_text(f'#!/usr/bin/env bash\necho "{command} ran with $* JAVA_HOME=${{JAVA_HOME:-unset}}"\n')
        exe.chmod(0o755)
    record = {
        "name": name,
        "version": version,
        "channel": f"https://conda.anaconda.org/{channel}/linux-64",
        "files": [f"bin/{c}" for c in commands] + [f"share/{name}/README"],
    }
    (prefix / "conda-meta" / f"{name}-{version}-h0_0.json").write_text(json.dumps(record))


@pytest.fixture
def env(tmp_path):
    prefix = tmp_path / "env"
    make_package(prefix, "samtools", "1.21", ["samtools"])
    make_package(prefix, "htslib", "1.21", ["bgzip", "tabix"])  # a dependency
    make_package(prefix, "fastqc", "0.12.1", ["fastqc"])
    make_package(prefix, "pandas", "2.2.3", [], channel="conda-forge")
    activate = prefix / "etc" / "conda" / "activate.d"
    activate.mkdir(parents=True)
    (activate / "java.sh").write_text('export JAVA_HOME="$CONDA_PREFIX/lib/jvm"\n')
    packages = tmp_path / "packages.txt"
    packages.write_text("samtools\nfastqc\npandas\n")
    return prefix, packages


def test_lmod_mode_makes_one_module_per_package(wrappers, env, tmp_path):
    prefix, packages = env
    assert wrappers.main(["--prefix", str(prefix), "--packages", str(packages), "--mode", "lmod"]) == 0

    modulefile = tmp_path / "modulefiles" / "samtools" / "1.21.lua"
    text = modulefile.read_text()
    bindir = tmp_path / "software" / "samtools" / "1.21" / "bin"
    assert f'prepend_path("PATH", "{bindir}")' in text
    assert "bioconda" in text
    assert (tmp_path / "modulefiles" / "fastqc" / "0.12.1.lua").is_file()
    # a package without commands gets no module, a dependency gets no wrapper
    assert not (tmp_path / "modulefiles" / "pandas").exists()
    assert not list((tmp_path / "software").rglob("bgzip"))

    wrapper = bindir / "samtools"
    assert os.access(wrapper, os.X_OK)
    out = subprocess.run([str(wrapper), "view", "x.bam"], capture_output=True, text=True, check=True).stdout
    assert out.strip() == f"samtools ran with view x.bam JAVA_HOME={prefix}/lib/jvm"

    rows = (tmp_path / "software" / "packages.tsv").read_text().splitlines()
    assert rows[0].split("\t") == ["name", "version", "channel", "commands", "module"]
    assert "samtools\t1.21\tbioconda\tsamtools\tsamtools/1.21" in rows
    assert "pandas\t2.2.3\tconda-forge\t\t" in rows


def test_path_mode_puts_every_command_in_one_directory(wrappers, env, tmp_path):
    prefix, packages = env
    assert wrappers.main(["--prefix", str(prefix), "--packages", str(packages), "--mode", "path"]) == 0
    bindir = tmp_path / "software" / "bin"
    assert sorted(p.name for p in bindir.iterdir()) == ["fastqc", "samtools"]
    assert not (tmp_path / "modulefiles").exists()


def test_a_missing_package_fails_the_build(wrappers, env, tmp_path):
    prefix, packages = env
    packages.write_text("samtools\nbowtie2\n")
    assert wrappers.main(["--prefix", str(prefix), "--packages", str(packages), "--mode", "path"]) == 1


def test_channel_names(wrappers):
    assert wrappers.channel_of({"channel": "https://conda.anaconda.org/conda-forge/noarch"}) == "conda-forge"
    assert wrappers.channel_of({"channel": "bioconda"}) == "bioconda"
    assert wrappers.channel_of({}) == "unknown"
