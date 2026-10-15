import threading
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from agentic_ops.watcher import PodWatcher
from agentic_ops.web import create_app
from test_aks import pod


def test_healthz():
    with TestClient(create_app()) as http:
        assert http.get("/healthz").json() == {"status": "ok"}


def test_incident_submission_route_is_absent():
    with TestClient(create_app()) as http:
        assert http.post("/api/v1/incidents", json={}).status_code == 404


def test_lifespan_starts_the_watcher_and_signals_it_to_stop():
    started, stopped = threading.Event(), threading.Event()

    class FakeWatcher:
        def run(self, stop):
            started.set()
            stop.wait(5)
            stopped.set()

    with TestClient(create_app(watcher=FakeWatcher())):
        assert started.wait(5)
        assert not stopped.is_set()
    assert stopped.wait(5)


def test_watcher_raises_incident_for_pull_failures_only():
    apps = MagicMock()
    parent = MagicMock(kind="Deployment")
    parent.name = "test-app"
    apps.read_namespaced_replica_set.return_value = MagicMock(metadata=MagicMock(owner_references=[parent]))
    handled = []
    watcher = PodWatcher(MagicMock(), apps, "test-app", handled.append)
    assert watcher.process_pod(pod()) is True
    assert handled[0].workload == "test-app" and handled[0].namespace == "test-app"
    assert watcher.process_pod(pod(reason="CrashLoopBackOff")) is False
    assert len(handled) == 1

