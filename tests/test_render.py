"""The generated apps: every file Open OnDemand and the build read must be usable."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest
import yaml

from conftest import EXAMPLES
from te_app_creator import render
from te_app_creator.spec import load

ERB = re.compile(r"<%=?.*?%>", re.S)


def apps(schema, defaults):
    for path in EXAMPLES:
        spec = load(path, schema, defaults)
        for interface in spec.app["interfaces"]:
            yield spec, interface


def rendered(schema, defaults):
    for spec, interface in apps(schema, defaults):
        files = render.render(spec, interface, defaults)
        yield spec, interface, {f.path: f for f in files}


def strip_erb(text: str) -> str:
    """Remove ERB, leaving placeholders where values are printed."""
    text = re.sub(r"<%[^=].*?%>", "", text, flags=re.S)
    return re.sub(r"<%=.*?%>", "ERB", text, flags=re.S)


def test_examples_exist():
    assert EXAMPLES, "examples/*.yml are the test fixtures"


def test_form_and_manifest_parse_the_way_open_ondemand_parses_them(schema, defaults):
    for spec, interface, files in rendered(schema, defaults):
        for name in ("form.yml", "manifest.yml"):
            text = files[name].content.decode()
            assert "&" not in re.sub(r"#.*", "", text).replace("&nbsp;", ""), f"{name} looks like it has a YAML anchor"
            data = yaml.safe_load(text)
            assert isinstance(data, dict)
        form = yaml.safe_load(files["form.yml"].content)
        assert form["cluster"] == defaults["ood_cluster"]
        for attribute in form["form"]:
            assert attribute in form["attributes"], f"{attribute} has no attributes entry"
        manifest = yaml.safe_load(files["manifest.yml"].content)
        assert manifest["role"] == "batch_connect"
        assert manifest["name"].startswith(spec.app["title"])


def test_gpu_form_has_one_memory_control_per_card(schema, defaults):
    for spec, interface, files in rendered(schema, defaults):
        gpu = spec.app["features"]["gpu"]
        form = yaml.safe_load(files["form.yml"].content)
        controls = [a for a in form["form"] if a.startswith("vram_")]
        if gpu["enabled"] and gpu["session_form"]:
            assert controls == [f"vram_{c}" for c in gpu["cards"]]
            for control in controls:
                values = [option[1] for option in form["attributes"][control]["options"]]
                assert gpu["vram"] in values and "off" in values
                assert form["attributes"][control]["value"] == gpu["vram"]
            submit = files["submit.yml.erb"].content.decode()
            for control in controls:
                assert f"=> {control}," in submit
        else:
            assert controls == []


def test_submit_is_yaml_once_erb_is_rendered(schema, defaults):
    for spec, interface, files in rendered(schema, defaults):
        submit = yaml.safe_load(strip_erb(files["submit.yml.erb"].content.decode()))
        container = submit["script"]["native"]["container"]
        repo = render.repo_name(defaults, interface, spec.name)
        assert container["image"] == f"ghcr.io/{defaults['github_org']}/{repo}:v{spec.app['version']}"
        assert re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", container["name"]) and len(container["name"]) <= 63
        assert container["port"] == ("8443" if interface == "codeserver" else "8080")
        mounts = {m["name"] for m in submit["script"]["native"]["mounts"]}
        assert {"home", "nslcd-socket", "nsswitch-conf"} <= mounts


EXECUTABLE_SCRIPTS = {
    "docker/scripts/fetch-data",
    "docker/scripts/stage-training-data",
    "docker/scripts/slurm-emulator",
    "docker/scripts/smoke-test.sh",
    "docker/scripts/make-conda-wrappers.py",
}


def test_scripts_are_executable(schema, defaults):
    """Open OnDemand runs the template scripts with their committed mode."""
    for spec, interface, files in rendered(schema, defaults):
        for path, f in files.items():
            if path.startswith("template/") or path in EXECUTABLE_SCRIPTS:
                assert f.mode == render.EXECUTABLE, f"{path} must be executable"
            else:
                assert f.mode == render.REGULAR, f"{path} should not be executable"


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_shell_scripts_are_valid_bash(schema, defaults, tmp_path):
    for spec, interface, files in rendered(schema, defaults):
        for path, f in files.items():
            is_shell = path.startswith("template/") or (
                path.startswith("docker/scripts/") and not path.endswith((".py", ".R"))
            )
            if not is_shell:
                continue
            script = tmp_path / "script.sh"
            script.write_text(strip_erb(f.content.decode()))
            proc = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
            assert proc.returncode == 0, f"{spec.name}/{interface}/{path}: {proc.stderr}"


def test_dockerfile_copies_only_files_that_exist(schema, defaults):
    for spec, interface, files in rendered(schema, defaults):
        dockerfile = files["docker/Dockerfile"].content.decode()
        for line in dockerfile.splitlines():
            if not line.startswith("COPY "):
                continue
            sources = line.split()[1:-1]
            for source in sources:
                if source.startswith("--"):
                    continue
                prefix = f"docker/{source.rstrip('/')}"
                assert any(p == prefix or p.startswith(prefix + "/") for p in files), (
                    f"{spec.name}/{interface}: COPY {source} has nothing to copy"
                )


def test_dockerfile_has_each_feature(schema, defaults):
    for spec, interface, files in rendered(schema, defaults):
        dockerfile = files["docker/Dockerfile"].content.decode()
        features = spec.app["features"]
        assert ("gpuemu-ctl" in files["template/script.sh.erb"].content.decode()) == features["gpu"]["enabled"]
        assert ("TACC/Lmod" in dockerfile) == features["lmod"]["enabled"]
        assert ("/bin/module" in dockerfile) == (not features["lmod"]["enabled"])
        assert ("slurm-cpu-node.py" in dockerfile) == (features["slurm"]["enabled"] and not features["gpu"]["enabled"])
        assert ("micromamba create" in dockerfile) == bool(spec.app["software"]["conda"]["packages"])
        for item in spec.app["data"]:
            assert f'"{item["dest"]}"' in dockerfile
        if interface == "rstudio":
            assert dockerfile.count("FROM ") == 1 and f"FROM {spec.app['advanced']['rstudio_image']}:" in dockerfile
        else:
            assert "FROM ubuntu:" in dockerfile
        assert dockerfile.rstrip().endswith("RUN bash /opt/app-creator/smoke-test.sh")


def test_manifest_lists_every_file(schema, defaults):
    for spec, interface, files in rendered(schema, defaults):
        manifest = json.loads(files[render.MANIFEST].content)
        assert sorted(manifest["files"]) == sorted(p for p in files if p != render.MANIFEST)
        assert manifest["interface"] == interface and manifest["app"] == spec.name


def test_rendering_is_deterministic(schema, defaults):
    first = [[(f.path, f.content, f.mode) for f in render.render(s, i, defaults)] for s, i in apps(schema, defaults)]
    second = [[(f.path, f.content, f.mode) for f in render.render(s, i, defaults)] for s, i in apps(schema, defaults)]
    assert first == second


def test_write_keeps_modes(schema, defaults, tmp_path):
    spec, interface = next(apps(schema, defaults))
    render.write(render.render(spec, interface, defaults), tmp_path)
    assert render.is_executable(tmp_path / "template" / "script.sh.erb")
    assert not render.is_executable(tmp_path / "form.yml")
    assert render.read_manifest(tmp_path)["app"] == spec.name


def test_no_template_leaves_jinja_behind(schema, defaults):
    for spec, interface, files in rendered(schema, defaults):
        for path, f in files.items():
            if path.endswith(".png"):
                continue
            text = f.content.decode()
            assert "{{" not in text.replace("${{", ""), f"{path} has an unrendered {{{{"
            assert "{%" not in text, f"{path} has an unrendered {{%"
