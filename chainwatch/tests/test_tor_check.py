"""bin/dmd-tor-check: Tor's answers from a pretend control socket, and pretend systemctl, apt-get,
dpkg-query and node, to see what it repairs and what it posts."""

from __future__ import annotations

import http.server
import json
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "bin" / "dmd-tor-check"


class FakeTor:
    """A Tor control socket answering AUTHENTICATE with the cookie, and GETINFO from `info`."""

    def __init__(self, path: Path, cookie: Path) -> None:
        self.info = {
            "version": "0.4.9.11",
            "status/version/current": "recommended",
            "status/bootstrap-phase": 'NOTICE BOOTSTRAP PROGRESS=100 TAG=done SUMMARY="Done"',
            "network-liveness": "up",
        }
        cookie.write_bytes(b"\x01" * 32)
        self.cookie = (b"\x01" * 32).hex()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen(5)
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn, conn.makefile("rwb") as stream:
                for raw in stream:
                    line = raw.decode().strip()
                    if line == f"AUTHENTICATE {self.cookie}":
                        stream.write(b"250 OK\r\n")
                    elif line.startswith("AUTHENTICATE"):
                        stream.write(b"515 Authentication failed\r\n")
                    elif line.startswith("GETINFO "):
                        key = line.split(" ", 1)[1]
                        if key in self.info:
                            stream.write(f"250-{key}={self.info[key]}\r\n250 OK\r\n".encode())
                        else:
                            stream.write(f'552 Unrecognized key "{key}"\r\n'.encode())
                    elif line == "QUIT":
                        stream.write(b"250 closing connection\r\n")
                        stream.flush()
                        break
                    stream.flush()

    def close(self) -> None:
        self.server.close()


class Hook(http.server.BaseHTTPRequestHandler):
    posts: ClassVar[list[dict]] = []

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        Hook.posts.append(json.loads(self.rfile.read(length)))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def world(tmp_path: Path) -> Iterator[dict]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.log"
    (bin_dir / "systemctl").write_text(
        '#!/bin/sh\necho "systemctl $*" >>"$TT/calls.log"\n'
        'if [ "$1" = is-active ]; then [ -f "$TT/stopped" ] && exit 3; exit 0; fi\n'
        'if [ "$1" = restart ] && [ -f "$TT/start-fixes" ]; then rm -f "$TT/stopped"; fi\nexit 0\n'
    )
    # apt-get "upgrades" Tor when a newer version is waiting
    (bin_dir / "apt-get").write_text(
        '#!/bin/sh\necho "apt-get $*" >>"$TT/calls.log"\n'
        'case "$*" in *only-upgrade*)\n'
        '  [ -f "$TT/newer" ] && cp "$TT/newer" "$TT/version";;\nesac\nexit 0\n'
    )
    (bin_dir / "dpkg-query").write_text('#!/bin/sh\ncat "$TT/version"\n')
    (bin_dir / "apt-config").write_text(
        "#!/bin/sh\necho 'Unattended-Upgrade::Origins-Pattern:: \"origin=TorProject\";'\n"
    )
    (bin_dir / "dmd-cli").write_text(
        '#!/bin/sh\nprintf \'{"networks": [{"name": "ipv4", "proxy": "%s"}]}\' '
        '"$(cat "$TT/proxy")"\n'
    )
    for tool in bin_dir.iterdir():
        tool.chmod(0o755)
    (tmp_path / "version").write_text("0.4.9.11-1~noble+1")
    (tmp_path / "proxy").write_text("127.0.0.1:9050")

    tor = FakeTor(tmp_path / "control", tmp_path / "cookie")
    Hook.posts = []
    hook = http.server.HTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=hook.serve_forever, daemon=True).start()
    env = dict(
        os.environ,
        TT=str(tmp_path),
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        DMD_TOR_CONTROL=str(tmp_path / "control"),
        DMD_TOR_COOKIE=str(tmp_path / "cookie"),
        DMD_TOR_STATE=str(tmp_path / "state.json"),
        DMD_NODE_CLI=str(bin_dir / "dmd-cli"),
        DMD_MONITOR_ENV=str(tmp_path / "monitor.env"),
        DMD_TOR_WEBHOOK=f"http://127.0.0.1:{hook.server_address[1]}/hook",
        DMD_SERVER_NAME="the test server",
        DMD_TOR_WAIT="0",
    )

    def check(*args: str) -> str:
        done = subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert done.returncode == 0, done.stderr
        return done.stdout

    yield {
        "dir": tmp_path,
        "tor": tor,
        "check": check,
        "calls": calls,
        "posts": Hook.posts,
        "env": env,
    }
    hook.shutdown()
    tor.close()


def calls(world: dict) -> str:
    return world["calls"].read_text() if world["calls"].exists() else ""


def test_all_well_changes_nothing_and_posts_nothing(world: dict) -> None:
    out = world["check"]()
    assert "Tor 0.4.9.11 (recommended), connected; the node goes through it." in out
    assert "restart" not in calls(world) and "apt-get" not in calls(world)
    assert world["posts"] == []
    assert "Updates:     every day, from the Tor Project" in world["check"]("--status")


def test_a_version_too_old_is_upgraded_at_once_and_tor_restarted(world: dict) -> None:
    world["tor"].info["status/version/current"] = "obsolete"
    (world["dir"] / "newer").write_text("0.4.9.12-1~noble+1")
    out = world["check"]()
    assert "upgraded it from 0.4.9.11-1~noble+1 to 0.4.9.12-1~noble+1" in out
    assert "apt-get -o DPkg::Lock::Timeout=300 install -y -q --only-upgrade" in calls(world)
    assert "systemctl restart tor" in calls(world)
    [posted] = world["posts"]
    assert posted["embeds"][0]["title"] == "Tor on the test server: repaired"


def test_a_version_too_old_with_nothing_newer_is_a_problem_posted_once(world: dict) -> None:
    world["tor"].info["status/version/current"] = "unrecommended"
    world["check"]()
    world["check"]()  # the same problem: not posted again before 6 hours
    [posted] = world["posts"]
    assert posted["embeds"][0]["title"] == "[WARNING] Tor on the test server"
    assert (
        "unrecommended for the Tor network, and no newer version could be installed"
        in posted["embeds"][0]["description"]
    )
    world["tor"].info["status/version/current"] = "recommended"
    world["check"]()
    assert (
        world["posts"][-1]["embeds"][0]["title"] == "Resolved: Tor on the test server works again"
    )


def test_cut_off_twice_restarts_tor_and_alerts(world: dict) -> None:
    world["tor"].info["network-liveness"] = "down"
    out = world["check"]()
    assert "not connected yet" in out and world["posts"] == []  # the first time it may be starting
    world["check"]()
    assert calls(world).count("systemctl restart tor") == 1
    assert "cut off from the Tor network" in world["posts"][0]["embeds"][0]["description"]
    world["check"]()  # within the hour: not restarted again
    assert calls(world).count("systemctl restart tor") == 1


def test_tor_stopped_is_started_again(world: dict) -> None:
    (world["dir"] / "stopped").touch()
    (world["dir"] / "start-fixes").touch()
    out = world["check"]()
    assert "Tor was not running: started it again." in out
    assert world["posts"][0]["embeds"][0]["title"] == "Tor on the test server: repaired"


def test_tor_that_does_not_start_is_a_problem(world: dict) -> None:
    (world["dir"] / "stopped").touch()
    world["check"]()
    assert "Tor does not start" in world["posts"][0]["embeds"][0]["description"]


def test_a_node_outside_tor_is_a_problem(world: dict) -> None:
    (world["dir"] / "proxy").write_text("")
    world["check"]()
    text = world["posts"][0]["embeds"][0]["description"]
    assert "does not go through Tor (its proxy: none)" in text
    assert "Add proxy=127.0.0.1:9050" in text
    assert "does NOT go through Tor" in world["check"]("--status")


def test_the_monitoring_servers_webhook_is_used_when_none_is_set(world: dict) -> None:
    hook = world["env"].pop("DMD_TOR_WEBHOOK")
    (world["dir"] / "monitor.env").write_text(f"DMDW_SMTP_HOST=x\nDMDW_DISCORD_WEBHOOK={hook}\n")
    (world["dir"] / "proxy").write_text("")
    world["check"]()
    assert len(world["posts"]) == 1


def test_without_any_webhook_problems_only_go_to_the_log(world: dict) -> None:
    world["env"].pop("DMD_TOR_WEBHOOK")
    (world["dir"] / "proxy").write_text("")
    out = world["check"]()
    assert "Problem: The DebitMyData node does not go through Tor" in out
    assert world["posts"] == []
