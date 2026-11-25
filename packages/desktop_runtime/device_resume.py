"""Native-owned Continue delivery; no page/model URL, credential or task replay."""
import logging
import secrets
import threading

log = logging.getLogger(__name__)


def _pause_marker(worker):
    steps = worker.last.get("steps") or []
    return next((step for step in reversed(steps) if isinstance(step, dict) and step.get("kind") == "pause"), None)


class ResumeMonitor:
    def __init__(self, worker):
        self.worker = worker
        self.marker = None
        self.token = None
        self.request_id = None
        self.accepted = False
        self.cleared = False

    def tick(self):
        worker, gateway = self.worker, self.worker.gateway
        cancellable = getattr(gateway, "cancellable_request", None)
        def request(method, path, body):
            if callable(cancellable):
                return cancellable(method, path, body, stop=worker.cancelled, deadline=worker.deadline)
            return gateway.request(method, path, body)
        def current():
            if callable(cancellable):
                return gateway.current(request=request)
            return gateway.current()
        if worker.cancelled.is_set() or getattr(worker, "closed", False):
            return
        marker = _pause_marker(worker)
        if worker.last.get("state") != "needs_you":
            if self.token and self.accepted and not self.cleared and worker.last.get("resumed_after") == "needs_you":
                current()
                result = request("POST", gateway.path + "/human-pause/clear", {"pause_token": self.token})
                if not isinstance(result, dict) or result.get("state") != "pause_loop_exited":
                    raise ValueError("native_resume_exit_unconfirmed")
                self.cleared = True
            return
        if marker is None:
            return  # progress was posted before the authoritative local pause receipt
        if marker is not self.marker:
            self.marker, self.token = marker, secrets.token_hex(16)
            self.request_id, self.accepted, self.cleared = None, False, False
        if self.cleared:
            return  # a new pause must have a new local receipt, not an old event
        current()
        if self.request_id is None:
            result = request("POST", gateway.path + "/human-pause", {
                "pause_token": self.token, "pause_kind": worker.last.get("pause_kind") or marker.get("pause_kind", "")})
            if (not isinstance(result, dict) or result.get("pause_token") != self.token
                    or not isinstance(result.get("request_id"), str) or not result["request_id"].startswith("nyr_")):
                raise ValueError("native_resume_registration_unconfirmed")
            self.request_id = result["request_id"]
        result = request("GET", gateway.path + "/human-resume", None)
        signal = result.get("signal") if isinstance(result, dict) else None
        if signal is None:
            return
        expected = {"request_id": self.request_id, "pause_token": self.token,
            "execution_id": gateway.execution_id, "review_digest": gateway.review_digest,
            "device_id": gateway.device_id}
        if signal != expected:
            raise PermissionError("native_resume_identity_changed")
        current()
        # Recheck local episode after every network boundary. A cleared or replaced
        # pause must never receive an old signal, even in the same execution.
        if (worker.last.get("state") != "needs_you" or _pause_marker(worker) is not self.marker
                or worker.cancelled.is_set() or getattr(worker, "closed", False)):
            return
        if not self.accepted:
            if not worker.continue_founder_handoff():
                return
            self.accepted = True  # lost ACK retries cannot set the Event twice
        result = request("POST", gateway.path + "/human-resume/ack", {
            "pause_token": self.token, "request_id": self.request_id, "execution_id": gateway.execution_id})
        if not isinstance(result, dict) or result.get("state") != "signal_accepted" or result.get("request_id") != self.request_id:
            raise ValueError("native_resume_ack_unconfirmed")


def start_resume_monitor(worker, worker_thread):
    """One bounded monitor for the already running worker; never restarts an order."""
    monitor = ResumeMonitor(worker)
    def watch():
        while worker_thread.is_alive() and not worker.cancelled.wait(2.0):
            try:
                monitor.tick()
            except (ValueError, PermissionError, RuntimeError, OSError):
                log.info("native_resume_signal_unavailable")
    thread = threading.Thread(target=watch, name="supymem-owned-resume", daemon=True)
    thread.start()
    return monitor
