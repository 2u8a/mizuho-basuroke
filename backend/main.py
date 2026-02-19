import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from firebase_functions import https_fn
import firebase_admin
from firebase_admin import firestore

from google.transit import gtfs_realtime_pb2


# ---- Firebase Admin init（import時OK / Firestore clientは遅延生成）----
if not firebase_admin._apps:
    firebase_admin.initialize_app()

_db = None

def get_db():
    global _db
    if _db is None:
        _db = firestore.client()
    return _db


API_KEY = os.environ.get("API_KEY", "")
DEFAULT_VEHICLE_ID = os.environ.get("DEFAULT_VEHICLE_ID", "mizuho-bus-01")
MAX_ACCURACY_M = float(os.environ.get("MAX_ACCURACY_M", "200"))
ZERO_EPS = 1e-8


def _now_ts() -> datetime:
    return datetime.now(timezone.utc)


def _parse_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _accept_decision(lat: Optional[float], lon: Optional[float], acc: Optional[float]) -> Tuple[bool, Optional[str]]:
    if lat is None or lon is None:
        return False, "missing_lat_lon"
    if abs(lat) < ZERO_EPS and abs(lon) < ZERO_EPS:
        return False, "zero_lat_lon"
    if acc is None:
        return False, "missing_accuracy"
    if acc > MAX_ACCURACY_M:
        return False, f"accuracy_too_large({acc})"
    return True, None


def _require_api_key(req) -> bool:
    if not API_KEY:
        return True
    got = req.headers.get("X-API-KEY", "")
    return got == API_KEY


@https_fn.on_request()
def gps(req: https_fn.Request):
    if req.method != "POST":
        return https_fn.Response("Method Not Allowed", status=405)

    if not _require_api_key(req):
        return https_fn.Response("Unauthorized", status=401)

    body: Dict[str, Any] = req.get_json(silent=True) or {}

    vehicle_id = str(body.get("vehicle_id") or DEFAULT_VEHICLE_ID)
    client_ts = body.get("timestamp")
    lat = _parse_float(body.get("lat"))
    lon = _parse_float(body.get("lon"))
    acc = _parse_float(body.get("accuracy"))
    speed = _parse_float(body.get("speed"))
    heading = _parse_float(body.get("heading"))
    server_received_at = _now_ts()

    accepted, reject_reason = _accept_decision(lat, lon, acc)

    db = get_db()

    log_doc = {
        "vehicle_id": vehicle_id,
        "client_timestamp": client_ts,
        "server_received_at": server_received_at,
        "lat": lat,
        "lon": lon,
        "accuracy": acc,
        "speed": speed,
        "heading": heading,
        "accepted": accepted,
        "reject_reason": reject_reason,
        "raw": body,
    }
    db.collection("gps_logs").add(log_doc)

    if accepted:
        latest_doc = {
            "vehicle_id": vehicle_id,
            "client_timestamp": client_ts,
            "server_received_at": server_received_at,
            "lat": lat,
            "lon": lon,
            "accuracy": acc,
            "speed": speed,
            "heading": heading,
            "accepted": True,
            "reject_reason": None,
        }
        db.collection("vehicles").document(vehicle_id).collection("state").document("latest").set(latest_doc)

    return https_fn.Response(
        response=f'{{"ok":true,"vehicle_id":"{vehicle_id}","accepted":{str(accepted).lower()}}}',
        status=200,
        content_type="application/json",
    )


@https_fn.on_request()
def gtfs_rt(req: https_fn.Request):
    if req.method != "GET":
        return https_fn.Response("Method Not Allowed", status=405)

    vehicle_id = req.args.get("vehicle_id") or DEFAULT_VEHICLE_ID
    db = get_db()

    snap = db.collection("vehicles").document(vehicle_id).collection("state").document("latest").get()

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.incrementality = gtfs_realtime_pb2.FeedHeader.FULL_DATASET
    feed.header.timestamp = int(_now_ts().timestamp())

    if snap.exists:
        latest = snap.to_dict() or {}
        if latest.get("accepted", False):
            lat = float(latest["lat"])
            lon = float(latest["lon"])

            ts = latest.get("server_received_at")
            feed_ts = int(ts.timestamp()) if hasattr(ts, "timestamp") else int(_now_ts().timestamp())

            feed.header.timestamp = feed_ts
            ent = feed.entity.add()
            ent.id = f"vehicle:{vehicle_id}"

            vp = ent.vehicle
            vp.vehicle.id = vehicle_id
            vp.timestamp = feed_ts
            vp.position.latitude = lat
            vp.position.longitude = lon

            sp = latest.get("speed")
            if sp is not None:
                try:
                    vp.position.speed = float(sp)
                except Exception:
                    pass
            hd = latest.get("heading")
            if hd is not None:
                try:
                    vp.position.bearing = float(hd)
                except Exception:
                    pass

    pb = feed.SerializeToString()
    return https_fn.Response(pb, status=200, content_type="application/x-protobuf")
