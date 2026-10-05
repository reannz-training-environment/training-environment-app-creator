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
        if gpu["enabled"] and gpu["mode"] == "all" and gpu["session_form"]:
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


def gpu_spec(schema, defaults, **gpu):
    from te_app_creator.spec import check

    raw = {
        "schema_version": 1,
        "name": "gpus",
        "title": "GPUs",
        "interfaces": ["jupyter", "rstudio", "codeserver"],
        "features": {"gpu": {"enabled": True, **gpu}},
    }
    return check(raw, "apps/gpus.yml", schema, defaults)


@pytest.mark.parametrize("session_form", [True, False])
def test_choose_mode_offers_one_gpu_from_a_menu(schema, defaults, session_form):
    spec = gpu_spec(schema, defaults, mode="choose", cards=["l4", "h100"], vram="1GiB", session_form=session_form)
    for interface in spec.app["interfaces"]:
        files = {f.path: f for f in render.render(spec, interface, defaults)}
        form = yaml.safe_load(files["form.yml"].content)
        assert "gpu_card" in form["form"]
        assert not [a for a in form["form"] if a.startswith("vram_")]
        menu = form["attributes"]["gpu_card"]
        assert menu["widget"] == "select"
        assert [option[1] for option in menu["options"]] == ["l4", "h100"]
        assert menu["value"] == "l4"
        assert ("gpu_vram" in form["form"]) == session_form
        if session_form:
            assert form["attributes"]["gpu_vram"]["value"] == "1GiB"

        submit = files["submit.yml.erb"].content.decode()
        assert "cards = %w[l4 h100]" in submit
        assert 'fleet = "#{card}:#{vram}"' in submit
        assert 'GPUEMU_FLEET: "<%= fleet %>"' in submit
        assert ("gpu_vram.to_s" in submit) == session_form
        # without the form, a session gets the first card on offer
        dockerfile = files["docker/Dockerfile"].content.decode()
        assert "ENV GPUEMU_FLEET=l4:1GiB\n" in dockerfile
        assert "an EMULATED NVIDIA GPU" in files["docker/scripts/gpu-banner.sh"].content.decode()


def test_all_mode_without_the_form_fixes_the_fleet(schema, defaults):
    spec = gpu_spec(schema, defaults, mode="all", cards=["l4", "a100"], session_form=False)
    files = {f.path: f for f in render.render(spec, "jupyter", defaults)}
    form = yaml.safe_load(files["form.yml"].content)
    assert form["form"] == ["cpu", "memory", "wall_time"]
    assert "GPUEMU_FLEET" not in files["submit.yml.erb"].content.decode()
    assert "ENV GPUEMU_FLEET=l4:200MiB,a100:200MiB\n" in files["docker/Dockerfile"].content.decode()


def test_choose_mode_with_one_card_warns(schema, defaults):
    spec = gpu_spec(schema, defaults, mode="choose", cards=["l4"])
    assert any("only one card" in w for w in spec.warnings)


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
        # NeSI's seff and svisit come with every Slurm, GPU or not
        assert ("opt-nesi-bin" in dockerfile) == features["slurm"]["enabled"]
        assert ("docker/scripts/slurm-nesi-tools.py" in files) == features["slurm"]["enabled"]
        assert ("micromamba create -y -p /opt/conda/envs/apps" in dockerfile) == bool(spec.app["software"]["conda"]["packages"])
        assert ("micromamba create -y -p /opt/python" in dockerfile) == bool(spec.app["advanced"]["python_version"])
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


def files_of(schema, defaults, interface, **raw):
    from te_app_creator.spec import check

    spec = check({"schema_version": 1, "name": "versions", "title": "Versions", **raw}, "t.yml", schema, defaults)
    return {f.path: f.content.decode() for f in render.render(spec, interface, defaults) if not f.path.endswith(".png")}


@pytest.mark.parametrize("interface", ["jupyter", "codeserver"])
def test_a_chosen_python_comes_first_on_path(schema, defaults, interface):
    files = files_of(
        schema, defaults, interface, interfaces=[interface],
        software={"pip": ["numpy"], "conda": {"packages": ["samtools"]}}, advanced={"python_version": "3.11.6"},
    )
    dockerfile = files["docker/Dockerfile"]
    assert '-c conda-forge "python=3.11.6" pip' in dockerfile
    assert 'ENV PATH="/opt/python/bin:${PATH}"' in dockerfile
    assert "RUN /opt/python/bin/pip --no-cache-dir install -r /opt/app-creator/requirements.txt" in dockerfile
    # one micromamba, for Python and the conda packages
    assert dockerfile.count("micromamba-linux-64") == 1
    assert dockerfile.index('"python=3.11.6"') < dockerfile.index("micromamba create -y -p /opt/conda/envs/apps")
    assert '= "3.11.6" ]' in files["docker/scripts/smoke-test.sh"]
    assert "| Python | 3.11.6, from conda-forge |" in files["README.md"]


def test_without_a_chosen_python_the_image_keeps_its_own(schema, defaults):
    files = files_of(schema, defaults, "jupyter", interfaces=["jupyter"], software={"pip": ["numpy"]})
    assert "/opt/python" not in files["docker/Dockerfile"]
    assert "RUN pip3 --no-cache-dir install jupyterlab" in files["docker/Dockerfile"]
    assert "| Python | 3.10, Ubuntu 22.04's own |" in files["README.md"]


def test_rstudio_makes_its_virtual_environment_from_the_chosen_python(schema, defaults):
    dockerfile = files_of(schema, defaults, "rstudio", interfaces=["rstudio"], advanced={"python_version": "3.11.6"})["docker/Dockerfile"]
    assert "RUN /opt/python/bin/python3 -m venv /opt/venv" in dockerfile
    assert 'ENV PATH="/opt/python/bin' not in dockerfile
    default = files_of(schema, defaults, "rstudio", interfaces=["rstudio"])
    assert "RUN python3 -m venv /opt/venv" in default["docker/Dockerfile"]
    assert f"FROM rocker/rstudio:{defaults['pins']['r_version']}\n" in default["docker/Dockerfile"]


def test_r_3_comes_from_posit_on_the_r_4_0_1_image(schema, defaults):
    files = files_of(schema, defaults, "rstudio", interfaces=["rstudio"], advanced={"r_version": "3.6.2"})
    dockerfile = files["docker/Dockerfile"]
    assert "FROM rocker/rstudio:4.0.1\n" in dockerfile
    assert "https://cdn.posit.co/r/ubuntu-2004/pkgs/r-${R_VERSION}_1_amd64.deb" in dockerfile
    assert "ARG R_VERSION=3.6.2" in dockerfile and "ENV R_HOME=/opt/R/${R_VERSION}/lib/R" in dockerfile
    assert "https://packagemanager.posit.co/cran/2020-02-28" in dockerfile
    assert '[ "${r_version}" = "3.6.2" ]' in files["docker/scripts/smoke-test.sh"]
    assert "| R | 3.6.2, from Posit's builds of R, with packages from CRAN as it was on 2020-02-28 |" in files["README.md"]
    assert "Python | 3.8, Ubuntu 20.04's own" in files["README.md"]


def test_r_4_comes_with_its_rocker_image(schema, defaults):
    files = files_of(schema, defaults, "rstudio", interfaces=["rstudio"], advanced={"r_version": "4.3.2", "rstudio_image": "rocker/verse"})
    assert "FROM rocker/verse:4.3.2\n" in files["docker/Dockerfile"]
    assert "cdn.posit.co" not in files["docker/Dockerfile"]
    assert "| R | 4.3.2, from `rocker/verse:4.3.2` |" in files["README.md"]


def test_gpus_on_ubuntu_20_04_get_cmake_from_pip(schema, defaults):
    gpu = {"features": {"gpu": {"enabled": True}}, "advanced": {"python_version": "3.11.6"}}
    old = files_of(schema, defaults, "rstudio", interfaces=["rstudio"], **{**gpu, "advanced": {**gpu["advanced"], "r_version": "4.1.0"}})
    assert "/opt/venv/bin/pip --no-cache-dir install 'cmake>=3.18'" in old["docker/Dockerfile"]
    assert "apt-get purge -y cmake" not in old["docker/Dockerfile"]
    new = files_of(schema, defaults, "jupyter", interfaces=["jupyter"], **gpu)
    assert "        cmake \\\n" in new["docker/Dockerfile"] and "install 'cmake" not in new["docker/Dockerfile"]


def test_rserver_gets_only_the_options_its_version_has(schema, defaults):
    script = files_of(schema, defaults, "rstudio", interfaces=["rstudio"])["template/script.sh.erb"]
    assert '"--database-config-file=/etc/rstudio/database/database.conf"' in script
    assert 'rserver_options+=("${option}")' in script
    assert script.rstrip().endswith('"${rserver_options[@]}"')
