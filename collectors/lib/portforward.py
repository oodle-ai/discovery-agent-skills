"""kubectl port-forward as context manager with process-group cleanup."""

import atexit
import os
import re
import signal
import subprocess
import time


class PortForward:
    """Context manager wrapping kubectl port-forward.

    Usage::

        with PortForward("svc/prometheus", 9090, namespace="monitoring") as url:
            requests.get(f"{url}/api/v1/query")
    """

    _READY_RE = re.compile(
        r"Forwarding from (?:127\.0\.0\.1|\[::1\]|localhost):(\d+) ->"
    )

    def __init__(
        self,
        resource,
        port,
        *,
        namespace=None,
        context=None,
        local_port=0,
        readiness_timeout=15,
    ):
        self.resource = resource
        self.port = port
        self.namespace = namespace
        self.context = context
        self.local_port = local_port
        self.readiness_timeout = readiness_timeout
        self._process = None
        self._assigned_port = None
        self._prev_sigterm = None
        self._prev_sigint = None

    @property
    def endpoint(self):
        if self._assigned_port is None:
            raise RuntimeError("PortForward not started")
        return f"http://127.0.0.1:{self._assigned_port}"

    def __enter__(self):
        cmd = ["kubectl", "port-forward"]
        if self.namespace:
            cmd.extend(["-n", self.namespace])
        if self.context:
            cmd.extend(["--context", self.context])
        cmd.append(self.resource)
        cmd.append(f"{self.local_port}:{self.port}")

        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid,
        )

        atexit.register(self._cleanup)
        self._prev_sigterm = signal.getsignal(signal.SIGTERM)
        self._prev_sigint = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

        self._assigned_port = self._wait_for_ready()
        return self.endpoint

    def __exit__(self, *exc):
        self._cleanup()
        if self._prev_sigterm is not None:
            signal.signal(signal.SIGTERM, self._prev_sigterm)
        if self._prev_sigint is not None:
            signal.signal(signal.SIGINT, self._prev_sigint)
        atexit.unregister(self._cleanup)
        return False

    def _wait_for_ready(self):
        deadline = time.monotonic() + self.readiness_timeout
        while time.monotonic() < deadline:
            rc = self._process.poll()
            if rc is not None:
                stderr = self._process.stderr.read().decode(errors="replace")
                raise RuntimeError(f"kubectl port-forward exited early: {stderr}")
            line = self._process.stdout.readline().decode(errors="replace")
            m = self._READY_RE.search(line)
            if m:
                return int(m.group(1))
        self._cleanup()
        raise TimeoutError(
            f"port-forward not ready within {self.readiness_timeout}s"
        )

    def _cleanup(self):
        if self._process is not None and self._process.poll() is None:
            try:
                os.killpg(os.getpgid(self._process.pid), signal.SIGTERM)
            except OSError:
                pass
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
                except OSError:
                    pass
        self._process = None

    def _signal_handler(self, signum, _frame):
        self._cleanup()
        raise SystemExit(128 + signum)
