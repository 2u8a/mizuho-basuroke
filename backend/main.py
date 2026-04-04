"""
Module B – GPS受信 / shapeベース便推定 / 遅延判定 / GTFS-RT生成

瑞穂町コミュニティバス バスロケーションシステム PoC
"""

import os
import csv
import math
import time
import logging
from datetime import datetime, timedelta, timezone

import firebase_admin
from firebase_admin import firestore
from firebase_functions import https_fn
from google.transit import gtfs_realtime_pb2

# ============================================================
# 定数
# ============================================================
JST = timezone(timedelta(hours=9))
VEHICLE_ID = "mizuho-bus-01"
GTFS_DIR = os.path.join(os.path.dirname(__file__), "data", "gtfs")

TRIP_TIME_BUFFER_SEC = 600      # 候補便の前後10分
MAX_MATCH_ERROR_M = 1000        # 期待距離との差が1km超なら不採用
MAX_SHAPE_OFFSET_M = 250        # shapeからの横ずれ閾値
MAX_JUMP_DISTANCE_M = 1000      # 前回距離との差1km超はreject
REVERSE_REJECT_M = 150          # 150m超の逆行はreject
STOP_NEAR_GEO_THRESHOLD_M = 70  # 停留所70m以内ならSTOPPED_AT
SMOOTHING_LOOKBACK_SEC = 30     # 直近30秒なら位置平滑化
EARTH_RADIUS_M = 6_371_000

LAT_MIN, LAT_MAX = 35.74, 35.80
LON_MIN, LON_MAX = 139.32, 139.38

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("busroq")

# ============================================================
# Firebase 初期化
# ============================================================
firebase_admin.initialize_app()
db = firestore.client()
API_KEY = os.environ.get("API_KEY")

# ============================================================
# 基本ユーティリティ
# ============================================================
def _load_csv(filename: str) -> list[dict]:
    path = os.path.join(GTFS_DIR, filename)
    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = []
        for row in reader:
            cleaned = {}
            for k, v in row.items():
                if k is None:
                    continue
                key = k.strip()
                cleaned[key] = v.strip() if isinstance(v, str) else v
            rows.append(cleaned)
        return rows


def _parse_gtfs_time(t: str) -> int:
    h, m, s = t.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def _now_jst() -> datetime:
    return datetime.now(JST)


def _seconds_since_midnight(dt: datetime) -> int:
    return dt.hour * 3600 + dt.minute * 60 + dt.second


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    rlat1, rlon1 = math.radians(lat1), math.radians(lon1)
    rlat2, rlon2 = math.radians(lat2), math.radians(lon2)
    dlat = rlat2 - rlat1
    dlon = rlon2 - rlon1
    a = math.sin(dlat / 2) ** 2 + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    return EARTH_RADIUS_M * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _latlon_to_xy(lat: float, lon: float, ref_lat: float, ref_lon: float) -> tuple[float, float]:
    x = math.radians(lon - ref_lon) * math.cos(math.radians(ref_lat)) * EARTH_RADIUS_M
    y = math.radians(lat - ref_lat) * EARTH_RADIUS_M
    return x, y


# ============================================================
# GTFS 静的データ読み込み
# ============================================================
_stops_raw = _load_csv("stops.txt")
STOPS = {
    r["stop_id"]: {
        "name": r["stop_name"],
        "lat": float(r["stop_lat"]),
        "lon": float(r["stop_lon"]),
    }
    for r in _stops_raw
}

_routes_raw = _load_csv("routes.txt")
ROUTES = {r["route_id"]: r for r in _routes_raw}

_trips_raw = _load_csv("trips.txt")
TRIPS = {r["trip_id"]: r for r in _trips_raw}

_stop_times_raw = _load_csv("stop_times.txt")
TRIP_STOP_TIMES: dict[str, list[dict]] = {}
for r in _stop_times_raw:
    tid = r["trip_id"]
    TRIP_STOP_TIMES.setdefault(tid, []).append({
        "stop_id": r["stop_id"],
        "arrival_sec": _parse_gtfs_time(r["arrival_time"]),
        "departure_sec": _parse_gtfs_time(r["departure_time"]),
        "stop_sequence": int(r["stop_sequence"]),
        "arrival_time_str": r["arrival_time"],
        "departure_time_str": r["departure_time"],
    })
for tid in TRIP_STOP_TIMES:
    TRIP_STOP_TIMES[tid].sort(key=lambda x: x["stop_sequence"])

_calendar_raw = _load_csv("calendar.txt")
CALENDAR = {r["service_id"]: r for r in _calendar_raw}

_calendar_dates_raw = _load_csv("calendar_dates.txt")
CALENDAR_DATES: dict[tuple[str, str], int] = {}
for r in _calendar_dates_raw:
    CALENDAR_DATES[(r["service_id"], r["date"])] = int(r["exception_type"])

# ============================================================
# 1. shape 前処理
# ============================================================
_shapes_raw = _load_csv("shapes.txt")
SHAPES: dict[str, list[dict]] = {}
for r in _shapes_raw:
    sid = r["shape_id"]
    SHAPES.setdefault(sid, []).append({
        "lat": float(r["shape_pt_lat"]),
        "lon": float(r["shape_pt_lon"]),
        "seq": int(r["shape_pt_sequence"]),
    })

for sid, pts in SHAPES.items():
    pts.sort(key=lambda x: x["seq"])
    cum = 0.0
    pts[0]["dist_traveled"] = 0.0
    for i in range(1, len(pts)):
        cum += _haversine(pts[i-1]["lat"], pts[i-1]["lon"], pts[i]["lat"], pts[i]["lon"])
        pts[i]["dist_traveled"] = cum

SHAPE_TOTAL_DISTANCE = {sid: (pts[-1]["dist_traveled"] if pts else 0.0) for sid, pts in SHAPES.items()}


# ============================================================
# 2. shape への投影
# ============================================================
def _project_point_to_segment(lat: float, lon: float, a: dict, b: dict) -> dict:
    ref_lat = (a["lat"] + b["lat"]) / 2.0
    ref_lon = (a["lon"] + b["lon"]) / 2.0
    px, py = _latlon_to_xy(lat, lon, ref_lat, ref_lon)
    ax, ay = _latlon_to_xy(a["lat"], a["lon"], ref_lat, ref_lon)
    bx, by = _latlon_to_xy(b["lat"], b["lon"], ref_lat, ref_lon)

    dx = bx - ax
    dy = by - ay
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq < 1e-9:
        t = 0.0
        qx, qy = ax, ay
    else:
        t = ((px - ax) * dx + (py - ay) * dy) / seg_len_sq
        t = max(0.0, min(1.0, t))
        qx = ax + t * dx
        qy = ay + t * dy

    offset_m = math.hypot(px - qx, py - qy)
    seg_start = a["dist_traveled"]
    seg_end = b["dist_traveled"]
    distance_m = seg_start + t * (seg_end - seg_start)

    return {
        "distance_m": distance_m,
        "offset_m": offset_m,
        "t": t,
    }


def project_to_shape(shape_id: str, lat: float, lon: float) -> dict | None:
    pts = SHAPES.get(shape_id)
    if not pts or len(pts) < 2:
        return None

    best = None
    for i in range(len(pts) - 1):
        proj = _project_point_to_segment(lat, lon, pts[i], pts[i+1])
        if best is None or proj["offset_m"] < best["offset_m"]:
            best = {**proj, "shape_id": shape_id, "segment_index": i}
    return best


# ============================================================
# 3. stop → shape距離マッピング / 仮想バスモデル構築
# ============================================================
# 同一 trip 内に stop_id 重複があり得るので stop_sequence をキーに保持する
STOP_TO_SHAPE_DIST: dict[tuple[str, int], float] = {}
TRIP_PROGRESS: dict[str, list[dict]] = {}
TRIP_SHAPE_START_ABS: dict[str, float] = {}
TRIP_ROUTE_LENGTH: dict[str, float] = {}

for trip_id, stop_times in TRIP_STOP_TIMES.items():
    shape_id = TRIPS[trip_id]["shape_id"]
    total_shape = SHAPE_TOTAL_DISTANCE.get(shape_id, 0.0)
    if not stop_times:
        TRIP_PROGRESS[trip_id] = []
        TRIP_SHAPE_START_ABS[trip_id] = 0.0
        TRIP_ROUTE_LENGTH[trip_id] = 0.0
        continue

    first_stop = STOPS.get(stop_times[0]["stop_id"])
    first_proj = project_to_shape(shape_id, first_stop["lat"], first_stop["lon"]) if first_stop else None
    base_abs = first_proj["distance_m"] if first_proj else 0.0
    TRIP_SHAPE_START_ABS[trip_id] = base_abs

    progress = []
    prev_rel = 0.0
    for idx, st in enumerate(stop_times):
        stop = STOPS.get(st["stop_id"])
        if not stop:
            continue
        proj = project_to_shape(shape_id, stop["lat"], stop["lon"])
        if proj is None:
            rel = prev_rel
            offset_m = None
        else:
            abs_d = proj["distance_m"]
            if idx == 0:
                rel = 0.0
            else:
                rel = abs_d - base_abs
                # ループshape対応: startより手前に見えたらshape全長を加算してunwrap
                while rel < (prev_rel - 20.0):
                    rel += total_shape
                if rel < prev_rel:
                    rel = prev_rel
            offset_m = proj["offset_m"]

        prev_rel = rel
        STOP_TO_SHAPE_DIST[(trip_id, st["stop_sequence"])] = rel
        progress.append({
            "time_sec": st["arrival_sec"],
            "distance_m": rel,
            "stop_id": st["stop_id"],
            "stop_sequence": st["stop_sequence"],
            "snap_offset_m": offset_m,
        })

    TRIP_PROGRESS[trip_id] = progress
    TRIP_ROUTE_LENGTH[trip_id] = progress[-1]["distance_m"] if progress else 0.0


# ============================================================
# 4. 仮想バス補間関数
# ============================================================
def time_to_distance(trip_id: str, t: int) -> float:
    prog = TRIP_PROGRESS.get(trip_id, [])
    if not prog:
        return 0.0
    if t <= prog[0]["time_sec"]:
        return prog[0]["distance_m"]
    if t >= prog[-1]["time_sec"]:
        return prog[-1]["distance_m"]

    for i in range(len(prog) - 1):
        p1 = prog[i]
        p2 = prog[i + 1]
        t1, t2 = p1["time_sec"], p2["time_sec"]
        if t1 <= t <= t2:
            if t2 == t1:
                return p2["distance_m"]
            ratio = (t - t1) / (t2 - t1)
            return p1["distance_m"] + ratio * (p2["distance_m"] - p1["distance_m"])
    return prog[-1]["distance_m"]


def distance_to_time(trip_id: str, d: float) -> int:
    prog = TRIP_PROGRESS.get(trip_id, [])
    if not prog:
        return 0
    if d <= prog[0]["distance_m"]:
        return prog[0]["time_sec"]
    if d >= prog[-1]["distance_m"]:
        return prog[-1]["time_sec"]

    for i in range(len(prog) - 1):
        p1 = prog[i]
        p2 = prog[i + 1]
        d1, d2 = p1["distance_m"], p2["distance_m"]
        if d1 <= d <= d2:
            if abs(d2 - d1) < 1e-6:
                # 停車やshape上同一点の繰り返し時は時刻レンジの後側を返す
                return p2["time_sec"]
            ratio = (d - d1) / (d2 - d1)
            return int(round(p1["time_sec"] + ratio * (p2["time_sec"] - p1["time_sec"])))
    return prog[-1]["time_sec"]


def _project_to_trip_distance(trip_id: str, lat: float, lon: float, anchor_distance_m: float | None = None) -> dict | None:
    shape_id = TRIPS[trip_id]["shape_id"]
    proj = project_to_shape(shape_id, lat, lon)
    if proj is None:
        return None

    abs_d = proj["distance_m"]
    base_abs = TRIP_SHAPE_START_ABS.get(trip_id, 0.0)
    total_shape = SHAPE_TOTAL_DISTANCE.get(shape_id, 0.0)

    raw_rel = abs_d - base_abs
    candidates = [raw_rel, raw_rel + total_shape, raw_rel - total_shape]
    candidates = [c for c in candidates if c >= -100.0]
    if not candidates:
        candidates = [0.0]

    if anchor_distance_m is None:
        distance_m = min((max(0.0, c) for c in candidates), key=lambda x: abs(x))
    else:
        distance_m = min((max(0.0, c) for c in candidates), key=lambda x: abs(x - anchor_distance_m))

    return {
        **proj,
        "abs_distance_m": round(abs_d, 1),
        "distance_m": round(distance_m, 1),
    }


# ============================================================
# 5. サービスID / 候補便
# ============================================================
_DOW_MAP = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def resolve_active_service_ids(target_date: datetime) -> list[str]:
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
            active.append(sid)
            continue
        if cal[_DOW_MAP[dow_idx]] == "1":
            active.append(sid)
    return active


def _candidate_trips(now_dt: datetime) -> list[str]:
    active_sids = resolve_active_service_ids(now_dt)
    if not active_sids:
        return []

    now_sec = _seconds_since_midnight(now_dt)
    candidates = []
    for trip_id, trip in TRIPS.items():
        if trip["service_id"] not in active_sids:
            continue
        stops = TRIP_STOP_TIMES.get(trip_id)
        if not stops:
            continue
        first_dep = stops[0]["departure_sec"]
        last_arr = stops[-1]["arrival_sec"]
        if (first_dep - TRIP_TIME_BUFFER_SEC) <= now_sec <= (last_arr + TRIP_TIME_BUFFER_SEC):
            candidates.append(trip_id)
    return candidates


# ============================================================
# 6. shapeベース便推定
# ============================================================
def _smooth_position(lat: float, lon: float, prev_state: dict | None, now_dt: datetime) -> tuple[float, float]:
    if not prev_state:
        return lat, lon
    prev_lat = prev_state.get("lat")
    prev_lon = prev_state.get("lon")
    prev_ts = prev_state.get("timestamp_unix")
    if prev_lat is None or prev_lon is None or prev_ts is None:
        return lat, lon
    age = int(now_dt.timestamp()) - int(prev_ts)
    if age < 0 or age > SMOOTHING_LOOKBACK_SEC:
        return lat, lon
    return lat * 0.8 + float(prev_lat) * 0.2, lon * 0.8 + float(prev_lon) * 0.2


def _nearest_progress_stop(trip_id: str, current_distance_m: float) -> dict | None:
    prog = TRIP_PROGRESS.get(trip_id, [])
    if not prog:
        return None
    return min(prog, key=lambda p: abs(p["distance_m"] - current_distance_m))


def match_trip(lat: float, lon: float, now_dt: datetime, prev_state: dict | None = None) -> dict | None:
    candidates = _candidate_trips(now_dt)
    if not candidates:
        logger.info("No candidate trips for %s", now_dt.isoformat())
        return None

    now_sec = _seconds_since_midnight(now_dt)
    smoothed_lat, smoothed_lon = _smooth_position(lat, lon, prev_state, now_dt)

    prev_trip = (prev_state or {}).get("trip") or {}
    prev_trip_id = prev_trip.get("trip_id")
    prev_distance = prev_trip.get("current_distance_m")
    prev_ts = (prev_state or {}).get("timestamp_unix")
    prev_is_fresh = False
    if prev_ts is not None:
        age = int(now_dt.timestamp()) - int(prev_ts)
        prev_is_fresh = 0 <= age <= 180

    best = None
    best_score = float("inf")

    for trip_id in candidates:
        expected_distance_m = time_to_distance(trip_id, now_sec)
        anchor = expected_distance_m
        if prev_is_fresh and prev_trip_id == trip_id and prev_distance is not None:
            anchor = (expected_distance_m + float(prev_distance)) / 2.0

        proj = _project_to_trip_distance(trip_id, smoothed_lat, smoothed_lon, anchor_distance_m=anchor)
        if proj is None:
            continue
        if proj["offset_m"] > MAX_SHAPE_OFFSET_M:
            continue

        current_distance_m = proj["distance_m"]
        distance_error_m = abs(expected_distance_m - current_distance_m)
        delta_from_prev = None
        continuity_penalty = 0.0

        if prev_is_fresh and prev_distance is not None:
            try:
                delta_from_prev = current_distance_m - float(prev_distance)
            except Exception:
                delta_from_prev = None

        if prev_trip_id == trip_id and delta_from_prev is not None:
            if abs(delta_from_prev) > MAX_JUMP_DISTANCE_M:
                continue
            if delta_from_prev < -REVERSE_REJECT_M:
                continue
            if delta_from_prev < -20:
                continuity_penalty += 250.0
            else:
                continuity_penalty -= 80.0
        elif prev_trip_id and prev_trip_id in candidates:
            continuity_penalty += 120.0

        score = distance_error_m + continuity_penalty
        if score < best_score:
            nearest_stop = _nearest_progress_stop(trip_id, current_distance_m)
            if nearest_stop is None:
                continue
            stop_geo = STOPS.get(nearest_stop["stop_id"])
            stop_geo_distance = None
            if stop_geo:
                stop_geo_distance = _haversine(smoothed_lat, smoothed_lon, stop_geo["lat"], stop_geo["lon"])
            status = "STOPPED_AT" if (stop_geo_distance is not None and stop_geo_distance <= STOP_NEAR_GEO_THRESHOLD_M) else "IN_TRANSIT_TO"
            expected_time_sec = distance_to_time(trip_id, current_distance_m)
            delay_sec = int(now_sec - expected_time_sec)
            route_id = TRIPS[trip_id]["route_id"]
            best_score = score
            best = {
                "trip_id": trip_id,
                "route_id": route_id,
                "route_name": ROUTES.get(route_id, {}).get("route_long_name", ""),
                "shape_id": TRIPS[trip_id]["shape_id"],
                "current_distance_m": round(current_distance_m, 1),
                "expected_distance_m": round(expected_distance_m, 1),
                "distance_error_m": round(distance_error_m, 1),
                "projection_offset_m": round(proj["offset_m"], 1),
                "expected_time_sec": int(expected_time_sec),
                "delay_sec": int(delay_sec),
                "nearest_stop_id": nearest_stop["stop_id"],
                "nearest_stop_name": STOPS.get(nearest_stop["stop_id"], {}).get("name", ""),
                "current_stop_sequence": nearest_stop["stop_sequence"],
                "vehicle_stop_status": status,
                "score": round(score, 1),
                "delta_from_prev_m": round(delta_from_prev, 1) if delta_from_prev is not None else None,
                "smoothed_lat": smoothed_lat,
                "smoothed_lon": smoothed_lon,
            }

    if best is None or best["distance_error_m"] > MAX_MATCH_ERROR_M:
        return None
    return best


# ============================================================
# 7. stop_time_update 生成
# ============================================================
def build_stop_time_updates(trip_id: str, delay_sec: int, current_distance_m: float | None = None, now_sec: int | None = None) -> list[dict]:
    updates = []
    for st in TRIP_STOP_TIMES.get(trip_id, []):
        stop_distance = STOP_TO_SHAPE_DIST.get((trip_id, st["stop_sequence"]))
        if current_distance_m is not None and stop_distance is not None:
            if stop_distance < current_distance_m - 30:
                continue
        elif now_sec is not None:
            if st["arrival_sec"] + delay_sec < now_sec - 60:
                continue
        updates.append({
            "stop_id": st["stop_id"],
            "stop_sequence": st["stop_sequence"],
            "arrival_delay_sec": int(delay_sec),
            "departure_delay_sec": int(delay_sec),
            "distance_m": stop_distance,
        })
    return updates


# ============================================================
# 8. GTFS-RT feed 生成
# ============================================================
def build_gtfs_rt_feed(lat: float, lon: float, timestamp_unix: int, trip_match: dict | None, vehicle_id: str = VEHICLE_ID) -> bytes:
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.incrementality = gtfs_realtime_pb2.FeedHeader.FULL_DATASET
    feed.header.timestamp = timestamp_unix

    entity_vp = feed.entity.add()
    entity_vp.id = f"vp_{vehicle_id}"
    vp = entity_vp.vehicle
    vp.vehicle.id = vehicle_id
    vp.vehicle.label = vehicle_id
    vp.position.latitude = lat
    vp.position.longitude = lon
    vp.timestamp = timestamp_unix

    if trip_match:
        vp.trip.trip_id = trip_match["trip_id"]
        vp.trip.route_id = trip_match["route_id"]
        vp.current_stop_sequence = trip_match["current_stop_sequence"]
        status_map = {
            "IN_TRANSIT_TO": gtfs_realtime_pb2.VehiclePosition.IN_TRANSIT_TO,
            "STOPPED_AT": gtfs_realtime_pb2.VehiclePosition.STOPPED_AT,
            "INCOMING_AT": gtfs_realtime_pb2.VehiclePosition.INCOMING_AT,
        }
        vp.current_status = status_map.get(trip_match["vehicle_stop_status"], gtfs_realtime_pb2.VehiclePosition.IN_TRANSIT_TO)

        now_sec = _seconds_since_midnight(datetime.fromtimestamp(timestamp_unix, tz=JST))
        stu_list = build_stop_time_updates(
            trip_match["trip_id"],
            trip_match["delay_sec"],
            current_distance_m=trip_match.get("current_distance_m"),
            now_sec=now_sec,
        )
        if stu_list:
            entity_tu = feed.entity.add()
            entity_tu.id = f"tu_{vehicle_id}"
            tu = entity_tu.trip_update
            tu.trip.trip_id = trip_match["trip_id"]
            tu.trip.route_id = trip_match["route_id"]
            tu.vehicle.id = vehicle_id
            tu.timestamp = timestamp_unix
            for u in stu_list:
                stu = tu.stop_time_update.add()
                stu.stop_sequence = u["stop_sequence"]
                stu.stop_id = u["stop_id"]
                stu.arrival.delay = u["arrival_delay_sec"]
                stu.departure.delay = u["departure_delay_sec"]

        logger.info(
            "Trip matched(shape): %s route=%s current=%.1fm expected=%.1fm err=%.1fm delay=%.1fmin offset=%.1fm",
            trip_match["trip_id"], trip_match["route_id"],
            trip_match["current_distance_m"], trip_match["expected_distance_m"],
            trip_match["distance_error_m"], trip_match["delay_sec"] / 60.0,
            trip_match["projection_offset_m"],
        )
    else:
        logger.info("No trip matched — VehiclePosition only")

    return feed.SerializeToString()


# ============================================================
# 9. GPS検証
# ============================================================
def _validate_gps(data: dict) -> tuple[bool, str]:
    lat = data.get("lat")
    lon = data.get("lon")
    accuracy = data.get("accuracy")

    if lat is None or lon is None:
        return False, "missing_lat_lon"
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return False, "invalid_lat_lon_type"
    if not (LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX):
        return False, "out_of_service_area"
    if accuracy is not None:
        try:
            if float(accuracy) > 200:
                return False, "accuracy_too_low"
        except (TypeError, ValueError):
            pass
    return True, ""


# ============================================================
# 10. Cloud Functions
# ============================================================
@https_fn.on_request()
def gps(req):
    key = req.headers.get("X-API-KEY")
    if key != API_KEY:
        return ("Unauthorized", 401)
    if req.method != "POST":
        return ("Method Not Allowed", 405)

    try:
        data = req.get_json(force=True)
    except Exception:
        return ({"ok": False, "error": "invalid_json"}, 400)

    now = _now_jst()
    ts_unix = int(now.timestamp())
    server_received_at = now.isoformat()
    accepted, reject_reason = _validate_gps(data)

    vehicle_id = data.get("vehicle_id", VEHICLE_ID)
    lat = data.get("lat")
    lon = data.get("lon")

    latest_ref = db.document(f"vehicles/{vehicle_id}/state/latest")
    latest_snap = latest_ref.get()
    prev_state = latest_snap.to_dict() if latest_snap.exists else None

    trip_match = None
    if accepted:
        try:
            trip_match = match_trip(float(lat), float(lon), now, prev_state=prev_state)
        except Exception as e:
            logger.error("Trip matching error: %s", e, exc_info=True)

    log_doc = {
        "vehicle_id": vehicle_id,
        "lat": float(lat) if lat is not None else None,
        "lon": float(lon) if lon is not None else None,
        "accuracy": data.get("accuracy"),
        "speed": data.get("speed"),
        "heading": data.get("heading"),
        "timestamp": data.get("timestamp"),
        "timestamp_unix": ts_unix,
        "server_received_at": server_received_at,
        "accepted": accepted,
        "reject_reason": reject_reason,
        "raw": data,
    }
    if trip_match:
        log_doc["trip_match"] = {
            "trip_id": trip_match["trip_id"],
            "route_id": trip_match["route_id"],
            "shape_id": trip_match["shape_id"],
            "current_distance_m": trip_match["current_distance_m"],
            "expected_distance_m": trip_match["expected_distance_m"],
            "distance_error_m": trip_match["distance_error_m"],
            "projection_offset_m": trip_match["projection_offset_m"],
            "delay_sec": trip_match["delay_sec"],
            "current_stop_sequence": trip_match["current_stop_sequence"],
            "nearest_stop_id": trip_match["nearest_stop_id"],
            "score": trip_match["score"],
            "delta_from_prev_m": trip_match["delta_from_prev_m"],
        }
    db.collection("gps_logs").add(log_doc)

    if accepted:
        latest_doc = {
            "vehicle_id": vehicle_id,
            "lat": float(lat),
            "lon": float(lon),
            "accuracy": data.get("accuracy"),
            "speed": data.get("speed"),
            "heading": data.get("heading"),
            "timestamp": data.get("timestamp"),
            "timestamp_unix": ts_unix,
            "server_received_at": server_received_at,
            "updated_at": firestore.SERVER_TIMESTAMP,
            "trip": None,
        }
        if trip_match:
            latest_doc["trip"] = {
                "trip_id": trip_match["trip_id"],
                "route_id": trip_match["route_id"],
                "route_name": trip_match["route_name"],
                "shape_id": trip_match["shape_id"],
                "current_distance_m": trip_match["current_distance_m"],
                "expected_distance_m": trip_match["expected_distance_m"],
                "distance_error_m": trip_match["distance_error_m"],
                "projection_offset_m": trip_match["projection_offset_m"],
                "expected_time_sec": trip_match["expected_time_sec"],
                "delay_sec": trip_match["delay_sec"],
                "nearest_stop_id": trip_match["nearest_stop_id"],
                "nearest_stop_name": trip_match["nearest_stop_name"],
                "current_stop_sequence": trip_match["current_stop_sequence"],
                "vehicle_stop_status": trip_match["vehicle_stop_status"],
                "score": trip_match["score"],
                "delta_from_prev_m": trip_match["delta_from_prev_m"],
            }
        latest_ref.set(latest_doc)

    resp = {"ok": True, "accepted": accepted}
    if trip_match:
        resp["trip"] = {
            "trip_id": trip_match["trip_id"],
            "route_id": trip_match["route_id"],
            "route_name": trip_match["route_name"],
            "delay_min": round(trip_match["delay_sec"] / 60.0, 1),
            "nearest_stop": trip_match["nearest_stop_name"],
            "status": trip_match["vehicle_stop_status"],
            "current_distance_m": trip_match["current_distance_m"],
            "distance_error_m": trip_match["distance_error_m"],
        }
    if not accepted:
        resp["reject_reason"] = reject_reason
    return (resp, 200)


@https_fn.on_request()
def gtfs_rt(req):
    doc_ref = db.document(f"vehicles/{VEHICLE_ID}/state/latest")
    doc = doc_ref.get()

    if not doc.exists:
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.header.gtfs_realtime_version = "2.0"
        feed.header.incrementality = gtfs_realtime_pb2.FeedHeader.FULL_DATASET
        feed.header.timestamp = int(time.time())
        return (feed.SerializeToString(), 200, {"Content-Type": "application/x-protobuf"})

    state = doc.to_dict()
    trip_info = state.get("trip")
    trip_match = trip_info if trip_info and trip_info.get("trip_id") else None

    if trip_match is None and state.get("lat") is not None and state.get("lon") is not None:
        try:
            ts_unix = int(state.get("timestamp_unix", int(time.time())))
            dt = datetime.fromtimestamp(ts_unix, tz=JST)
            trip_match = match_trip(float(state["lat"]), float(state["lon"]), dt, prev_state=state)
        except Exception as e:
            logger.error("Fallback trip match failed in gtfs_rt: %s", e, exc_info=True)

    pb = build_gtfs_rt_feed(
        lat=float(state.get("lat", 0.0)),
        lon=float(state.get("lon", 0.0)),
        timestamp_unix=int(state.get("timestamp_unix", int(time.time()))),
        trip_match=trip_match,
        vehicle_id=VEHICLE_ID,
    )
    return (pb, 200, {"Content-Type": "application/x-protobuf"})


logger.info(
    "GTFS loaded: %d stops, %d routes, %d trips, %d shape_ids, %d stop_time rows",
    len(STOPS), len(ROUTES), len(TRIPS), len(SHAPES), sum(len(v) for v in TRIP_STOP_TIMES.values())
)
