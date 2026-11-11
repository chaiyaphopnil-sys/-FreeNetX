import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, Field

# --- Configuration ---------------------------------------------------------
DB_PATH = os.getenv("FREENETX_DB", "freenetx.db")
TOKEN_PAIRS: dict[str, str] = {}
for item in os.getenv("FREENETX_NODE_TOKENS", "node-001:dev-only-change-this").split(","):
    if ":" in item:
        node_id, token = item.split(":", 1)
        TOKEN_PAIRS[node_id.strip()] = token.strip()

ADMIN_TOKEN = os.getenv("FREENETX_ADMIN_TOKEN", "").strip()
ONLINE_THRESHOLD_SECONDS = int(os.getenv("FREENETX_ONLINE_THRESHOLD", "90"))
MQTT_BROKER = os.getenv("FREENETX_MQTT_BROKER", "").strip()
MQTT_PORT = int(os.getenv("FREENETX_MQTT_PORT", "1883"))
MQTT_TOPIC_HEARTBEAT = os.getenv("FREENETX_MQTT_TOPIC_HB", "freenetx/heartbeat")
MQTT_TOPIC_EVENTS = os.getenv("FREENETX_MQTT_TOPIC_EV", "freenetx/events")

app = FastAPI(title="FreeNet-X Fleet API", version="2.0.0")
app.add_middleware(GZipMiddleware, minimum_size=500)


# --- Database --------------------------------------------------------------
def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with closing(connect()) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS nodes (
            node_id TEXT PRIMARY KEY,
            last_seen INTEGER NOT NULL,
            status TEXT NOT NULL,
            rssi INTEGER,
            firmware TEXT,
            ip_address TEXT
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            source_type TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS node_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id TEXT NOT NULL,
            ts INTEGER NOT NULL,
            status TEXT NOT NULL,
            rssi INTEGER,
            ip_address TEXT,
            firmware TEXT
        );
        CREATE TABLE IF NOT EXISTS node_config (
            node_id TEXT PRIMARY KEY,
            config_json TEXT NOT NULL,
            updated_at INTEGER NOT NULL,
            updated_by TEXT
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER NOT NULL,
            node_id TEXT,
            action TEXT NOT NULL,
            detail TEXT
        );
        """)
        con.commit()


@app.on_event("startup")
def startup():
    init_db()
    if MQTT_BROKER:
        _start_mqtt_bridge()


# --- Models ---------------------------------------------------------------
class Heartbeat(BaseModel):
    node_id: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    firmware: str = Field(default="2.0.0-mvp", max_length=40)
    rssi: Optional[int] = Field(default=None, ge=-127, le=0)
    ip_address: Optional[str] = Field(default=None, max_length=64)
    uptime_seconds: int = Field(default=0, ge=0)


class EventIn(BaseModel):
    node_id: str = Field(min_length=3, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    source_type: str = Field(pattern=r"^(owned|authorized)$")
    source_ref: str = Field(min_length=1, max_length=200)
    event_type: str = Field(min_length=1, max_length=64)
    payload: dict = Field(default_factory=dict)


class ConfigIn(BaseModel):
    config: dict = Field(default_factory=dict)


# --- Helpers --------------------------------------------------------------
def authenticate(node_id: str, authorization: Optional[str]):
    expected = TOKEN_PAIRS.get(node_id)
    if not expected or not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unknown node or missing bearer token")
    supplied = authorization[7:]
    if hashlib.sha256(supplied.encode()).digest() != hashlib.sha256(expected.encode()).digest():
        raise HTTPException(status_code=401, detail="Invalid node token")


def _admin_ok(x_admin_token: Optional[str]) -> bool:
    if not ADMIN_TOKEN:
        return True  # dev mode: no admin token configured
    return bool(x_admin_token) and x_admin_token == ADMIN_TOKEN


def audit(node_id: Optional[str], action: str, detail: Optional[str] = None):
    try:
        with closing(connect()) as con:
            con.execute(
                "INSERT INTO audit_log(ts,node_id,action,detail) VALUES(?,?,?,?)",
                (int(time.time()), node_id, action, detail),
            )
            con.commit()
    except Exception:
        pass


def _record_heartbeat(body: Heartbeat, now: Optional[int] = None):
    now = now or int(time.time())
    with closing(connect()) as con:
        con.execute(
            """
            INSERT INTO nodes(node_id,last_seen,status,rssi,firmware,ip_address)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(node_id) DO UPDATE SET
              last_seen=excluded.last_seen,status=excluded.status,
              rssi=excluded.rssi,firmware=excluded.firmware,
              ip_address=excluded.ip_address
            """,
            (body.node_id, now, "online", body.rssi, body.firmware, body.ip_address),
        )
        con.execute(
            "INSERT INTO node_history(node_id,ts,status,rssi,ip_address,firmware) VALUES(?,?,?,?,?,?)",
            (body.node_id, now, "online", body.rssi, body.ip_address, body.firmware),
        )
        con.commit()
    return now


# --- Routes: health -------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok", "service": "FreeNet-X Fleet API", "version": "2.0.0"}


# --- Routes: heartbeat ----------------------------------------------------
@app.post("/v1/heartbeat")
def heartbeat(body: Heartbeat, authorization: Optional[str] = Header(default=None)):
    authenticate(body.node_id, authorization)
    now = _record_heartbeat(body)
    audit(body.node_id, "heartbeat", f"rssi={body.rssi} ip={body.ip_address}")
    return {"accepted": True, "node_id": body.node_id, "server_time": now}


# --- Routes: events -------------------------------------------------------
@app.post("/v1/events")
def ingest_event(body: EventIn, authorization: Optional[str] = Header(default=None)):
    authenticate(body.node_id, authorization)
    now = int(time.time())
    with closing(connect()) as con:
        con.execute(
            """
            INSERT INTO events(node_id,created_at,source_type,source_ref,event_type,payload_json)
            VALUES(?,?,?,?,?,?)
            """,
            (body.node_id, now, body.source_type, body.source_ref,
             body.event_type, json.dumps(body.payload, separators=(",", ":"))),
        )
        con.commit()
    audit(body.node_id, "event", f"{body.source_type}:{body.event_type}:{body.source_ref}")
    return {"accepted": True, "source_type": body.source_type, "created_at": now}


# --- Routes: fleet --------------------------------------------------------
@app.get("/v1/fleet")
def fleet():
    now = int(time.time())
    with closing(connect()) as con:
        rows = con.execute(
            "SELECT node_id,last_seen,status,rssi,firmware,ip_address FROM nodes ORDER BY node_id"
        ).fetchall()
    nodes = []
    for row in rows:
        d = dict(row)
        age = now - (d["last_seen"] or 0)
        d["age_seconds"] = age
        d["online"] = age <= ONLINE_THRESHOLD_SECONDS and d["status"] == "online"
        nodes.append(d)
    return {"nodes": nodes, "server_time": now, "online_threshold_seconds": ONLINE_THRESHOLD_SECONDS}


# --- Routes: node events/history ------------------------------------------
@app.get("/v1/nodes/{node_id}/events")
def node_events(node_id: str, limit: int = 50):
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be 1..200")
    with closing(connect()) as con:
        rows = con.execute(
            "SELECT id,node_id,created_at,source_type,source_ref,event_type,payload_json "
            "FROM events WHERE node_id=? ORDER BY id DESC LIMIT ?",
            (node_id, limit),
        ).fetchall()
    return {"events": [dict(row) for row in rows]}


@app.get("/v1/nodes/{node_id}/history")
def node_history(node_id: str, limit: int = 50):
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be 1..500")
    with closing(connect()) as con:
        rows = con.execute(
            "SELECT id,node_id,ts,status,rssi,ip_address,firmware FROM node_history "
            "WHERE node_id=? ORDER BY id DESC LIMIT ?",
            (node_id, limit),
        ).fetchall()
    return {"history": [dict(row) for row in rows]}


# --- Routes: config manager ----------------------------------------------
@app.get("/v1/config/{node_id}")
def get_config(node_id: str, authorization: Optional[str] = Header(default=None)):
    authenticate(node_id, authorization)
    with closing(connect()) as con:
        row = con.execute(
            "SELECT config_json,updated_at,updated_by FROM node_config WHERE node_id=?",
            (node_id,),
        ).fetchone()
    if not row:
        return {"node_id": node_id, "config": {}, "updated_at": None, "updated_by": None}
    try:
        cfg = json.loads(row["config_json"])
    except Exception:
        cfg = {}
    return {
        "node_id": node_id,
        "config": cfg,
        "updated_at": row["updated_at"],
        "updated_by": row["updated_by"],
    }


@app.put("/v1/config/{node_id}")
def set_config(
    node_id: str,
    body: ConfigIn,
    authorization: Optional[str] = Header(default=None),
    x_admin_token: Optional[str] = Header(default=None),
):
    authenticate(node_id, authorization)
    if not _admin_ok(x_admin_token):
        raise HTTPException(status_code=403, detail="Admin token required")
    now = int(time.time())
    with closing(connect()) as con:
        con.execute(
            """
            INSERT INTO node_config(node_id,config_json,updated_at,updated_by)
            VALUES(?,?,?,?)
            ON CONFLICT(node_id) DO UPDATE SET
              config_json=excluded.config_json,updated_at=excluded.updated_at,
              updated_by=excluded.updated_by
            """,
            (node_id, json.dumps(body.config, separators=(",", ":")), now, "admin"),
        )
        con.commit()
    audit(node_id, "config_update", json.dumps(body.config, separators=(",", ":")))
    return {"accepted": True, "node_id": node_id, "updated_at": now}


# --- Routes: audit log ----------------------------------------------------
@app.get("/v1/audit")
def audit_log(limit: int = 100, x_admin_token: Optional[str] = Header(default=None)):
    if not _admin_ok(x_admin_token):
        raise HTTPException(status_code=403, detail="Admin token required")
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit must be 1..500")
    with closing(connect()) as con:
        rows = con.execute(
            "SELECT id,ts,node_id,action,detail FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return {"events": [dict(row) for row in rows]}


# --- Routes: analytics ----------------------------------------------------
@app.get("/v1/analytics/summary")
def analytics_summary():
    now = int(time.time())
    with closing(connect()) as con:
        rows = con.execute(
            "SELECT node_id,last_seen,status,rssi,firmware,ip_address FROM nodes ORDER BY node_id"
        ).fetchall()
        ev_count = con.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]
        audit_count = con.execute("SELECT COUNT(*) AS c FROM audit_log").fetchone()["c"]
    nodes = []
    online = 0
    offline = []
    rssi_values = []
    for row in rows:
        d = dict(row)
        age = now - (d["last_seen"] or 0)
        is_online = age <= ONLINE_THRESHOLD_SECONDS and d["status"] == "online"
        if is_online:
            online += 1
        else:
            offline.append(d["node_id"])
        if d["rssi"] is not None:
            rssi_values.append(d["rssi"])
        nodes.append({
            "node_id": d["node_id"],
            "online": is_online,
            "age_seconds": age,
            "rssi": d["rssi"],
            "firmware": d["firmware"],
        })
    avg_rssi = (sum(rssi_values) / len(rssi_values)) if rssi_values else None
    return {
        "server_time": now,
        "total_nodes": len(nodes),
        "online_nodes": online,
        "offline_nodes": offline,
        "average_rssi": avg_rssi,
        "total_events": ev_count,
        "total_audit_entries": audit_count,
        "nodes": nodes,
    }


@app.get("/v1/analytics/nodes/{node_id}")
def analytics_node(node_id: str):
    now = int(time.time())
    with closing(connect()) as con:
        node = con.execute(
            "SELECT node_id,last_seen,status,rssi,firmware,ip_address FROM nodes WHERE node_id=?",
            (node_id,),
        ).fetchone()
        hist = con.execute(
            "SELECT ts,rssi FROM node_history WHERE node_id=? ORDER BY id DESC LIMIT 50",
            (node_id,),
        ).fetchall()
        ev = con.execute(
            "SELECT COUNT(*) AS c FROM events WHERE node_id=?", (node_id,)
        ).fetchone()["c"]
    if not node:
        raise HTTPException(status_code=404, detail="Unknown node")
    rssi_hist = [r["rssi"] for r in hist if r["rssi"] is not None]
    avg = (sum(rssi_hist) / len(rssi_hist)) if rssi_hist else None
    return {
        "node_id": node_id,
        "last_seen": node["last_seen"],
        "age_seconds": now - (node["last_seen"] or 0),
        "online": (now - (node["last_seen"] or 0)) <= ONLINE_THRESHOLD_SECONDS,
        "current_rssi": node["rssi"],
        "average_rssi_last_50": avg,
        "rssi_samples": len(rssi_hist),
        "event_count": ev,
        "firmware": node["firmware"],
    }


@app.get("/v1/analytics/forecast/{node_id}")
def analytics_forecast(node_id: str, horizon: int = 5):
    # Simple moving-average forecast (NOT machine learning).
    if horizon < 1 or horizon > 50:
        raise HTTPException(status_code=400, detail="horizon must be 1..50")
    with closing(connect()) as con:
        rows = con.execute(
            "SELECT rssi FROM node_history WHERE node_id=? AND rssi IS NOT NULL "
            "ORDER BY id DESC LIMIT 30",
            (node_id,),
        ).fetchall()
    vals = [r["rssi"] for r in rows]
    if not vals:
        raise HTTPException(status_code=404, detail="No RSSI history for node")
    window = vals[: min(5, len(vals))]
    avg = sum(window) / len(window)
    # naive forecast: repeat moving average; widen uncertainty with sample std
    import statistics
    std = statistics.pstdev(vals) if len(vals) > 1 else 0.0
    forecast = [{"step": i + 1, "predicted_rssi": round(avg, 2),
                "lower": round(avg - std, 2), "upper": round(avg + std, 2)}
               for i in range(horizon)]
    return {
        "node_id": node_id,
        "method": "moving_average (not ML)",
        "samples": len(vals),
        "last_rssi": vals[0],
        "forecast": forecast,
    }


# --- Analytics: anomaly detection (rule-based, not ML) ---------------------
@app.get("/v1/analytics/anomalies")
def analytics_anomalies(rssi_drop_threshold: int = 15):
    now = int(time.time())
    anomalies: List[Dict[str, Any]] = []
    with closing(connect()) as con:
        rows = con.execute(
            "SELECT node_id,last_seen,status,rssi,firmware FROM nodes"
        ).fetchall()
        for row in rows:
            d = dict(row)
            age = now - (d["last_seen"] or 0)
            if age > ONLINE_THRESHOLD_SECONDS:
                anomalies.append({
                    "node_id": d["node_id"], "type": "offline",
                    "detail": f"no heartbeat for {age}s", "severity": "high",
                })
                continue
            # RSSI drop detection using recent history
            hist = con.execute(
                "SELECT rssi FROM node_history WHERE node_id=? ORDER BY id DESC LIMIT 10",
                (d["node_id"],),
            ).fetchall()
            rssis = [h["rssi"] for h in hist if h["rssi"] is not None]
            if len(rssis) >= 3:
                recent = rssis[0]
                baseline = sum(rssis[1:]) / (len(rssis) - 1)
                if baseline - recent >= rssi_drop_threshold:
                    anomalies.append({
                        "node_id": d["node_id"], "type": "rssi_drop",
                        "detail": f"RSSI {recent} vs baseline {baseline:.1f}",
                        "severity": "medium",
                    })
            # Flapping: too many status transitions in recent history
            transitions = con.execute(
                "SELECT COUNT(*) AS c FROM (SELECT status FROM node_history WHERE node_id=? ORDER BY id DESC LIMIT 20)",
                (d["node_id"],),
            ).fetchone()["c"]
            if transitions and len(hist) >= 10:
                # heuristic: many records in short window
                pass
    return {"server_time": now, "count": len(anomalies), "anomalies": anomalies}


# --- MQTT bridge (optional) ----------------------------------------------
def _ingest_heartbeat_dict(data: dict):
    try:
        body = Heartbeat(**data)
    except Exception:
        return False
    if body.node_id not in TOKEN_PAIRS:
        return False
    # MQTT path still requires a token? We trust broker-side auth; require node exists.
    _record_heartbeat(body)
    audit(body.node_id, "heartbeat_mqtt", f"rssi={body.rssi}")
    return True


def _start_mqtt_bridge():
    try:
        import paho.mqtt.client as mqtt  # type: ignore
    except Exception:
        print("[MQTT] paho-mqtt not installed; bridge disabled.")
        return

    def on_connect(client, userdata, flags, rc, properties=None):
        client.subscribe(MQTT_TOPIC_HEARTBEAT)
        client.subscribe(MQTT_TOPIC_EVENTS)
        print(f"[MQTT] connected rc={rc}")

    def on_message(client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode("utf-8", errors="replace"))
        except Exception:
            return
        if msg.topic == MQTT_TOPIC_HEARTBEAT:
            _ingest_heartbeat_dict(data)
        elif msg.topic == MQTT_TOPIC_EVENTS:
            try:
                body = EventIn(**data)
            except Exception:
                return
            if body.node_id not in TOKEN_PAIRS:
                return
            now = int(time.time())
            with closing(connect()) as con:
                con.execute(
                    "INSERT INTO events(node_id,created_at,source_type,source_ref,event_type,payload_json) VALUES(?,?,?,?,?,?)",
                    (body.node_id, now, body.source_type, body.source_ref,
                     body.event_type, json.dumps(body.payload, separators=(",", ":"))),
                )
                con.commit()
            audit(body.node_id, "event_mqtt", f"{body.source_type}:{body.event_type}")

    def run():
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2) if hasattr(mqtt, "CallbackAPIVersion") else mqtt.Client()
        client.on_connect = on_connect
        client.on_message = on_message
        try:
            client.connect(MQTT_BROKER, MQTT_PORT, 60)
            client.loop_forever()
        except Exception as e:
            print(f"[MQTT] bridge error: {e}")

    t = threading.Thread(target=run, daemon=True, name="mqtt-bridge")
    t.start()
    print(f"[MQTT] bridge starting for {MQTT_BROKER}:{MQTT_PORT}")