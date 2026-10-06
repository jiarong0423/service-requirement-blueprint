from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request

import pytest


def _unused_loopback_port() -> int:
    probe = socket.socket()
    try:
        try:
            probe.bind(("127.0.0.1", 0))
        except PermissionError:
            pytest.skip("loopback binding is unavailable in the current sandbox")
        return int(probe.getsockname()[1])
    finally:
        probe.close()


def test_loopback_server_subprocess_is_bounded_and_released(demo_runtime, project_root) -> None:
    _app, instance_root, _passwords = demo_runtime
    port = _unused_loopback_port()
    environment = {
        "POC_INSTANCE_PATH": str(instance_root),
        "POC_LOCAL_HTTP": "1",
        "POC_PORT": str(port),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "app"],
        cwd=project_root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        response_status = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/en/", timeout=0.5) as response:
                    response_status = response.status
                    break
            except OSError:
                time.sleep(0.1)
        assert process.poll() is None
        assert response_status == 200
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
    assert process.returncode is not None
    release_probe = socket.socket()
    try:
        release_probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        release_probe.bind(("127.0.0.1", port))
    finally:
        release_probe.close()


def test_local_port_rejects_invalid_values(monkeypatch) -> None:
    from app.__main__ import local_port

    for value in ("0", "80", "65536", "not-a-port", "5000 "):
        monkeypatch.setenv("POC_PORT", value)
        try:
            local_port()
        except RuntimeError as error:
            assert str(error) == "local_port_invalid"
        else:
            raise AssertionError("invalid port was accepted")
