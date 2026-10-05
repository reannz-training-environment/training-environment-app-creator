"""What the spec checks accept and refuse."""

from __future__ import annotations

import copy

import pytest

from te_app_creator.spec import SpecError, check, conda_package_name, rstudio_base

MINIMAL = {"schema_version": 1, "name": "intro-python", "title": "Intro to Python", "interfaces": ["jupyter"]}


def spec_with(**changes):
    raw = copy.deepcopy(MINIMAL)
    raw.update(changes)
    return raw


def errors_of(raw, schema, defaults):
    with pytest.raises(SpecError) as exc:
        check(raw, "test.yml", schema, defaults)
    return " ".join(exc.value.errors)


def test_minimal_spec_gets_defaults(schema, defaults):
    app = check(MINIMAL, "test.yml", schema, defaults).app
    assert app["version"] == "0.1.0"
    assert app["visibility"] == "public"
    assert app["resources"] == {"cpu": 2, "memory_gb": 4, "wall_time_hours": {"default": 8, "min": 4, "max": 12}}
    assert not app["features"]["gpu"]["enabled"]
    assert not app["features"]["slurm"]["enabled"]
    assert app["advanced"]["rstudio_image"] == defaults["pins"]["rstudio_image"]


@pytest.mark.parametrize("name", ["intro_python", "Intro", "-intro", "intro-", "a", "intro--python", "1intro"])
def test_bad_names_are_refused(schema, defaults, name):
    assert "name" in errors_of(spec_with(name=name), schema, defaults)


def test_unknown_keys_are_refused(schema, defaults):
    assert "Additional properties" in errors_of(spec_with(colour="blue"), schema, defaults)


def test_an_interface_is_required(schema, defaults):
    errors_of(spec_with(interfaces=[]), schema, defaults)
    errors_of(spec_with(interfaces=["matlab"]), schema, defaults)


def test_interfaces_keep_a_stable_order(schema, defaults):
    app = check(spec_with(interfaces=["codeserver", "jupyter"]), "t.yml", schema, defaults).app
    assert app["interfaces"] == ["jupyter", "codeserver"]


def test_gpu_turns_slurm_on_with_the_emulator_names(schema, defaults):
    raw = spec_with(features={"gpu": {"enabled": True}, "slurm": {"enabled": False}})
    spec = check(raw, "t.yml", schema, defaults)
    slurm = spec.app["features"]["slurm"]
    assert slurm == {"enabled": True, "partition": "gpu", "node_name": "gpunode001"}
    assert any("Slurm is on" in w for w in spec.warnings)
    # a GPU session gets enough CPUs for the utilisation meter to mean something
    assert spec.app["resources"]["cpu"] == 4


def test_wall_time_must_be_ordered(schema, defaults):
    raw = spec_with(resources={"wall_time_hours": {"min": 10, "default": 8, "max": 12}})
    assert "below min" in errors_of(raw, schema, defaults)
    raw = spec_with(resources={"wall_time_hours": {"min": 10, "max": 4}})
    assert "min is larger than max" in errors_of(raw, schema, defaults)


@pytest.mark.parametrize(
    "item",
    [
        {"type": "github", "repo": "nesi/intro-python", "path": "../etc"},
        {"type": "github", "repo": "nesi/intro-python", "dest": "a/../../b"},
        {"type": "github", "repo": "nesi/intro-python", "dest": "/abs"},
        {"type": "github", "repo": "nesi/intro-python", "ref": "main; rm -rf /"},
        {"type": "github", "repo": "nesi/intro python"},
        {"type": "url", "url": "https://example.org/$(whoami).zip"},
        {"type": "url", "url": "https://example.org/a\".zip"},
        {"type": "url", "url": "ftp://example.org/data.zip"},
        {"type": "url", "url": "https://example.org/"},
    ],
)
def test_unsafe_data_sources_are_refused(schema, defaults, item):
    errors_of(spec_with(data=[item]), schema, defaults)


def test_data_defaults(schema, defaults):
    raw = spec_with(
        data=[
            {"type": "github", "repo": "nesi/intro-python"},
            {"type": "url", "url": "https://example.org/files/shell_data.tar.gz?raw=1"},
            {"type": "url", "url": "https://example.org/files/table.csv"},
        ]
    )
    spec = check(raw, "t.yml", schema, defaults)
    github, archive, plain = spec.app["data"]
    assert (github["ref"], github["path"], github["dest"]) == ("HEAD", "", "intro-python")
    assert (archive["extract"], archive["dest"]) == (True, "shell_data")
    assert (plain["extract"], plain["dest"]) == (False, "table")
    assert any("not pinned" in w for w in spec.warnings)


@pytest.mark.parametrize(
    "packages",
    [["samtools; rm -rf /"], ["samtools$(id)"], ["sam tools"], ["`id`"], ["samtools=1.21 extra"]],
)
def test_bad_conda_packages_are_refused(schema, defaults, packages):
    errors_of(spec_with(software={"conda": {"packages": packages}}), schema, defaults)


@pytest.mark.parametrize(
    "package",
    [
        "samtools",
        "samtools=1.21",
        "samtools==1.21",
        "bioconda::samtools=1.21",
        "samtools=1.21=h50ea8bc_0",
        "python >=3.10,<3.13",
        "seqkit>=2.5",
        "r-base=4.3.*",
    ],
)
def test_conda_package_specs_are_accepted(schema, defaults, package):
    check(spec_with(software={"conda": {"packages": [package]}}), "t.yml", schema, defaults)


def test_duplicate_conda_packages_are_refused(schema, defaults):
    raw = spec_with(software={"conda": {"packages": ["samtools", "bioconda::samtools=1.21"]}})
    assert "more than once" in errors_of(raw, schema, defaults)


@pytest.mark.parametrize(
    "spec, name",
    [
        ("samtools", "samtools"),
        ("samtools=1.21", "samtools"),
        ("bioconda::samtools=1.21", "samtools"),
        ("python >=3.10,<3.13", "python"),
        ("R-Base", "r-base"),
    ],
)
def test_conda_package_names(spec, name):
    assert conda_package_name(spec) == name


def test_r_and_extensions_warn_without_their_interface(schema, defaults):
    raw = spec_with(software={"r": {"cran": ["vegan"]}, "vscode_extensions": ["ms-python.python"]})
    warnings = check(raw, "t.yml", schema, defaults).warnings
    assert any("RStudio" in w for w in warnings)
    assert any("VS Code" in w for w in warnings)


def test_python_is_the_images_own_unless_chosen(schema, defaults):
    assert check(MINIMAL, "t.yml", schema, defaults).app["advanced"]["python_version"] == ""
    app = check(spec_with(advanced={"python_version": "3.11.6"}), "t.yml", schema, defaults).app
    assert app["advanced"]["python_version"] == "3.11.6"


@pytest.mark.parametrize("version", ["2.7.18", "3.11", "python3.11", "3.11.6 ", "4.0.0"])
def test_bad_python_versions_are_refused(schema, defaults, version):
    assert "python_version" in errors_of(spec_with(advanced={"python_version": version}), schema, defaults)


def test_python_too_old_for_jupyterlab_is_refused(schema, defaults):
    assert "Python 3.6.15 is too old" in errors_of(spec_with(advanced={"python_version": "3.6.15"}), schema, defaults)
    check(spec_with(advanced={"python_version": "3.7.3"}), "t.yml", schema, defaults)


@pytest.mark.parametrize("feature", ["slurm", "gpu"])
def test_the_emulators_need_python_3_10(schema, defaults, feature):
    raw = spec_with(features={feature: {"enabled": True}}, advanced={"python_version": "3.9.9"})
    assert "the Slurm and GPU emulators need Python 3.10 or newer, not 3.9.9" in errors_of(raw, schema, defaults)
    raw["advanced"]["python_version"] = "3.10.5"
    check(raw, "t.yml", schema, defaults)


def test_each_r_version_has_its_rocker_image():
    assert rstudio_base("4.6.0") == {"tag": "4.6.0", "ubuntu": "noble", "posit_r": False, "snapshot": ""}
    assert rstudio_base("4.4.2")["ubuntu"] == "noble"
    assert rstudio_base("4.4.1")["ubuntu"] == "jammy"
    assert rstudio_base("4.2.2")["ubuntu"] == "jammy"
    assert rstudio_base("4.2.1")["ubuntu"] == "focal"
    assert rstudio_base("4.0.1")["ubuntu"] == "focal"
    # R 3 comes from Posit, on rocker's oldest Ubuntu image
    assert rstudio_base("3.6.2") == {"tag": "4.0.1", "ubuntu": "focal", "posit_r": True, "snapshot": "2020-02-28"}


def test_r_3_needs_rocker_rstudio_and_a_known_version(schema, defaults):
    def rstudio(**advanced):
        return spec_with(interfaces=["rstudio"], advanced=advanced)

    check(rstudio(r_version="3.5.3"), "t.yml", schema, defaults)
    assert "use rocker/rstudio" in errors_of(rstudio(r_version="3.6.2", rstudio_image="rocker/tidyverse"), schema, defaults)
    assert "cannot make R 3.3.3" in errors_of(rstudio(r_version="3.3.3"), schema, defaults)
    # without RStudio, the R version does not matter
    check(spec_with(advanced={"r_version": "3.3.3"}), "t.yml", schema, defaults)


def test_emulators_with_old_r_need_a_newer_python(schema, defaults):
    raw = spec_with(interfaces=["rstudio"], features={"slurm": {"enabled": True}}, advanced={"r_version": "4.1.0"})
    assert "Ubuntu 20.04, whose Python 3.8 is too old" in errors_of(raw, schema, defaults)
    raw["advanced"]["python_version"] = "3.11.6"
    check(raw, "t.yml", schema, defaults)
    # newer R's images have Python 3.10 or newer
    raw["advanced"] = {"r_version": "4.3.2"}
    check(raw, "t.yml", schema, defaults)
