"""
Module B – GPS受信 / shapeベース便推定 / 遅延判定 / GTFS-RT生成

瑞穂町コミュニティバス バスロケーションシステム PoC

初期化戦略:
  - モジュールレベルには重い処理を一切置かない
  - Firebase公式 @init デコレータで初期化を遅延
  - @init 未対応の旧SDKでは _ensure_init() フォールバック
"""

# ============================================================
# import のみ (I/O なし)
# ============================================================
import os
import csv
import json
import math
import time
import logging
import threading
from datetime import datetime, timedelta, timezone

from firebase_functions import https_fn

# @init デコレータの取得 (なければフォールバック)
try:
    from firebase_functions.core import init as _firebase_init
except ImportError:
    _firebase_init = None

import firebase_admin
from firebase_admin import firestore as _firestore_mod
from google.transit import gtfs_realtime_pb2

# ============================================================
# 定数 (I/O なし — デプロイ検査に影響しない)
# ============================================================
JST = timezone(timedelta(hours=9))
VEHICLE_ID = "mizuho-bus-01"
GTFS_DIR = os.path.join(os.path.dirname(__file__), "data", "gtfs")

TRIP_TIME_BUFFER_SEC = 600
MAX_MATCH_ERROR_M = 1000
MAX_SHAPE_OFFSET_M = 250
MAX_JUMP_DISTANCE_M = 1000
REVERSE_REJECT_M = 150
STOP_NEAR_GEO_THRESHOLD_M = 70
SMOOTHING_LOOKBACK_SEC = 30
EARTH_RADIUS_M = 6_371_000

LAT_MIN, LAT_MAX = 35.74, 35.80
LON_MIN, LON_MAX = 139.32, 139.38

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("busroq")

# ============================================================
# 遅延初期化の状態管理
# ============================================================
_init_done = False
_init_lock = threading.Lock()

# 初期化後に埋まるグローバル変数 (初期値は空)
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


def _do_init():
    """全データの読み込み。@init または初回リクエスト時に1回だけ実行。"""
    global _init_done, db, API_KEY
    global STOPS, ROUTES, TRIPS, TRIP_STOP_TIMES, CALENDAR, CALENDAR_DATES
    global SHAPES, SHAPE_TOTAL_DISTANCE
    global TRIP_PROGRESS, STOP_TO_SHAPE_DIST, TRIP_SHAPE_START_ABS, TRIP_ROUTE_LENGTH

    if _init_done:
        return

    # --- Firebase ---
    firebase_admin.initialize_app()
    db = _firestore_mod.client()
    API_KEY = os.environ.get("API_KEY")

    # --- CSV (軽量) ---
    STOPS.update({
        r["stop_id"]: {"name": r["stop_name"], "lat": float(r["stop_lat"]), "lon": float(r["stop_lon"])}
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

    # --- precomputed.json (shape投影済み) ---
    pc_path = os.path.join(GTFS_DIR, "precomputed.json")
    with open(pc_path, "r", encoding="utf-8") as f:
        pc = json.load(f)

    SHAPES.update({
        sid: [{"lat": p["lat"], "lon": p["lon"], "dist_traveled": p["dist_traveled"]} for p in pts]
        for sid, pts in pc["shapes"].items()
    })
    SHAPE_TOTAL_DISTANCE.update({k: float(v) for k, v in pc["shape_total_distance"].items()})
    TRIP_PROGRESS.update(pc["trip_progress"])
    for key, val in pc["stop_to_shape_dist"].items():
        trip_id, seq_str = key.split("|")
        STOP_TO_SHAPE_DIST[(trip_id, int(seq_str))] = float(val)
    TRIP_SHAPE_START_ABS.update({k: float(v) for k, v in pc["trip_shape_start_abs"].items()})
    TRIP_ROUTE_LENGTH.update({k: float(v) for k, v in pc["trip_route_length"].items()})

    _init_done = True
    logger.info(
        "Init complete: %d stops, %d routes, %d trips, %d shapes",
        len(STOPS), len(ROUTES), len(TRIPS), len(SHAPES),
    )


def _ensure_init():
    """スレッドセーフな遅延初期化 (フォールバック用)。"""
    if _init_done:
        return
    with _init_lock:
        if not _init_done:
            _do_init()


# --- Firebase @init 対応 ---
if _firebase_init is not None:
    @_firebase_init
    def _on_firebase_init():
        _do_init()


# ============================================================
# ユーティリティ
# ============================================================
def _load_csv(filename):
    path = os.path.join(GTFS_DIR, filename)
    with open(path, "r", encoding="utf-8-sig") as f:
        return [
            {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k}
            for row in csv.DictReader(f)
        ]


def _parse_gtfs_time(t):
    h, m, s = t.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def _now_jst():
    return datetime.now(JST)


def _seconds_since_midnight(dt):
    return dt.hour * 3600 + dt.minute * 60 + dt.second


def _haversine(lat1, lon1, lat2, lon2):
    rlat1, rlon1 = math.radians(lat1), math.radians(lon1)
    rlat2, rlon2 = math.radians(lat2), math.radians(lon2)
    dlat = rlat2 - rlat1
    dlon = rlon2 - rlon1
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return EARTH_RADIUS_M * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _latlon_to_xy(lat, lon, ref_lat, ref_lon):
    x = math.radians(lon - ref_lon) * math.cos(math.radians(ref_lat)) * EARTH_RADIUS_M
    y = math.radians(lat - ref_lat) * EARTH_RADIUS_M
    return x, y


# ============================================================
# ランタイム shape投影
# ============================================================
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
        if best is None or proj["offset_m"] < best["offset_m"]:
            best = {**proj, "shape_id": shape_id, "segment_index": i}
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
    candidates = [c for c in [raw_rel, raw_rel + total_shape, raw_rel - total_shape] if c >= -100.0] or [0.0]
    if anchor_distance_m is None:
        distance_m = min((max(0.0, c) for c in candidates), key=abs)
    else:
        distance_m = min((max(0.0, c) for c in candidates), key=lambda x: abs(x - anchor_distance_m))
    return {**proj, "abs_distance_m": round(abs_d, 1), "distance_m": round(distance_m, 1)}


# ============================================================
# 仮想バス補間
# ============================================================
def time_to_distance(trip_id, t):
    prog = TRIP_PROGRESS.get(trip_id, [])
    if not prog:
        return 0.0
    if t <= prog[0]["time_sec"]:
        return prog[0]["distance_m"]
    if t >= prog[-1]["time_sec"]:
        return prog[-1]["distance_m"]
    for i in range(len(prog) - 1):
        p1, p2 = prog[i], prog[i + 1]
        if p1["time_sec"] <= t <= p2["time_sec"]:
            dt = p2["time_sec"] - p1["time_sec"]
            return p1["distance_m"] + (0 if dt == 0 else (t - p1["time_sec"]) / dt * (p2["distance_m"] - p1["distance_m"]))
    return prog[-1]["distance_m"]


def distance_to_time(trip_id, d):
    prog = TRIP_PROGRESS.get(trip_id, [])
    if not prog:
        return 0
    if d <= prog[0]["distance_m"]:
        return prog[0]["time_sec"]
    if d >= prog[-1]["distance_m"]:
        return prog[-1]["time_sec"]
    for i in range(len(prog) - 1):
        p1, p2 = prog[i], prog[i + 1]
        dd = p2["distance_m"] - p1["distance_m"]
        if p1["distance_m"] <= d <= p2["distance_m"]:
            return p2["time_sec"] if abs(dd) < 1e-6 else int(round(p1["time_sec"] + (d - p1["distance_m"]) / dd * (p2["time_sec"] - p1["time_sec"])))
    return prog[-1]["time_sec"]


# ============================================================
# サービスID / 候補便
# ============================================================
_DOW_MAP = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def resolve_active_service_ids(target_date):
    d = target_date.date() if isinstance(target_date, datetime) else target_date
    date_str = d.strftime("%Y%m%d")
    dow_idx = d.weekday()
    active = []
    for sid, cal in CALENDAR.items():
        if date_str < cal["start_date"] or date_str > cal["end_date"]:
            continue
        exc = CALENDAR_DATES.get((sid, date_str))
        if exc == 2:
            continue
        if exc == 1:
            active.append(sid); continue
        if cal[_DOW_MAP[dow_idx]] == "1":
            active.append(sid)
    return active


def _candidate_trips(now_dt):
    active_sids = resolve_active_service_ids(now_dt)
    if not active_sids:
        return []
    now_sec = _seconds_since_midnight(now_dt)
    return [
        tid for tid, trip in TRIPS.items()
        if trip["service_id"] in active_sids
        and TRIP_STOP_TIMES.get(tid)
        and (TRIP_STOP_TIMES[tid][0]["departure_sec"] - TRIP_TIME_BUFFER_SEC) <= now_sec <= (TRIP_STOP_TIMES[tid][-1]["arrival_sec"] + TRIP_TIME_BUFFER_SEC)
    ]


# ============================================================
# 便推定
# ============================================================
def _smooth_position(lat, lon, prev_state, now_dt):
    if not prev_state:
        return lat, lon
    plat, plon, pts = prev_state.get("lat"), prev_state.get("lon"), prev_state.get("timestamp_unix")
    if plat is None or plon is None or pts is None:
        return lat, lon
    age = int(now_dt.timestamp()) - int(pts)
    if age < 0 or age > SMOOTHING_LOOKBACK_SEC:
        return lat, lon
    return lat * 0.8 + float(plat) * 0.2, lon * 0.8 + float(plon) * 0.2


def _nearest_progress_stop(trip_id, dist):
    prog = TRIP_PROGRESS.get(trip_id, [])
    return min(prog, key=lambda p: abs(p["distance_m"] - dist)) if prog else None


def match_trip(lat, lon, now_dt, prev_state=None):
    _ensure_init()
    candidates = _candidate_trips(now_dt)
    if not candidates:
        return None
    now_sec = _seconds_since_midnight(now_dt)
    slat, slon = _smooth_position(lat, lon, prev_state, now_dt)
    pt = (prev_state or {}).get("trip") or {}
    ptid, pdist = pt.get("trip_id"), pt.get("current_distance_m")
    pts = (prev_state or {}).get("timestamp_unix")
    fresh = pts is not None and 0 <= (int(now_dt.timestamp()) - int(pts)) <= 180

    best, best_score = None, float("inf")
    for tid in candidates:
        exp_d = time_to_distance(tid, now_sec)
        anchor = (exp_d + float(pdist)) / 2.0 if (fresh and ptid == tid and pdist is not None) else exp_d
        proj = _project_to_trip_distance(tid, slat, slon, anchor_distance_m=anchor)
        if proj is None or proj["offset_m"] > MAX_SHAPE_OFFSET_M:
            continue
        cur_d = proj["distance_m"]
        err = abs(exp_d - cur_d)
        dfp = None
        penalty = 0.0
        if fresh and pdist is not None:
            try:
                dfp = cur_d - float(pdist)
            except Exception:
                pass
        if ptid == tid and dfp is not None:
            if abs(dfp) > MAX_JUMP_DISTANCE_M or dfp < -REVERSE_REJECT_M:
                continue
            penalty = 250.0 if dfp < -20 else -80.0
        elif ptid and ptid in candidates:
            penalty = 120.0
        score = err + penalty
        if score < best_score:
            ns = _nearest_progress_stop(tid, cur_d)
            if not ns:
                continue
            sg = STOPS.get(ns["stop_id"])
            sgd = _haversine(slat, slon, sg["lat"], sg["lon"]) if sg else None
            status = "STOPPED_AT" if (sgd is not None and sgd <= STOP_NEAR_GEO_THRESHOLD_M) else "IN_TRANSIT_TO"
            best_score = score
            best = {
                "trip_id": tid, "route_id": TRIPS[tid]["route_id"],
                "route_name": ROUTES.get(TRIPS[tid]["route_id"], {}).get("route_long_name", ""),
                "shape_id": TRIPS[tid]["shape_id"],
                "current_distance_m": round(cur_d, 1), "expected_distance_m": round(exp_d, 1),
                "distance_error_m": round(err, 1), "projection_offset_m": round(proj["offset_m"], 1),
                "expected_time_sec": int(distance_to_time(tid, cur_d)),
                "delay_sec": int(now_sec - distance_to_time(tid, cur_d)),
                "nearest_stop_id": ns["stop_id"],
                "nearest_stop_name": STOPS.get(ns["stop_id"], {}).get("name", ""),
                "current_stop_sequence": ns["stop_sequence"],
                "vehicle_stop_status": status, "score": round(score, 1),
                "delta_from_prev_m": round(dfp, 1) if dfp is not None else None,
                "smoothed_lat": slat, "smoothed_lon": slon,
            }
    return best if best and best["distance_error_m"] <= MAX_MATCH_ERROR_M else None


# ============================================================
# stop_time_update
# ============================================================
def build_stop_time_updates(trip_id, delay_sec, current_distance_m=None, now_sec=None):
    return [
        {"stop_id": st["stop_id"], "stop_sequence": st["stop_sequence"],
         "arrival_delay_sec": int(delay_sec), "departure_delay_sec": int(delay_sec)}
        for st in TRIP_STOP_TIMES.get(trip_id, [])
        if not (current_distance_m is not None and STOP_TO_SHAPE_DIST.get((trip_id, st["stop_sequence"]), float("inf")) < current_distance_m - 30)
        and not (current_distance_m is None and now_sec is not None and st["arrival_sec"] + delay_sec < now_sec - 60)
    ]


# ============================================================
# GTFS-RT feed
# ============================================================
def build_gtfs_rt_feed(lat, lon, timestamp_unix, trip_match, vehicle_id=VEHICLE_ID):
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.incrementality = gtfs_realtime_pb2.FeedHeader.FULL_DATASET
    feed.header.timestamp = timestamp_unix
    vp_ent = feed.entity.add()
    vp_ent.id = f"vp_{vehicle_id}"
    vp = vp_ent.vehicle
    vp.vehicle.id = vehicle_id
    vp.vehicle.label = vehicle_id
    vp.position.latitude = lat
    vp.position.longitude = lon
    vp.timestamp = timestamp_unix
    if trip_match:
        vp.trip.trip_id = trip_match["trip_id"]
        vp.trip.route_id = trip_match["route_id"]
        vp.current_stop_sequence = trip_match["current_stop_sequence"]
        vp.current_status = (gtfs_realtime_pb2.VehiclePosition.STOPPED_AT
                             if trip_match["vehicle_stop_status"] == "STOPPED_AT"
                             else gtfs_realtime_pb2.VehiclePosition.IN_TRANSIT_TO)
        ns = _seconds_since_midnight(datetime.fromtimestamp(timestamp_unix, tz=JST))
        stus = build_stop_time_updates(trip_match["trip_id"], trip_match["delay_sec"],
                                       trip_match.get("current_distance_m"), ns)
        if stus:
            tu_ent = feed.entity.add()
            tu_ent.id = f"tu_{vehicle_id}"
            tu = tu_ent.trip_update
            tu.trip.trip_id = trip_match["trip_id"]
            tu.trip.route_id = trip_match["route_id"]
            tu.vehicle.id = vehicle_id
            tu.timestamp = timestamp_unix
            for u in stus:
                s = tu.stop_time_update.add()
                s.stop_sequence = u["stop_sequence"]
                s.stop_id = u["stop_id"]
                s.arrival.delay = u["arrival_delay_sec"]
                s.departure.delay = u["departure_delay_sec"]
    return feed.SerializeToString()


# ============================================================
# GPS検証
# ============================================================
def _validate_gps(data):
    lat, lon = data.get("lat"), data.get("lon")
    if lat is None or lon is None:
        return False, "missing_lat_lon"
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False, "invalid_lat_lon_type"
    if not (LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX):
        return False, "out_of_service_area"
    acc = data.get("accuracy")
    if acc is not None:
        try:
            if float(acc) > 200: return False, "accuracy_too_low"
        except (TypeError, ValueError):
            pass
    return True, ""


# ============================================================
# Cloud Functions (モジュールレベルに重い処理なし)
# ============================================================
@https_fn.on_request()
def gps(req):
    _ensure_init()

    if req.headers.get("X-API-KEY") != API_KEY:
        return ("Unauthorized", 401)
    if req.method != "POST":
        return ("Method Not Allowed", 405)
    try:
        data = req.get_json(force=True)
    except Exception:
        return ({"ok": False, "error": "invalid_json"}, 400)

    now = _now_jst()
    ts_unix = int(now.timestamp())
    srv_at = now.isoformat()
    accepted, reason = _validate_gps(data)
    vid = data.get("vehicle_id", VEHICLE_ID)
    lat, lon = data.get("lat"), data.get("lon")

    latest_ref = db.document(f"vehicles/{vid}/state/latest")
    snap = latest_ref.get()
    prev = snap.to_dict() if snap.exists else None

    tm = None
    if accepted:
        try:
            tm = match_trip(float(lat), float(lon), now, prev_state=prev)
        except Exception as e:
            logger.error("Trip match error: %s", e, exc_info=True)

    log_doc = {
        "vehicle_id": vid, "lat": float(lat) if lat else None, "lon": float(lon) if lon else None,
        "accuracy": data.get("accuracy"), "speed": data.get("speed"), "heading": data.get("heading"),
        "timestamp": data.get("timestamp"), "timestamp_unix": ts_unix,
        "server_received_at": srv_at, "accepted": accepted, "reject_reason": reason, "raw": data,
    }
    if tm:
        log_doc["trip_match"] = {k: tm[k] for k in
            ["trip_id", "route_id", "shape_id", "current_distance_m", "expected_distance_m",
             "distance_error_m", "projection_offset_m", "delay_sec", "current_stop_sequence",
             "nearest_stop_id", "score", "delta_from_prev_m"]}
    db.collection("gps_logs").add(log_doc)

    if accepted:
        latest = {
            "vehicle_id": vid, "lat": float(lat), "lon": float(lon),
            "accuracy": data.get("accuracy"), "speed": data.get("speed"), "heading": data.get("heading"),
            "timestamp": data.get("timestamp"), "timestamp_unix": ts_unix,
            "server_received_at": srv_at, "updated_at": _firestore_mod.SERVER_TIMESTAMP,
            "trip": None,
        }
        if tm:
            latest["trip"] = {k: tm[k] for k in
                ["trip_id", "route_id", "route_name", "shape_id",
                 "current_distance_m", "expected_distance_m", "distance_error_m", "projection_offset_m",
                 "expected_time_sec", "delay_sec", "nearest_stop_id", "nearest_stop_name",
                 "current_stop_sequence", "vehicle_stop_status", "score", "delta_from_prev_m"]}
        latest_ref.set(latest)

    resp = {"ok": True, "accepted": accepted}
    if tm:
        resp["trip"] = {
            "trip_id": tm["trip_id"], "route_id": tm["route_id"], "route_name": tm["route_name"],
            "delay_min": round(tm["delay_sec"] / 60.0, 1), "nearest_stop": tm["nearest_stop_name"],
            "status": tm["vehicle_stop_status"],
            "current_distance_m": tm["current_distance_m"], "distance_error_m": tm["distance_error_m"],
        }
    if not accepted:
        resp["reject_reason"] = reason
    return (resp, 200)


@https_fn.on_request()
def gtfs_rt(req):
    _ensure_init()

    doc = db.document(f"vehicles/{VEHICLE_ID}/state/latest").get()
    if not doc.exists:
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.header.gtfs_realtime_version = "2.0"
        feed.header.incrementality = gtfs_realtime_pb2.FeedHeader.FULL_DATASET
        feed.header.timestamp = int(time.time())
        return (feed.SerializeToString(), 200, {"Content-Type": "application/x-protobuf"})

    state = doc.to_dict()
    ti = state.get("trip")
    tm = ti if ti and ti.get("trip_id") else None

    if tm is None and state.get("lat") is not None:
        try:
            ts = int(state.get("timestamp_unix", int(time.time())))
            tm = match_trip(float(state["lat"]), float(state["lon"]),
                            datetime.fromtimestamp(ts, tz=JST), prev_state=state)
        except Exception as e:
            logger.error("gtfs_rt fallback: %s", e, exc_info=True)

    pb = build_gtfs_rt_feed(
        float(state.get("lat", 0.0)), float(state.get("lon", 0.0)),
        int(state.get("timestamp_unix", int(time.time()))), tm, VEHICLE_ID,
    )
    return (pb, 200, {"Content-Type": "application/x-protobuf"})
