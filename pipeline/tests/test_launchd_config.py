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


def test_install_includes_weekly_service(tmp_path: Path) -> None:
    home = tmp_path / "home"
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)

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

    assert (launch_agents / "com.signalcatcher.superstar-weekly.plist").is_symlink()
    assert "/com.signalcatcher.superstar-weekly" in launchctl_log.read_text()


def test_daily_launch_agent_uses_keychain_daily_wrapper() -> None:
    shell, wrapper = _program_arguments("com.signalcatcher.daily.plist")
    wrapper_path = Path(wrapper)

    assert shell == "/bin/bash"
    assert wrapper_path.is_file()
    assert 'run-pipeline.sh" daily' in wrapper_path.read_text()
    assert "daily" in cli.commands


def test_event_launch_agent_uses_keychain_event_wrapper() -> None:
    shell, wrapper = _program_arguments("com.signalcatcher.event.plist")
    wrapper_path = Path(wrapper)

    assert shell == "/bin/bash"
    assert wrapper_path.is_file()
    assert 'run-pipeline.sh" event' in wrapper_path.read_text()
    assert "event" in cli.commands


def test_weekly_launch_agent_runs_sunday_at_0900_kst() -> None:
    plist_path = LAUNCHD / "com.signalcatcher.superstar-weekly.plist"
    with plist_path.open("rb") as plist_file:
        config = plistlib.load(plist_file)
    shell, wrapper = config["ProgramArguments"]

    assert config["StartCalendarInterval"] == {"Weekday": 0, "Hour": 9, "Minute": 0}
    assert shell == "/bin/bash"
    assert Path(wrapper).is_file()
    assert 'run-pipeline.sh" superstar-weekly' in Path(wrapper).read_text()
    assert "superstar-weekly" in cli.commands
