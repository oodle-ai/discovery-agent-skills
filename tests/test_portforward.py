"""Unit tests for lib/portforward.py — kubectl port-forward context manager."""

import signal
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collectors"))
from lib.portforward import PortForward  # noqa: E402


class TestPortForward:
    def _mock_process(self, local_port=54321, remote_port=9090):
        proc = MagicMock()
        proc.stdout.readline.return_value = (
            f"Forwarding from 127.0.0.1:{local_port} -> {remote_port}\n".encode()
        )
        proc.poll.return_value = None
        proc.pid = 99999
        return proc

    @patch("os.killpg")
    @patch("os.getpgid", return_value=99999)
    @patch("subprocess.Popen")
    def test_basic_open_and_cleanup(self, mock_popen, mock_getpgid, mock_killpg):
        mock_popen.return_value = self._mock_process()

        with PortForward("svc/prometheus", 9090) as endpoint:
            assert endpoint == "http://127.0.0.1:54321"

        cmd = mock_popen.call_args[0][0]
        assert cmd == ["kubectl", "port-forward", "svc/prometheus", "0:9090"]
        mock_killpg.assert_called_once_with(99999, signal.SIGTERM)

    @patch("os.killpg")
    @patch("os.getpgid", return_value=99999)
    @patch("subprocess.Popen")
    def test_namespace_and_context(self, mock_popen, mock_getpgid, mock_killpg):
        mock_popen.return_value = self._mock_process()

        with PortForward(
            "svc/prometheus", 9090, namespace="monitoring", context="prod"
        ) as endpoint:
            assert endpoint == "http://127.0.0.1:54321"

        cmd = mock_popen.call_args[0][0]
        assert cmd == [
            "kubectl", "port-forward",
            "-n", "monitoring",
            "--context", "prod",
            "svc/prometheus", "0:9090",
        ]

    @patch("os.killpg")
    @patch("os.getpgid", return_value=99999)
    @patch("subprocess.Popen")
    def test_explicit_local_port(self, mock_popen, mock_getpgid, mock_killpg):
        mock_popen.return_value = self._mock_process(local_port=8080)

        with PortForward("svc/prometheus", 9090, local_port=8080) as endpoint:
            assert endpoint == "http://127.0.0.1:8080"

        cmd = mock_popen.call_args[0][0]
        assert "8080:9090" in cmd

    @patch("subprocess.Popen")
    def test_process_exits_early_raises(self, mock_popen):
        proc = MagicMock()
        proc.poll.return_value = 1
        proc.stderr.read.return_value = b"error: no such service"
        proc.stdout.readline.return_value = b""
        mock_popen.return_value = proc

        with pytest.raises(RuntimeError, match="exited early"):
            PortForward("svc/missing", 9090).__enter__()

    @patch("subprocess.Popen")
    def test_timeout_raises(self, mock_popen):
        proc = MagicMock()
        proc.poll.return_value = None
        proc.stdout.readline.return_value = b""
        proc.pid = 99999
        mock_popen.return_value = proc

        with patch("os.killpg"), patch("os.getpgid", return_value=99999):
            with pytest.raises(TimeoutError, match="not ready"):
                PortForward("svc/prometheus", 9090, readiness_timeout=0.1).__enter__()

    @patch("os.killpg")
    @patch("os.getpgid", return_value=99999)
    @patch("subprocess.Popen")
    def test_endpoint_property_before_enter_raises(self, mock_popen, *_):
        pf = PortForward("svc/prometheus", 9090)
        with pytest.raises(RuntimeError, match="not started"):
            _ = pf.endpoint

    @patch("os.killpg")
    @patch("os.getpgid", return_value=99999)
    @patch("subprocess.Popen")
    def test_ipv6_forwarding_line(self, mock_popen, mock_getpgid, mock_killpg):
        proc = self._mock_process()
        proc.stdout.readline.return_value = (
            b"Forwarding from [::1]:54321 -> 9090\n"
        )
        mock_popen.return_value = proc

        with PortForward("svc/prometheus", 9090) as endpoint:
            assert endpoint == "http://127.0.0.1:54321"
