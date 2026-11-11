import os
import tempfile
from fastapi.testclient import TestClient

# Use an isolated DB for tests.
_tmp = tempfile.NamedTemporaryFile(delete=False)
_tmp.close()
os.environ["FREENETX_DB"] = _tmp.name
os.environ["FREENETX_NODE_TOKENS"] = "node-001:test-token,node-002:test-token-2"
os.environ["FREENETX_ONLINE_THRESHOLD"] = "90"

from server.app import app, init_db
init_db()
client = TestClient(app)
AUTH = {"Authorization": "Bearer test-token"}
AUTH2 = {"Authorization": "Bearer test-token-2"}


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_heartbeat_requires_token():
    payload = {"node_id": "node-001", "firmware": "2.0.0-mvp", "rssi": -55}
    assert client.post("/v1/heartbeat", json=payload).status_code == 401


def test_heartbeat_registers_allowlisted_node():
    payload = {"node_id": "node-001", "firmware": "2.0.0-mvp", "rssi": -55}
    r = client.post("/v1/heartbeat", json=payload, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["accepted"] is True
    fleet = client.get("/v1/fleet").json()["nodes"]
    assert any(n["node_id"] == "node-001" and n["status"] == "online" for n in fleet)


def test_event_accepts_only_declared_source_types():
    good = {
        "node_id": "node-001", "source_type": "owned",
        "source_ref": "lab-router-01", "event_type": "signal_sample",
        "payload": {"rssi": -55}
    }
    assert client.post("/v1/events", json=good, headers=AUTH).status_code == 200
    bad = dict(good)
    bad["source_type"] = "unknown"
    assert client.post("/v1/events", json=bad, headers=AUTH).status_code == 422


def test_unknown_node_rejected():
    payload = {"node_id": "node-999", "firmware": "2.0.0-mvp"}
    r = client.post("/v1/heartbeat", json=payload, headers=AUTH)
    assert r.status_code == 401


def test_multi_node_online():
    for nid, auth in (("node-001", AUTH), ("node-002", AUTH2)):
        r = client.post("/v1/heartbeat", json={"node_id": nid, "firmware": "2.0.0-mvp", "rssi": -60}, headers=auth)
        assert r.status_code == 200
    fleet = client.get("/v1/fleet").json()["nodes"]
    ids = {n["node_id"] for n in fleet}
    assert {"node-001", "node-002"}.issubset(ids)
    assert all(n["online"] for n in fleet if n["node_id"] in ("node-001", "node-002"))


def test_node_history_recorded():
    client.post("/v1/heartbeat", json={"node_id": "node-001", "firmware": "2.0.0-mvp", "rssi": -50}, headers=AUTH)
    r = client.get("/v1/nodes/node-001/history?limit=5")
    assert r.status_code == 200
    assert len(r.json()["history"]) >= 1


def test_node_events_endpoint():
    r = client.get("/v1/nodes/node-001/events?limit=10")
    assert r.status_code == 200
    assert "events" in r.json()


def test_config_default_and_update():
    r = client.get("/v1/config/node-001", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["config"] == {}
    r2 = client.put("/v1/config/node-001", json={"config": {"sync_interval_ms": 15000}}, headers=AUTH)
    assert r2.status_code == 200
    r3 = client.get("/v1/config/node-001", headers=AUTH)
    assert r3.json()["config"].get("sync_interval_ms") == 15000


def test_analytics_summary():
    r = client.get("/v1/analytics/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["total_nodes"] >= 2
    assert "online_nodes" in body
    assert "average_rssi" in body


def test_analytics_node():
    r = client.get("/v1/analytics/nodes/node-001")
    assert r.status_code == 200
    assert r.json()["node_id"] == "node-001"


def test_audit_log():
    r = client.get("/v1/audit?limit=10")
    assert r.status_code == 200
    assert "events" in r.json()
    assert len(r.json()["events"]) >= 1


def test_provenance_owned_and_authorized():
    for st in ("owned", "authorized"):
        r = client.post("/v1/events", json={
            "node_id": "node-001", "source_type": st,
            "source_ref": "src-1", "event_type": "sample", "payload": {}
        }, headers=AUTH)
        assert r.status_code == 200
        assert r.json()["source_type"] == st


def test_anomalies_endpoint():
    r = client.get("/v1/analytics/anomalies")
    assert r.status_code == 200
    assert "anomalies" in r.json()
    assert "count" in r.json()


def test_forecast_endpoint():
    r = client.get("/v1/analytics/forecast/node-001?horizon=3")
    assert r.status_code == 200
    body = r.json()
    assert body["method"].startswith("moving_average")
    assert len(body["forecast"]) == 3