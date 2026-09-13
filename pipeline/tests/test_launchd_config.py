from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path

from pipeline.main import cli


ROOT = Path(__file__).resolve().parents[2]
LAUNCHD = ROOT / "launchd"


def _program_arguments(name: str) -> list[str]:
    with (LAUNCHD / name).open("rb") as plist_file:
        return plistlib.load(plist_file)["ProgramArguments"]


def test_install_cleans_up_obsolete_weekly_service_on_upgrade(tmp_path: Path) -> None:
    home = tmp_path / "home"
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    legacy_plist = launch_agents / "com.signalcatcher.weekly.plist"
    legacy_plist.write_text("legacy")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    launchctl_log = tmp_path / "launchctl.log"
    fake_launchctl = fake_bin / "launchctl"
    fake_launchctl.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$LAUNCHCTL_LOG"\n'
    )
    fake_launchctl.chmod(0o755)

    env = os.environ.copy()
    env.update(
        HOME=str(home),
        LAUNCHCTL_LOG=str(launchctl_log),
        PATH=f"{fake_bin}:{env['PATH']}",
    )
    subprocess.run(
        ["/bin/bash", str(LAUNCHD / "install.sh"), "install"],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )

    assert not legacy_plist.exists()
    assert "bootout gui/" in launchctl_log.read_text()
    assert "/com.signalcatcher.weekly" in launchctl_log.read_text()
    assert not (LAUNCHD / "com.signalcatcher.weekly.plist").exists()


def test_daily_launch_agent_uses_existing_daily_cli_wrapper() -> None:
    shell, wrapper = _program_arguments("com.signalcatcher.daily.plist")
    wrapper_path = Path(wrapper)

    assert shell == "/bin/bash"
    assert wrapper_path.is_file()
    assert '"$VENV" -m pipeline daily' in wrapper_path.read_text()
    assert "daily" in cli.commands


def test_event_launch_agent_uses_existing_event_cli_command() -> None:
    python, module_flag, module, command = _program_arguments(
        "com.signalcatcher.event.plist"
    )

    assert Path(python).is_file()
    assert (module_flag, module, command) == ("-m", "pipeline", "event")
    assert command in cli.commands
