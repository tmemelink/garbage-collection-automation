"""The install bundle: what it must carry, and what must never leave the workstation."""

from __future__ import annotations

import shutil
import subprocess
import tarfile

import pytest

from .conftest import REPO_ROOT

BUILD = REPO_ROOT / "build.sh"

pytestmark = pytest.mark.skipif(shutil.which("tar") is None, reason="needs tar")


@pytest.fixture
def local_secrets():
    """A checkout that holds real secrets - the case the bundle has to survive.

    Whatever the developer already had is left exactly as it was; only files this
    test created are cleaned up again.
    """
    created = []
    for name, text in (("config.toml", "# real config\n"), ("env", "GCA_TODOIST_TOKEN=s3cret\n")):
        path = REPO_ROOT / "config" / name
        if not path.exists():
            path.write_text(text)
            created.append(path)
    yield
    for path in created:
        path.unlink(missing_ok=True)


@pytest.fixture
def bundle(local_secrets):
    """Build the real bundle and hand back the names inside it."""
    result = subprocess.run([str(BUILD), "lxc"], capture_output=True, text=True, cwd=REPO_ROOT)
    assert result.returncode == 0, result.stderr

    tarball = next(iter(sorted((REPO_ROOT / "dist").glob("*-lxc.tar.gz"))), None)
    assert tarball is not None, "the build reported success but produced no tarball"
    with tarfile.open(tarball) as archive:
        # Drop the leading <name>/ so the assertions read like the repository.
        return {name.split("/", 1)[1] for name in archive.getnames() if "/" in name}


def test_the_bundle_carries_nothing_that_could_hold_a_token(bundle):
    """The README promises a local config never travels; so must the env file."""
    assert "config/config.toml" not in bundle
    assert "config/env" not in bundle
    assert "config/config.example.toml" in bundle


def test_the_bundle_carries_the_lockfile_it_promises(bundle):
    """Without it the container re-resolves, and the air-gap guarantee is a fiction."""
    assert "uv.lock" in bundle


def test_the_bundle_carries_what_the_installer_reads(bundle):
    for needed in (
        "install.sh",
        "pyproject.toml",
        ".python-version",
        "src/run-job.sh",
        "src/garbage_collection_automation/__init__.py",
        "scheduling/garbage-collection-automation.cron",
        "scheduling/garbage-collection-automation-web.service",
    ):
        assert needed in bundle, f"the installer reads {needed}, so the bundle must carry it"


def test_the_bundle_carries_the_page_but_not_the_design_behind_it(bundle):
    """The container serves ui/; the mockups are megabytes it has no use for."""
    assert "ui/index.html" in bundle
    assert "ui/static/app.css" in bundle
    assert not [name for name in bundle if name.startswith("ui/mockups")]


def test_the_bundle_carries_no_build_artefacts(bundle):
    assert not [name for name in bundle if "__pycache__" in name]


# --- the build wrapper's dispatch -----------------------------------------------------


def build(*args) -> subprocess.CompletedProcess:
    return subprocess.run([str(BUILD), *args], capture_output=True, text=True, cwd=REPO_ROOT)


def test_a_planned_target_is_reported_as_planned_not_as_unknown():
    """--list calls docker "not implemented yet"; asking for it must say the same."""
    result = build("docker")

    assert result.returncode == 1
    assert "not implemented yet" in result.stderr
    assert "unknown target" not in result.stderr


def test_a_target_this_project_does_not_have_is_unknown():
    result = build("solaris")

    assert result.returncode == 1
    assert "unknown target" in result.stderr
    assert "Available targets" in result.stderr


def test_the_listing_and_the_dispatch_agree_about_every_target():
    listed = build("--list")
    assert listed.returncode == 0

    for line in listed.stdout.splitlines()[1:]:
        target, _, rest = line.strip().partition(" ")
        planned = "not implemented yet" in rest
        attempted = build(target, "--help-that-no-target-takes")
        assert planned == ("not implemented yet" in attempted.stderr), (
            f"--list and ./build.sh disagree about {target}"
        )
