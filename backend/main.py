"""
Module B – GPS受信 / route推定 / shapeベース便推定 / 遅延判定 / GTFS-RT生成 / Firestore包括保存

瑞穂町コミュニティバス バスロケーションシステム PoC

この版で行うこと:
- GPS受信
- route候補推定（shape幾何のみ）
- trip候補推定（時刻+shape+継続性+route lock）
- 遅延推定
- Firestoreへ包括保存
  - gps_logs
  - vehicles/{vehicle_id}/state/latest
  - vehicles/{vehicle_id}/events/{event_id}
  - vehicles/{vehicle_id}/events/{event_id}/candidates/{candidate_id}
  - vehicles/{vehicle_id}/gtfs_rt_audit/{snapshot_id}
- GTFS-Realtime生成
"""

# ============================================================
# imports
# ============================================================
import os
import csv
import json
import math
import time
import hashlib
import base64
import logging
import threading
from datetime import datetime, timedelta, timezone

from firebase_functions import https_fn
import firebase_admin
from firebase_admin import firestore as _firestore_mod
from google.transit import gtfs_realtime_pb2

# ============================================================
# constants
# ============================================================
JST = timezone(timedelta(hours=9))
VEHICLE_ID = "mizuho-bus-01"
GTFS_DIR = os.path.join(os.path.dirname(__file__), "data", "gtfs")

# --- matching thresholds ---
TRIP_TIME_BUFFER_SEC = 600
MAX_MATCH_ERROR_M = 5000          # 遅延大きめでも便を捨てないよう緩和
MAX_SHAPE_OFFSET_M = 250
MAX_JUMP_DISTANCE_M = 1500
REVERSE_REJECT_M = 150
STOP_NEAR_GEO_THRESHOLD_M = 70
SMOOTHING_LOOKBACK_SEC = 30

# --- route lock ---
SAME_TRIP_BONUS = 180
SAME_ROUTE_BONUS = 80
ROUTE_SWITCH_PENALTY = 250
ROUTE_LOCK_BONUS = 500
OTHER_ROUTE_PENALTY_WHEN_LOCKED = 800
LOCK_ESTABLISH_COUNT = 3
LOCK_RELEASE_NOMATCH_SEC = 300

# --- geo ---
EARTH_RADIUS_M = 6_371_000
LAT_MIN, LAT_MAX = 35.74, 35.80
LON_MIN, LON_MAX = 139.32, 139.38

# --- persistence ---
EVENT_RETENTION_DAYS = 400
CANDIDATE_STORE_LIMIT = 8
ENABLE_GTFS_RT_AUDIT = True
ALGORITHM_VERSION = "route-first-trip-match-full-persistence-v2"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("busroq")

# ============================================================
# lazy init state
# ============================================================
_init_done = False
_init_lock = threading.Lock()

db = None
API_KEY = None

STOPS = {}
ROUTES = {}
TRIPS = {}
TRIP_STOP_TIMES = {}
CALENDAR = {}
CALENDAR_DATES = {}

SHAPES = {}
SHAPE_TOTAL_DISTANCE = {}
TRIP_PROGRESS = {}
STOP_TO_SHAPE_DIST = {}
TRIP_SHAPE_START_ABS = {}
TRIP_ROUTE_LENGTH = {}

ROUTE_TO_SHAPES = {}
SHAPE_TO_ROUTES = {}
PRECOMPUTED_VERSION = None

# ============================================================
# init
# ============================================================
def _load_csv(filename):
    path = os.path.join(GTFS_DIR, filename)
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _parse_gtfs_time(hhmmss):
    if not hhmmss:
        return 0
    hh, mm, ss = hhmmss.strip().split(":")
    return int(hh) * 3600 + int(mm) * 60 + int(ss)


def _now_jst():
    return datetime.now(JST)


def _seconds_since_midnight(dt):
    return dt.hour * 3600 + dt.minute * 60 + dt.second


def _file_sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _do_init():
    global _init_done, db, API_KEY
    global STOPS, ROUTES, TRIPS, TRIP_STOP_TIMES, CALENDAR, CALENDAR_DATES
    global SHAPES, SHAPE_TOTAL_DISTANCE, TRIP_PROGRESS
    global STOP_TO_SHAPE_DIST, TRIP_SHAPE_START_ABS, TRIP_ROUTE_LENGTH
    global ROUTE_TO_SHAPES, SHAPE_TO_ROUTES, PRECOMPUTED_VERSION

    if _init_done:
        return

    firebase_admin.initialize_app()
    db = _firestore_mod.client()
    API_KEY = os.environ.get("API_KEY")

    # --- GTFS CSV ---
    STOPS.update({
        r["stop_id"]: {
            "name": r.get("stop_name", r["stop_id"]),
            "lat": float(r["stop_lat"]),
            "lon": float(r["stop_lon"]),
        }
        for r in _load_csv("stops.txt")
    })

    ROUTES.update({r["route_id"]: r for r in _load_csv("routes.txt")})
    TRIPS.update({r["trip_id"]: r for r in _load_csv("trips.txt")})

    for r in _load_csv("stop_times.txt"):
        tid = r["trip_id"]
        TRIP_STOP_TIMES.setdefault(tid, []).append({
            "stop_id": r["stop_id"],
            "arrival_sec": _parse_gtfs_time(r["arrival_time"]),
            "departure_sec": _parse_gtfs_time(r["departure_time"]),
            "stop_sequence": int(r["stop_sequence"]),
        })
    for tid in TRIP_STOP_TIMES:
        TRIP_STOP_TIMES[tid].sort(key=lambda x: x["stop_sequence"])

    CALENDAR.update({r["service_id"]: r for r in _load_csv("calendar.txt")})
    for r in _load_csv("calendar_dates.txt"):
        CALENDAR_DATES[(r["service_id"], r["date"])] = int(r["exception_type"])

    # --- precomputed.json ---
    pc_path = os.path.join(GTFS_DIR, "precomputed.json")
    PRECOMPUTED_VERSION = _file_sha1(pc_path)
    with open(pc_path, "r", encoding="utf-8") as f:
        pc = json.load(f)

    SHAPES.update({
        sid: [
            {
                "lat": float(p["lat"]),
                "lon": float(p["lon"]),
                "dist_traveled": float(p["dist_traveled"]),
            }
            for p in pts
        ]
        for sid, pts in pc["shapes"].items()
    })
    SHAPE_TOTAL_DISTANCE.update({k: float(v) for k, v in pc["shape_total_distance"].items()})
    TRIP_PROGRESS.update(pc["trip_progress"])

    for key, val in pc["stop_to_shape_dist"].items():
        trip_id, seq_str = key.split("|")
        STOP_TO_SHAPE_DIST[(trip_id, int(seq_str))] = float(val)

    TRIP_SHAPE_START_ABS.update({k: float(v) for k, v in pc["trip_shape_start_abs"].items()})
    TRIP_ROUTE_LENGTH.update({k: float(v) for k, v in pc["trip_route_length"].items()})

    ROUTE_TO_SHAPES.clear()
    SHAPE_TO_ROUTES.clear()
    for trip in TRIPS.values():
        route_id = trip.get("route_id")
        shape_id = trip.get("shape_id")
        if not route_id or not shape_id:
            continue
        ROUTE_TO_SHAPES.setdefault(route_id, set()).add(shape_id)
        SHAPE_TO_ROUTES.setdefault(shape_id, set()).add(route_id)

    _init_done = True
    logger.info(
        "Init complete: stops=%d routes=%d trips=%d shapes=%d precomputed=%s",
        len(STOPS), len(ROUTES), len(TRIPS), len(SHAPES), PRECOMPUTED_VERSION[:10],
    )


def _ensure_init():
    global _init_done
    if _init_done:
        return
    with _init_lock:
        if not _init_done:
            _do_init()

# ============================================================
# utils
# ============================================================
def _route_name(route_id):
    r = ROUTES.get(route_id, {})
    return (
        r.get("route_long_name")
        or r.get("route_short_name")
        or route_id
    )


def _parse_payload_timestamp(data):
    ts = data.get("timestamp")
    if not ts:
        return _now_jst()
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=JST)
        return dt.astimezone(JST)
    except Exception:
        return _now_jst()


def _cors_headers(content_type):
    return {
        "Content-Type": content_type,
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-API-KEY",
    }


def _expire_at(days=EVENT_RETENTION_DAYS):
    return _now_jst() + timedelta(days=days)


def _make_event_id(vehicle_id, ts_unix, lat, lon):
    raw = f"{vehicle_id}|{ts_unix}|{lat}|{lon}|{time.time_ns()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def _safe_firestore_value(v):
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        return round(v, 3)
    if isinstance(v, dict):
        return {str(k): _safe_firestore_value(val) for k, val in v.items()}
    if isinstance(v, (list, tuple)):
        return [_safe_firestore_value(x) for x in v]
    return v

# ============================================================
# geometry
# ============================================================
def _haversine(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def _latlon_to_xy(lat, lon, ref_lat, ref_lon):
    x = math.radians(lon - ref_lon) * EARTH_RADIUS_M * math.cos(math.radians(ref_lat))
    y = math.radians(lat - ref_lat) * EARTH_RADIUS_M
    return x, y


def _project_point_to_segment(lat, lon, a, b):
    ref_lat = (a["lat"] + b["lat"]) / 2.0
    ref_lon = (a["lon"] + b["lon"]) / 2.0
    px, py = _latlon_to_xy(lat, lon, ref_lat, ref_lon)
    ax, ay = _latlon_to_xy(a["lat"], a["lon"], ref_lat, ref_lon)
    bx, by = _latlon_to_xy(b["lat"], b["lon"], ref_lat, ref_lon)

    dx, dy = bx - ax, by - ay
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq < 1e-9:
        t = 0.0
    else:
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg_len_sq))

    qx, qy = ax + t * dx, ay + t * dy
    return {
        "distance_m": a["dist_traveled"] + t * (b["dist_traveled"] - a["dist_traveled"]),
        "offset_m": math.hypot(px - qx, py - qy),
    }


def project_to_shape(shape_id, lat, lon):
    pts = SHAPES.get(shape_id)
    if not pts or len(pts) < 2:
        return None

    best = None
    for i in range(len(pts) - 1):
        proj = _project_point_to_segment(lat, lon, pts[i], pts[i + 1])
        cand = {**proj, "shape_id": shape_id, "segment_index": i}

        # loop shapeで同程度のoffsetなら、distanceの若い方を優先
        if best is None or cand["offset_m"] < best["offset_m"] - 1.0:
            best = cand
        elif abs(cand["offset_m"] - best["offset_m"]) <= 1.0:
            if cand["distance_m"] < best["distance_m"]:
                best = cand

    return best


def _project_to_trip_distance(trip_id, lat, lon, anchor_distance_m=None):
    shape_id = TRIPS[trip_id]["shape_id"]
    proj = project_to_shape(shape_id, lat, lon)
    if proj is None:
        return None

    abs_d = proj["distance_m"]
    base_abs = TRIP_SHAPE_START_ABS.get(trip_id, 0.0)
    total_shape = SHAPE_TOTAL_DISTANCE.get(shape_id, 0.0)

    raw_rel = abs_d - base_abs
    candidates = [c for c in [raw_rel, raw_rel + total_shape, raw_rel - total_shape] if c >= -100.0]
    if not candidates:
        candidates = [0.0]

    if anchor_distance_m is None:
        distance_m = min((max(0.0, c) for c in candidates), key=abs)
    else:
        distance_m = min((max(0.0, c) for c in candidates), key=lambda x: abs(x - anchor_distance_m))

    return {
        **proj,
        "abs_distance_m": round(abs_d, 1),
        "distance_m": round(distance_m, 1),
    }

# ============================================================
# trip progress interpolation
# ============================================================
def time_to_distance(trip_id, t_sec):
    prog = TRIP_PROGRESS.get(trip_id, [])
    if not prog:
        return 0.0
    if t_sec <= prog[0]["time_sec"]:
        return float(prog[0]["distance_m"])
    if t_sec >= prog[-1]["time_sec"]:
        return float(prog[-1]["distance_m"])

    for i in range(len(prog) - 1):
        p1, p2 = prog[i], prog[i + 1]
        if p1["time_sec"] <= t_sec <= p2["time_sec"]:
            dt = p2["time_sec"] - p1["time_sec"]
            if dt <= 0:
                return float(p1["distance_m"])
            ratio = (t_sec - p1["time_sec"]) / dt
            return float(p1["distance_m"]) + ratio * (float(p2["distance_m"]) - float(p1["distance_m"]))
    return float(prog[-1]["distance_m"])


def distance_to_time(trip_id, d_m):
    prog = TRIP_PROGRESS.get(trip_id, [])
    if not prog:
        return 0

    if d_m <= prog[0]["distance_m"]:
        return int(prog[0]["time_sec"])
    if d_m >= prog[-1]["distance_m"]:
        return int(prog[-1]["time_sec"])

    for i in range(len(prog) - 1):
        p1, p2 = prog[i], prog[i + 1]
        d1 = float(p1["distance_m"])
        d2 = float(p2["distance_m"])
        if d1 <= d_m <= d2:
            dd = d2 - d1
            if dd <= 0:
                return int(p1["time_sec"])
            ratio = (d_m - d1) / dd
            return int(round(p1["time_sec"] + ratio * (p2["time_sec"] - p1["time_sec"])))
    return int(prog[-1]["time_sec"])

# ============================================================
# service / candidates
# ============================================================
def resolve_active_service_ids(now_dt):
    date_str = now_dt.strftime("%Y%m%d")
    weekday_idx = now_dt.weekday()  # 0=Mon
    weekday_key = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"][weekday_idx]

    active = set()

    for sid, row in CALENDAR.items():
        start_date = row.get("start_date", "00000000")
        end_date = row.get("end_date", "99999999")
        if not (start_date <= date_str <= end_date):
            continue
        if str(row.get(weekday_key, "0")) == "1":
            active.add(sid)

    # calendar_dates overrides
    for (sid, d), exc_type in CALENDAR_DATES.items():
        if d != date_str:
            continue
        if exc_type == 1:
            active.add(sid)
        elif exc_type == 2:
            active.discard(sid)

    return sorted(active)


def _candidate_trips(now_dt):
    now_sec = _seconds_since_midnight(now_dt)
    active_service_ids = set(resolve_active_service_ids(now_dt))
    rows = []

    for trip_id, trip in TRIPS.items():
        if trip.get("service_id") not in active_service_ids:
            continue

        prog = TRIP_PROGRESS.get(trip_id, [])
        if not prog:
            continue

        start_sec = int(prog[0]["time_sec"])
        end_sec = int(prog[-1]["time_sec"])
        if start_sec - TRIP_TIME_BUFFER_SEC <= now_sec <= end_sec + TRIP_TIME_BUFFER_SEC:
            rows.append({
                "trip_id": trip_id,
                "route_id": trip.get("route_id"),
                "shape_id": trip.get("shape_id"),
                "service_id": trip.get("service_id"),
                "start_sec": start_sec,
                "end_sec": end_sec,
            })

    rows.sort(key=lambda x: (x["start_sec"], x["route_id"], x["trip_id"]))
    return rows

# ============================================================
# stop / route helpers
# ============================================================
def _nearest_progress_stop(trip_id, current_distance_m):
    prog = TRIP_PROGRESS.get(trip_id, [])
    if not prog:
        return None, None, None, None

    best = None
    for p in prog:
        d = float(p["distance_m"])
        gap = abs(d - current_distance_m)
        if best is None or gap < best["gap"]:
            best = {
                "gap": gap,
                "distance_m": d,
                "stop_id": p.get("stop_id"),
                "stop_sequence": p.get("stop_sequence"),
            }

    if best is None:
        return None, None, None, None

    stop_id = best["stop_id"]
    stop_name = STOPS.get(stop_id, {}).get("name", stop_id)
    return stop_id, best["stop_sequence"], stop_name, best["distance_m"]


def estimate_route_candidates(lat, lon, topn=5):
    best_by_route = {}

    for shape_id in SHAPES.keys():
        proj = project_to_shape(shape_id, lat, lon)
        if not proj:
            continue

        route_ids = SHAPE_TO_ROUTES.get(shape_id, set())
        for route_id in route_ids:
            row = {
                "route_id": route_id,
                "route_name": _route_name(route_id),
                "shape_id": shape_id,
                "offset_m": round(proj["offset_m"], 1),
                "distance_on_shape_m": round(proj["distance_m"], 1),
                "shape_total_m": round(SHAPE_TOTAL_DISTANCE.get(shape_id, 0.0), 1),
            }
            cur = best_by_route.get(route_id)
            if cur is None or row["offset_m"] < cur["offset_m"]:
                best_by_route[route_id] = row

    rows = sorted(best_by_route.values(), key=lambda x: (x["offset_m"], x["route_id"]))
    return rows[:topn]

# ============================================================
# route lock
# ============================================================
def _get_prev_trip(prev_state):
    if not prev_state:
        return None
    trip = prev_state.get("trip")
    return trip if isinstance(trip, dict) else None


def _get_route_lock(prev_state):
    if not prev_state:
        return {
            "locked": False,
            "route_id": None,
            "trip_id": None,
            "consecutive": 0,
            "lock_since_unix": None,
            "last_match_unix": None,
            "last_nomatch_unix": None,
        }
    lock = prev_state.get("route_lock") or {}
    return {
        "locked": bool(lock.get("locked", False)),
        "route_id": lock.get("route_id"),
        "trip_id": lock.get("trip_id"),
        "consecutive": int(lock.get("consecutive", 0) or 0),
        "lock_since_unix": lock.get("lock_since_unix"),
        "last_match_unix": lock.get("last_match_unix"),
        "last_nomatch_unix": lock.get("last_nomatch_unix"),
    }


def _is_trip_ended(trip_id, now_dt):
    prog = TRIP_PROGRESS.get(trip_id, [])
    if not prog:
        return False
    now_sec = _seconds_since_midnight(now_dt)
    return now_sec > int(prog[-1]["time_sec"]) + 180


def _compute_route_lock(prev_state, trip_match, now_dt):
    now_unix = int(now_dt.timestamp())
    prev_lock = _get_route_lock(prev_state)
    prev_trip = _get_prev_trip(prev_state)

    if trip_match:
        same_route = prev_lock.get("route_id") == trip_match.get("route_id")
        same_trip = prev_lock.get("trip_id") == trip_match.get("trip_id")

        if same_route:
            consecutive = prev_lock.get("consecutive", 0) + 1
        else:
            consecutive = 1

        locked = prev_lock.get("locked", False)
        if not locked and consecutive >= LOCK_ESTABLISH_COUNT:
            locked = True

        lock_since_unix = prev_lock.get("lock_since_unix")
        if locked and lock_since_unix is None:
            lock_since_unix = now_unix

        return {
            "locked": locked,
            "route_id": trip_match.get("route_id"),
            "trip_id": trip_match.get("trip_id"),
            "consecutive": consecutive,
            "lock_since_unix": lock_since_unix,
            "last_match_unix": now_unix,
            "last_nomatch_unix": None,
            "same_trip": same_trip,
            "state_label": "LOCKED" if locked else "tracking",
        }

    # no-match 時
    if prev_lock.get("locked"):
        last_match_unix = prev_lock.get("last_match_unix")
        if last_match_unix and now_unix - int(last_match_unix) <= LOCK_RELEASE_NOMATCH_SEC:
            keep = dict(prev_lock)
            keep["last_nomatch_unix"] = now_unix
            keep["state_label"] = "hold_on_nomatch"
            return keep

    if prev_trip and prev_trip.get("trip_id") and not _is_trip_ended(prev_trip["trip_id"], now_dt):
        # ロック未成立でも直前route/trip情報は温存
        return {
            "locked": False,
            "route_id": prev_trip.get("route_id"),
            "trip_id": prev_trip.get("trip_id"),
            "consecutive": 0,
            "lock_since_unix": None,
            "last_match_unix": prev_lock.get("last_match_unix"),
            "last_nomatch_unix": now_unix,
            "state_label": "recent_context_only",
        }

    return {
        "locked": False,
        "route_id": None,
        "trip_id": None,
        "consecutive": 0,
        "lock_since_unix": None,
        "last_match_unix": prev_lock.get("last_match_unix"),
        "last_nomatch_unix": now_unix,
        "state_label": "none",
    }

# ============================================================
# smoothing
# ============================================================
def _smooth_position(lat, lon, now_dt, prev_state):
    if not prev_state:
        return lat, lon

    prev_ts = prev_state.get("timestamp_unix")
    if not prev_ts:
        return lat, lon

    age = int(now_dt.timestamp()) - int(prev_ts)
    if age < 0 or age > SMOOTHING_LOOKBACK_SEC:
        return lat, lon

    prev_lat = prev_state.get("lat")
    prev_lon = prev_state.get("lon")
    if prev_lat is None or prev_lon is None:
        return lat, lon

    # 80:20 current-prior smoothing
    return (0.8 * lat + 0.2 * float(prev_lat), 0.8 * lon + 0.2 * float(prev_lon))

# ============================================================
# candidate analysis / trip match
# ============================================================
def _evaluate_candidate(trip_id, lat, lon, now_dt, prev_state):
    trip = TRIPS[trip_id]
    prev_trip = _get_prev_trip(prev_state)
    route_lock = _get_route_lock(prev_state)
    now_sec = _seconds_since_midnight(now_dt)

    anchor = None
    if prev_trip and prev_trip.get("trip_id") == trip_id:
        anchor = prev_trip.get("current_distance_m")
    elif route_lock.get("locked") and route_lock.get("route_id") == trip.get("route_id") and prev_trip:
        anchor = prev_trip.get("current_distance_m")

    proj = _project_to_trip_distance(trip_id, lat, lon, anchor_distance_m=anchor)
    if proj is None:
        return {
            "trip_id": trip_id,
            "route_id": trip.get("route_id"),
            "shape_id": trip.get("shape_id"),
            "reject_reason": "projection_failed",
            "score": 999999,
        }

    current_distance_m = float(proj["distance_m"])
    expected_distance_m = float(time_to_distance(trip_id, now_sec))
    distance_error_m = abs(current_distance_m - expected_distance_m)

    nearest_stop_id, nearest_stop_seq, nearest_stop_name, nearest_stop_dist = _nearest_progress_stop(
        trip_id, current_distance_m
    )

    vehicle_stop_status = "IN_TRANSIT_TO"
    if nearest_stop_dist is not None and abs(nearest_stop_dist - current_distance_m) <= STOP_NEAR_GEO_THRESHOLD_M:
        vehicle_stop_status = "STOPPED_AT"

    score = distance_error_m
    penalties = 0
    reject_reason = None
    delta_from_prev_m = None

    # basic reject
    if proj["offset_m"] > MAX_SHAPE_OFFSET_M:
        reject_reason = f"offset_too_large (>{MAX_SHAPE_OFFSET_M}m)"

    if distance_error_m > MAX_MATCH_ERROR_M:
        reject_reason = f"distance_error_too_large (>{MAX_MATCH_ERROR_M}m)"

    # continuity / prev trip
    if prev_trip and prev_trip.get("trip_id"):
        prev_dist = prev_trip.get("current_distance_m")
        prev_route = prev_trip.get("route_id")
        prev_trip_id = prev_trip.get("trip_id")

        if prev_dist is not None:
            delta_from_prev_m = round(current_distance_m - float(prev_dist), 1)

        if trip_id == prev_trip_id:
            penalties -= SAME_TRIP_BONUS
        elif trip.get("route_id") == prev_route:
            penalties -= SAME_ROUTE_BONUS
        else:
            penalties += ROUTE_SWITCH_PENALTY

        if delta_from_prev_m is not None:
            if delta_from_prev_m < -REVERSE_REJECT_M:
                reject_reason = f"reverse_too_large (<-{REVERSE_REJECT_M}m)"
            elif abs(delta_from_prev_m) > MAX_JUMP_DISTANCE_M and trip_id == prev_trip_id:
                reject_reason = f"jump_too_large (>{MAX_JUMP_DISTANCE_M}m)"

    # route lock
    if route_lock.get("locked"):
        lock_route = route_lock.get("route_id")
        lock_trip = route_lock.get("trip_id")
        if trip.get("route_id") == lock_route:
            penalties -= ROUTE_LOCK_BONUS
            if trip_id == lock_trip:
                penalties -= SAME_TRIP_BONUS
        else:
            penalties += OTHER_ROUTE_PENALTY_WHEN_LOCKED

    score = round(score + penalties, 1)

    # delay estimation
    expected_time_sec = distance_to_time(trip_id, current_distance_m)
    delay_sec = int(now_sec - expected_time_sec)

    return {
        "trip_id": trip_id,
        "route_id": trip.get("route_id"),
        "route_name": _route_name(trip.get("route_id")),
        "shape_id": trip.get("shape_id"),
        "current_distance_m": round(current_distance_m, 1),
        "expected_distance_m": round(expected_distance_m, 1),
        "distance_error_m": round(distance_error_m, 1),
        "projection_offset_m": round(proj["offset_m"], 1),
        "abs_distance_m": round(proj["abs_distance_m"], 1),
        "expected_time_sec": int(expected_time_sec),
        "delay_sec": int(delay_sec),
        "nearest_stop_id": nearest_stop_id,
        "nearest_stop_name": nearest_stop_name,
        "current_stop_sequence": nearest_stop_seq,
        "vehicle_stop_status": vehicle_stop_status,
        "score": score,
        "delta_from_prev_m": delta_from_prev_m,
        "reject_reason": reject_reason,
        "penalties": penalties,
    }


def _analyze_trip_candidates(lat, lon, now_dt, prev_state=None):
    lat2, lon2 = _smooth_position(lat, lon, now_dt, prev_state)
    cand_rows = _candidate_trips(now_dt)

    if not cand_rows:
        return {
            "best_match": None,
            "summary_reason": "no_candidate_trips_for_current_time",
            "candidate_count": 0,
            "top_candidate": None,
            "candidates": [],
        }

    results = []
    for row in cand_rows:
        try:
            results.append(_evaluate_candidate(row["trip_id"], lat2, lon2, now_dt, prev_state))
        except Exception as e:
            logger.error("candidate eval error trip=%s err=%s", row["trip_id"], e, exc_info=True)
            results.append({
                "trip_id": row["trip_id"],
                "route_id": row["route_id"],
                "shape_id": row["shape_id"],
                "reject_reason": f"candidate_exception: {e}",
                "score": 999999,
            })

    results.sort(key=lambda x: (x.get("score", 999999), x.get("distance_error_m", 999999), x.get("trip_id", "")))

    accepted = [r for r in results if not r.get("reject_reason")]
    best_match = accepted[0] if accepted else None
    top_candidate = results[0] if results else None

    if best_match:
        summary_reason = "matched"
    elif top_candidate:
        summary_reason = top_candidate.get("reject_reason") or "candidate_exists_but_shape_match_failed"
    else:
        summary_reason = "candidate_exists_but_shape_match_failed"

    return {
        "best_match": best_match,
        "summary_reason": summary_reason,
        "candidate_count": len(results),
        "top_candidate": top_candidate,
        "candidates": results[:CANDIDATE_STORE_LIMIT],
    }


def match_trip(lat, lon, now_dt, prev_state=None):
    analyzed = _analyze_trip_candidates(lat, lon, now_dt, prev_state=prev_state)
    return analyzed["best_match"]


def explain_trip_match_failure(lat, lon, now_dt, prev_state=None):
    analyzed = _analyze_trip_candidates(lat, lon, now_dt, prev_state=prev_state)
    return {
        "summary_reason": analyzed["summary_reason"],
        "candidate_count": analyzed["candidate_count"],
        "top_candidate": analyzed["top_candidate"],
        "candidates": analyzed["candidates"],
    }

# ============================================================
# GTFS-RT builders
# ============================================================
def build_stop_time_updates(trip_id, delay_sec, current_distance_m=None, nearest_stop_sequence=None):
    out = []
    for st in TRIP_STOP_TIMES.get(trip_id, []):
        seq = st["stop_sequence"]
        stop_shape_d = STOP_TO_SHAPE_DIST.get((trip_id, seq))

        # すでに通過済みの停留所は出さない
        if current_distance_m is not None and stop_shape_d is not None:
            if stop_shape_d < current_distance_m - 30:
                continue

        stu = gtfs_realtime_pb2.TripUpdate.StopTimeUpdate()
        stu.stop_sequence = seq
        stu.stop_id = st["stop_id"]
        stu.arrival.delay = int(delay_sec)
        stu.departure.delay = int(delay_sec)
        out.append(stu)
    return out


def build_gtfs_rt_feed(lat, lon, timestamp_unix, trip_match, vehicle_id=VEHICLE_ID):
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.incrementality = gtfs_realtime_pb2.FeedHeader.FULL_DATASET
    feed.header.timestamp = int(timestamp_unix)

    ent = feed.entity.add()
    ent.id = f"vp_{vehicle_id}"
    ent.vehicle.vehicle.id = vehicle_id
    ent.vehicle.position.latitude = float(lat)
    ent.vehicle.position.longitude = float(lon)
    ent.vehicle.timestamp = int(timestamp_unix)

    if trip_match:
        ent.vehicle.trip.trip_id = trip_match["trip_id"]
        ent.vehicle.trip.route_id = trip_match["route_id"]
        if trip_match.get("current_stop_sequence") is not None:
            ent.vehicle.current_stop_sequence = int(trip_match["current_stop_sequence"])

        ent.vehicle.current_status = (
            gtfs_realtime_pb2.VehiclePosition.STOPPED_AT
            if trip_match.get("vehicle_stop_status") == "STOPPED_AT"
            else gtfs_realtime_pb2.VehiclePosition.IN_TRANSIT_TO
        )

        stus = build_stop_time_updates(
            trip_match["trip_id"],
            int(trip_match.get("delay_sec", 0)),
            trip_match.get("current_distance_m"),
            trip_match.get("current_stop_sequence"),
        )

        if stus:
            ent2 = feed.entity.add()
            ent2.id = f"tu_{vehicle_id}"
            tu = ent2.trip_update
            tu.trip.trip_id = trip_match["trip_id"]
            tu.trip.route_id = trip_match["route_id"]
            tu.vehicle.id = vehicle_id
            tu.timestamp = int(timestamp_unix)
            tu.stop_time_update.extend(stus)

    return feed.SerializeToString()

# ============================================================
# validation
# ============================================================
def _validate_api_key(req):
    if not API_KEY:
        return True
    return req.headers.get("X-API-KEY") == API_KEY


def _validate_gps(data):
    lat = data.get("lat")
    lon = data.get("lon")

    if lat is None or lon is None:
        return False, "missing_lat_lon"

    try:
        lat = float(lat)
        lon = float(lon)
    except Exception:
        return False, "invalid_lat_lon_type"

    if not (LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX):
        return False, "out_of_service_area"

    acc = data.get("accuracy")
    if acc is not None:
        try:
            acc = float(acc)
            if acc > 100:
                return False, "accuracy_too_low"
        except Exception:
            pass

    return True, None

# ============================================================
# persistence
# ============================================================
def _thin_trip_match(tm):
    if not tm:
        return None
    keys = [
        "trip_id", "route_id", "route_name", "shape_id",
        "current_distance_m", "expected_distance_m", "distance_error_m",
        "projection_offset_m", "abs_distance_m",
        "expected_time_sec", "delay_sec",
        "nearest_stop_id", "nearest_stop_name",
        "current_stop_sequence", "vehicle_stop_status",
        "score", "delta_from_prev_m",
    ]
    return {k: _safe_firestore_value(tm.get(k)) for k in keys if k in tm}


def _thin_top_candidate(c):
    if not c:
        return None
    keys = [
        "trip_id", "route_id", "route_name", "shape_id",
        "current_distance_m", "expected_distance_m", "distance_error_m",
        "projection_offset_m", "score", "reject_reason",
        "delta_from_prev_m", "nearest_stop_id", "nearest_stop_name",
        "delay_sec",
    ]
    return {k: _safe_firestore_value(c.get(k)) for k in keys if k in c}


def _thin_trip_debug(diag):
    if not diag:
        return None
    return {
        "reason": diag.get("summary_reason"),
        "candidate_count": diag.get("candidate_count"),
        "top_candidate": _thin_top_candidate(diag.get("top_candidate")),
        "candidates": [_safe_firestore_value(c) for c in diag.get("candidates", [])[:CANDIDATE_STORE_LIMIT]],
    }


def _previous_context(prev_state):
    prev_trip = _get_prev_trip(prev_state) or {}
    prev_lock = _get_route_lock(prev_state)
    return _safe_firestore_value({
        "prev_trip_id": prev_trip.get("trip_id"),
        "prev_route_id": prev_trip.get("route_id"),
        "prev_shape_id": prev_trip.get("shape_id"),
        "prev_current_distance_m": prev_trip.get("current_distance_m"),
        "prev_timestamp_unix": prev_state.get("timestamp_unix") if prev_state else None,
        "route_lock": prev_lock,
        "last_event_id": prev_state.get("last_event_id") if prev_state else None,
    })


def _persist_gtfs_rt_audit(vehicle_id, state, trip_match, pb):
    if not ENABLE_GTFS_RT_AUDIT:
        return
    ts_unix = int(state.get("timestamp_unix", int(time.time())))
    snapshot_id = f"{ts_unix}_{vehicle_id}"
    doc = {
        "vehicle_id": vehicle_id,
        "timestamp_unix": ts_unix,
        "server_created_at": _firestore_mod.SERVER_TIMESTAMP,
        "expire_at": _expire_at(),
        "feed_size_bytes": len(pb),
        "trip_id": trip_match.get("trip_id") if trip_match else None,
        "route_id": trip_match.get("route_id") if trip_match else None,
        "lat": state.get("lat"),
        "lon": state.get("lon"),
        "has_trip_update": bool(trip_match and trip_match.get("trip_id")),
        "feed_b64": base64.b64encode(pb).decode("ascii"),
    }
    db.document(f"vehicles/{vehicle_id}/gtfs_rt_audit/{snapshot_id}").set(_safe_firestore_value(doc))


def _persist_observation(
    *,
    vehicle_id,
    payload,
    observed_dt,
    accepted,
    reject_reason,
    prev_state,
    route_candidates,
    trip_match,
    trip_diag,
    route_lock,
):
    ts_unix = int(observed_dt.timestamp())
    lat = payload.get("lat")
    lon = payload.get("lon")
    event_id = _make_event_id(vehicle_id, ts_unix, lat, lon)

    gps_block = {
        "lat": float(lat) if lat is not None else None,
        "lon": float(lon) if lon is not None else None,
        "accuracy": _safe_firestore_value(payload.get("accuracy")),
        "speed": _safe_firestore_value(payload.get("speed")),
        "heading": _safe_firestore_value(payload.get("heading")),
    }

    route_guess = route_candidates[0] if route_candidates else None
    thin_trip = _thin_trip_match(trip_match)
    thin_debug = _thin_trip_debug(trip_diag)

    event_ref = db.document(f"vehicles/{vehicle_id}/events/{event_id}")
    latest_ref = db.document(f"vehicles/{vehicle_id}/state/latest")

    batch = db.batch()

    # gps_logs 互換
    log_doc = {
        "event_id": event_id,
        "vehicle_id": vehicle_id,
        "timestamp": payload.get("timestamp"),
        "timestamp_unix": ts_unix,
        "server_received_at": observed_dt.isoformat(),
        "created_at": _firestore_mod.SERVER_TIMESTAMP,
        "expire_at": _expire_at(),
        "accepted": accepted,
        "reject_reason": reject_reason,
        "lat": gps_block["lat"],
        "lon": gps_block["lon"],
        "accuracy": gps_block["accuracy"],
        "speed": gps_block["speed"],
        "heading": gps_block["heading"],
        "raw": _safe_firestore_value(payload),
        "route_guess": _safe_firestore_value(route_guess),
        "trip_match": _safe_firestore_value(thin_trip),
        "trip_debug": _safe_firestore_value(thin_debug),
        "route_lock": _safe_firestore_value(route_lock),
    }
    batch.set(db.collection("gps_logs").document(event_id), _safe_firestore_value(log_doc))

    # complete event
    event_doc = {
        "event_id": event_id,
        "vehicle_id": vehicle_id,
        "observed_at": payload.get("timestamp"),
        "observed_unix": ts_unix,
        "server_received_at": observed_dt.isoformat(),
        "created_at": _firestore_mod.SERVER_TIMESTAMP,
        "expire_at": _expire_at(),
        "accepted": accepted,
        "reject_reason": reject_reason,
        "gps": gps_block,
        "raw": _safe_firestore_value(payload),
        "route_guess": _safe_firestore_value(route_guess),
        "route_candidates": _safe_firestore_value(route_candidates),
        "trip_match": _safe_firestore_value(thin_trip),
        "trip_debug": _safe_firestore_value(thin_debug),
        "route_lock": _safe_firestore_value(route_lock),
        "previous_context": _previous_context(prev_state),
        "system": {
            "algorithm_version": ALGORITHM_VERSION,
            "precomputed_version": PRECOMPUTED_VERSION,
            "thresholds": {
                "TRIP_TIME_BUFFER_SEC": TRIP_TIME_BUFFER_SEC,
                "MAX_MATCH_ERROR_M": MAX_MATCH_ERROR_M,
                "MAX_SHAPE_OFFSET_M": MAX_SHAPE_OFFSET_M,
                "MAX_JUMP_DISTANCE_M": MAX_JUMP_DISTANCE_M,
                "REVERSE_REJECT_M": REVERSE_REJECT_M,
                "STOP_NEAR_GEO_THRESHOLD_M": STOP_NEAR_GEO_THRESHOLD_M,
                "SMOOTHING_LOOKBACK_SEC": SMOOTHING_LOOKBACK_SEC,
                "LOCK_ESTABLISH_COUNT": LOCK_ESTABLISH_COUNT,
                "LOCK_RELEASE_NOMATCH_SEC": LOCK_RELEASE_NOMATCH_SEC,
            },
        },
    }
    batch.set(event_ref, _safe_firestore_value(event_doc))

    # candidate subcollection
    for idx, cand in enumerate((trip_diag or {}).get("candidates", [])[:CANDIDATE_STORE_LIMIT], start=1):
        candidate_id = f"{idx:02d}_{cand.get('trip_id', 'unknown')}"
        candidate_doc = {
            "rank": idx,
            "event_id": event_id,
            "vehicle_id": vehicle_id,
            "observed_unix": ts_unix,
            "created_at": _firestore_mod.SERVER_TIMESTAMP,
            "expire_at": _expire_at(),
            **_safe_firestore_value(cand),
        }
        batch.set(event_ref.collection("candidates").document(candidate_id), candidate_doc)

    latest_doc = {
        "vehicle_id": vehicle_id,
        "last_event_id": event_id,
        "lat": gps_block["lat"],
        "lon": gps_block["lon"],
        "accuracy": gps_block["accuracy"],
        "speed": gps_block["speed"],
        "heading": gps_block["heading"],
        "timestamp": payload.get("timestamp"),
        "timestamp_unix": ts_unix,
        "server_received_at": observed_dt.isoformat(),
        "updated_at": _firestore_mod.SERVER_TIMESTAMP,
        "accepted": accepted,
        "reject_reason": reject_reason,
        "route_guess": _safe_firestore_value(route_guess),
        "route_candidates": _safe_firestore_value(route_candidates[:3]),
        "trip": _safe_firestore_value(thin_trip),
        "trip_debug": _safe_firestore_value(thin_debug),
        "route_lock": _safe_firestore_value(route_lock),
    }
    batch.set(latest_ref, _safe_firestore_value(latest_doc), merge=True)

    batch.commit()
    return event_id

# ============================================================
# HTTP functions
# ============================================================
@https_fn.on_request()
def gps(req):
    _ensure_init()

    if req.method == "OPTIONS":
        return ("", 204, _cors_headers("application/json"))

    if req.method != "POST":
        return ("Method Not Allowed", 405, _cors_headers("text/plain"))

    if not _validate_api_key(req):
        return ({"ok": False, "error": "unauthorized"}, 401, _cors_headers("application/json"))

    try:
        data = req.get_json(force=True)
        if not isinstance(data, dict):
            raise ValueError("json body is not object")
    except Exception:
        return ({"ok": False, "error": "invalid_json"}, 400, _cors_headers("application/json"))

    observed_dt = _parse_payload_timestamp(data)
    accepted, reject_reason = _validate_gps(data)

    vehicle_id = data.get("vehicle_id", VEHICLE_ID)
    latest_ref = db.document(f"vehicles/{vehicle_id}/state/latest")
    snap = latest_ref.get()
    prev_state = snap.to_dict() if snap.exists else None

    lat = data.get("lat")
    lon = data.get("lon")

    route_candidates = []
    trip_diag = None
    trip_match = None

    if accepted:
        try:
            lat = float(lat)
            lon = float(lon)

            route_candidates = estimate_route_candidates(lat, lon, topn=5)
            trip_diag = _analyze_trip_candidates(lat, lon, observed_dt, prev_state=prev_state)
            trip_match = trip_diag.get("best_match")
        except Exception as e:
            logger.error("gps processing error: %s", e, exc_info=True)
            trip_diag = {
                "summary_reason": "match_trip_exception",
                "candidate_count": 0,
                "top_candidate": None,
                "candidates": [],
            }

    route_lock = _compute_route_lock(prev_state, trip_match, observed_dt)

    event_id = _persist_observation(
        vehicle_id=vehicle_id,
        payload=data,
        observed_dt=observed_dt,
        accepted=accepted,
        reject_reason=reject_reason,
        prev_state=prev_state,
        route_candidates=route_candidates,
        trip_match=trip_match,
        trip_diag=trip_diag,
        route_lock=route_lock,
    )

    resp = {
        "ok": True,

    }

    if trip_match:
        resp["trip"] = {
            "trip_id": trip_match["trip_id"],
            "route_id": trip_match["route_id"],
            "route_name": trip_match["route_name"],
            "nearest_stop": trip_match.get("nearest_stop_name"),
            "status": trip_match.get("vehicle_stop_status"),
            "current_distance_m": trip_match.get("current_distance_m"),
            "expected_distance_m": trip_match.get("expected_distance_m"),
            "distance_error_m": trip_match.get("distance_error_m"),
            "offset_m": trip_match.get("projection_offset_m"),
            "delay_sec": trip_match.get("delay_sec"),
            "delay_min": round(trip_match.get("delay_sec", 0) / 60.0, 1),
            "score": trip_match.get("score"),
        }
    else:
        resp["trip_debug"] = _thin_trip_debug(trip_diag)

    if not accepted:
        resp["reject_reason"] = reject_reason

    return (resp, 200, _cors_headers("application/json"))


@https_fn.on_request()
def gtfs_rt(req):
    _ensure_init()

    if req.method == "OPTIONS":
        return ("", 204, _cors_headers("application/x-protobuf"))

    vehicle_id = VEHICLE_ID
    doc = db.document(f"vehicles/{vehicle_id}/state/latest").get()

    if not doc.exists:
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.header.gtfs_realtime_version = "2.0"
        feed.header.incrementality = gtfs_realtime_pb2.FeedHeader.FULL_DATASET
        feed.header.timestamp = int(time.time())
        pb = feed.SerializeToString()
        return (pb, 200, _cors_headers("application/x-protobuf"))

    state = doc.to_dict()

    trip_match = None
    if state.get("trip") and state["trip"].get("trip_id"):
        trip_match = state["trip"]
    elif state.get("lat") is not None and state.get("lon") is not None:
        try:
            observed_dt = datetime.fromtimestamp(int(state.get("timestamp_unix", int(time.time()))), tz=JST)
            trip_diag = _analyze_trip_candidates(
                float(state["lat"]),
                float(state["lon"]),
                observed_dt,
                prev_state=state,
            )
            trip_match = trip_diag.get("best_match")
        except Exception as e:
            logger.error("gtfs_rt fallback error: %s", e, exc_info=True)

    pb = build_gtfs_rt_feed(
        float(state.get("lat", 0.0)),
        float(state.get("lon", 0.0)),
        int(state.get("timestamp_unix", int(time.time()))),
        trip_match,
        vehicle_id,
    )

    _persist_gtfs_rt_audit(vehicle_id, state, trip_match, pb)
    return (pb, 200, _cors_headers("application/x-protobuf"))
