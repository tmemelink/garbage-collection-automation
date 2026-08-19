"""The container the job is meant to live in: what `pct create` is asked for.

`create-container.sh` runs on a Proxmox host, which this test host is not. Its
--dry-run prints the exact command instead of running it, so every setting the
recommendation is made of can be checked here without a hypervisor.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from .conftest import REPO_ROOT

CREATE = REPO_ROOT / "packaging" / "lxc" / "create-container.sh"
README = REPO_ROOT / "README.md"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def create(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(CREATE), "--dry-run", *args], capture_output=True, text=True, cwd=REPO_ROOT
    )


def pct_create_line(*args) -> str:
    """The single `pct create ...` command a run would have executed."""
    result = create(*args)
    assert result.returncode == 0, result.stderr
    line = next((text for text in result.stdout.splitlines() if "pct create" in text), None)
    assert line is not None, f"--dry-run printed no pct create command:\n{result.stdout}"
    return line


def test_the_defaults_are_the_recommendation():
    """Nobody should have to remember these numbers; running it plain is the advice."""
    line = pct_create_line("--vmid", "120")

    assert "--unprivileged 1" in line
    assert "--cores 1" in line
    assert "--memory 256" in line
    assert "--swap 256" in line
    assert "--rootfs local-lvm:2" in line
    assert "--ostype debian" in line
    assert "--onboot 1" in line
    assert "ip=dhcp" in line


def test_nothing_the_job_does_not_need_is_switched_on():
    """nesting, fuse and mounts all widen an unprivileged container; the job wants none."""
    line = pct_create_line("--vmid", "120")

    assert "--features" not in line
    assert "nesting" not in line
    assert "--mp0" not in line


def test_a_debian_12_template_is_what_it_asks_for():
    line = pct_create_line("--vmid", "120")

    assert "debian-12-standard" in line
    assert re.search(r"pct create 120 \S*vztmpl", line), line


def test_every_default_can_be_overridden():
    line = pct_create_line(
        "--vmid",
        "121",
        "--hostname",
        "bakje",
        "--cores",
        "2",
        "--memory",
        "512",
        "--swap",
        "0",
        "--disk",
        "8",
        "--storage",
        "tank",
        "--bridge",
        "vmbr1",
    )

    assert "pct create 121 " in line
    assert "--hostname bakje" in line
    assert "--cores 2" in line
    assert "--memory 512" in line
    assert "--swap 0" in line
    assert "--rootfs tank:8" in line
    assert "bridge=vmbr1" in line


def test_an_ssh_key_is_only_passed_when_there_is_one(tmp_path):
    assert "--ssh-public-keys" not in pct_create_line("--vmid", "120")

    key = tmp_path / "id_ed25519.pub"
    key.write_text("ssh-ed25519 AAAA test\n")

    assert f"--ssh-public-keys {key}" in pct_create_line("--vmid", "120", "--ssh-key", str(key))


def test_a_missing_ssh_key_is_caught_before_the_container_exists():
    result = create("--vmid", "120", "--ssh-key", "/does/not/exist.pub")

    assert result.returncode != 0
    assert "ssh key" in result.stderr.lower()


def test_install_is_shown_in_the_dry_run_too():
    """--install is the second half of the promise, so --dry-run has to show it."""
    result = create("--vmid", "120", "--install")

    assert result.returncode == 0, result.stderr
    assert "pct exec 120" in result.stdout
    assert "install.sh" in result.stdout


def test_it_refuses_to_run_anywhere_that_is_not_a_proxmox_host():
    """Without --dry-run there is no half-way: no pct, no run."""
    result = subprocess.run(
        [str(CREATE), "--vmid", "120"], capture_output=True, text=True, cwd=REPO_ROOT
    )

    assert result.returncode != 0
    assert "pct" in result.stderr
    assert "Proxmox" in result.stderr


def test_it_asks_for_a_vmid_rather_than_guessing_one():
    result = subprocess.run(
        [str(CREATE), "--dry-run", "--vmid"], capture_output=True, text=True, cwd=REPO_ROOT
    )

    assert result.returncode != 0
    assert "--vmid" in result.stderr


# --- the README is where the recommendation is read ----------------------------------


#: The header of the one table in the README that describes the container.
CONTAINER_TABLE = ["setting", "value", "why"]


def readme_recommendation() -> dict[str, str]:
    """The `| setting | value |` rows of the README's container table.

    Anchored on the table's own header row rather than the heading above it: the
    prose around the numbers is the README's to reword, the numbers are not.
    """
    rows = {}
    in_table = False
    for line in README.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not in_table:
            in_table = [cell.lower() for cell in cells[:3]] == CONTAINER_TABLE
            continue
        if len(cells) < 2:  # the first line that is not a table row ends it
            break
        if not cells[0].startswith("---"):
            rows[cells[0].lower()] = cells[1]
    return rows


def documented_number(table: dict[str, str], setting: str) -> str:
    """The number the README gives for *setting*, whichever row happens to name it.

    One row may cover two settings - "Memory / Swap", "256 MiB each" - which is
    the README explaining itself, not two numbers to keep in step.
    """
    for name, value in table.items():
        if setting in name:
            number = re.search(r"\d+", value)
            assert number, f"no number in the README's {name} row"
            return number.group()
    raise AssertionError(f"the README's container table says nothing about {setting}")


def test_the_readme_and_the_script_agree_about_the_recommendation():
    """Two places to change a number is one too many; this catches the drift."""
    table = readme_recommendation()
    line = pct_create_line("--vmid", "120")

    assert table, "the README has no | Setting | Value | Why | table for the container"
    for setting, flag in (("cores", "--cores"), ("memory", "--memory"), ("swap", "--swap")):
        documented = documented_number(table, setting)
        assert f"{flag} {documented}" in line, f"README and script disagree on {setting}"

    disk = documented_number(table, "disk")
    assert f":{disk}" in line, "README and script disagree on the disk size"


def test_a_contradiction_is_caught_before_the_container_is_created():
    """--install cannot reach a stopped container; finding that out afterwards is too late."""
    result = create("--vmid", "120", "--no-start", "--install")

    assert result.returncode != 0
    assert "--no-start" in result.stderr
    assert "pct create" not in result.stdout, "it planned a container it could not have finished"
