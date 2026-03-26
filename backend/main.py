"""
Module B – GPS受信 / 便(trip)推定 / 遅延判定 / GTFS-RT生成

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

# 便推定パラメータ
STOP_NEAR_THRESHOLD_M = 150       # この距離以内なら「停留所付近」と判定
TRIP_TIME_BUFFER_SEC = 600        # 便の前後10分をマッチ対象に含める
MAX_MATCH_DISTANCE_M = 800        # これ以上遠いと便マッチしない
EARTH_RADIUS_M = 6_371_000        # 地球半径 (m)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("busroq")

# ============================================================
# Firebase 初期化
# ============================================================
firebase_admin.initialize_app()
db = firestore.client()
API_KEY = os.environ.get("API_KEY")

# ============================================================
# GTFS 静的データ読み込み (コールドスタート時に1回)
# ============================================================

def _load_csv(filename: str) -> list[dict]:
    """UTF-8 BOM付きCSVを読み込む"""
    path = os.path.join(GTFS_DIR, filename)
    with open(path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = []
        for row in reader:
            # 改行文字の除去
            cleaned = {k.strip(): v.strip() for k, v in row.items() if k}
            rows.append(cleaned)
        return rows


def _parse_gtfs_time(t: str) -> int:
    """GTFS時刻文字列 "H:MM:SS" → 深夜0時からの秒数"""
    parts = t.strip().split(":")
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])


# --- 停留所 ---
_stops_raw = _load_csv("stops.txt")
STOPS: dict[str, dict] = {}
for r in _stops_raw:
    STOPS[r["stop_id"]] = {
        "name": r["stop_name"],
        "lat": float(r["stop_lat"]),
        "lon": float(r["stop_lon"]),
    }

# --- 路線 ---
_routes_raw = _load_csv("routes.txt")
ROUTES: dict[str, dict] = {r["route_id"]: r for r in _routes_raw}

# --- 便 ---
_trips_raw = _load_csv("trips.txt")
TRIPS: dict[str, dict] = {r["trip_id"]: r for r in _trips_raw}

# --- 便ごとの停車時刻 (stop_sequence順) ---
_stop_times_raw = _load_csv("stop_times.txt")
TRIP_STOP_TIMES: dict[str, list[dict]] = {}
for r in _stop_times_raw:
    tid = r["trip_id"]
    if tid not in TRIP_STOP_TIMES:
        TRIP_STOP_TIMES[tid] = []
    TRIP_STOP_TIMES[tid].append({
        "stop_id": r["stop_id"],
        "arrival_sec": _parse_gtfs_time(r["arrival_time"]),
        "departure_sec": _parse_gtfs_time(r["departure_time"]),
        "stop_sequence": int(r["stop_sequence"]),
        "arrival_time_str": r["arrival_time"],
        "departure_time_str": r["departure_time"],
    })
for tid in TRIP_STOP_TIMES:
    TRIP_STOP_TIMES[tid].sort(key=lambda x: x["stop_sequence"])

# --- カレンダー ---
_calendar_raw = _load_csv("calendar.txt")
CALENDAR: dict[str, dict] = {r["service_id"]: r for r in _calendar_raw}

_calendar_dates_raw = _load_csv("calendar_dates.txt")
CALENDAR_DATES: dict[tuple[str, str], int] = {}
for r in _calendar_dates_raw:
    CALENDAR_DATES[(r["service_id"], r["date"])] = int(r["exception_type"])

logger.info(
    "GTFS loaded: %d stops, %d routes, %d trips, %d stop_time records",
    len(STOPS), len(ROUTES), len(TRIPS),
    sum(len(v) for v in TRIP_STOP_TIMES.values()),
)


# ============================================================
# ユーティリティ
# ============================================================

def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """2点間の距離 (m) — Haversine式"""
    rlat1, rlon1 = math.radians(lat1), math.radians(lon1)
    rlat2, rlon2 = math.radians(lat2), math.radians(lon2)
    dlat = rlat2 - rlat1
    dlon = rlon2 - rlon1
    a = (math.sin(dlat / 2) ** 2
         + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2)
    return EARTH_RADIUS_M * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _now_jst() -> datetime:
    return datetime.now(JST)


def _seconds_since_midnight(dt: datetime) -> int:
    """JST日時 → その日の0時からの経過秒"""
    return dt.hour * 3600 + dt.minute * 60 + dt.second


# ============================================================
# 1. サービスID判定 (今日は平日 or 土休日 か)
# ============================================================

_DOW_MAP = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def resolve_active_service_ids(target_date: datetime) -> list[str]:
    """
    指定日に有効な service_id のリストを返す。
    calendar_dates の例外を優先する。
    """
    d = target_date.date() if isinstance(target_date, datetime) else target_date
    date_str = d.strftime("%Y%m%d")
    dow_idx = d.weekday()  # 0=月 … 6=日

    active = []
    for sid, cal in CALENDAR.items():
        # 期間チェック
        if date_str < cal["start_date"] or date_str > cal["end_date"]:
            continue

        # calendar_dates 例外チェック
        exc = CALENDAR_DATES.get((sid, date_str))
        if exc == 2:
            # exception_type=2 → この日は運休
            continue
        if exc == 1:
            # exception_type=1 → この日は追加運行
            active.append(sid)
            continue

        # 曜日チェック
        if cal[_DOW_MAP[dow_idx]] == "1":
            active.append(sid)

    return active


# ============================================================
# 2. 便(trip)推定
# ============================================================

def _candidate_trips(now_dt: datetime) -> list[str]:
    """
    現在時刻と曜日から、運行中の可能性がある trip_id を返す。
    """
    active_sids = resolve_active_service_ids(now_dt)
    if not active_sids:
        return []

    now_sec = _seconds_since_midnight(now_dt)
    candidates = []

    for tid, trip in TRIPS.items():
        if trip["service_id"] not in active_sids:
            continue
        stops = TRIP_STOP_TIMES.get(tid)
        if not stops:
            continue
        first_dep = stops[0]["departure_sec"]
        last_arr = stops[-1]["arrival_sec"]
        # 便の時間帯 ± バッファ内か
        if (first_dep - TRIP_TIME_BUFFER_SEC) <= now_sec <= (last_arr + TRIP_TIME_BUFFER_SEC):
            candidates.append(tid)

    return candidates


def match_trip(lat: float, lon: float, now_dt: datetime) -> dict | None:
    """
    GPS位置と現在時刻から最も尤もらしい便を推定する。

    返値:
        {
            "trip_id": str,
            "route_id": str,
            "route_name": str,
            "nearest_stop_id": str,
            "nearest_stop_name": str,
            "nearest_stop_seq": int,
            "distance_to_stop_m": float,
            "current_stop_sequence": int,
            "vehicle_stop_status": str,  # "IN_TRANSIT_TO" | "STOPPED_AT" | "INCOMING_AT"
            "delay_sec": int,            # 遅延秒数 (正=遅れ / 負=早着)
            "score": float,
        }
        またはマッチなしの場合 None
    """
    candidates = _candidate_trips(now_dt)
    if not candidates:
        logger.info("No candidate trips for %s", now_dt.isoformat())
        return None

    now_sec = _seconds_since_midnight(now_dt)
    best = None
    best_score = float("inf")

    for tid in candidates:
        stops = TRIP_STOP_TIMES[tid]
        trip = TRIPS[tid]

        # 各停留所への距離を計算
        min_dist = float("inf")
        min_stop = None
        min_idx = -1
        for i, st in enumerate(stops):
            s = STOPS.get(st["stop_id"])
            if s is None:
                continue
            d = _haversine(lat, lon, s["lat"], s["lon"])
            if d < min_dist:
                min_dist = d
                min_stop = st
                min_idx = i

        if min_stop is None or min_dist > MAX_MATCH_DISTANCE_M:
            continue

        # --- 遅延推定 ---
        delay_sec = _estimate_delay(lat, lon, now_sec, stops, min_idx)

        # --- スコア計算 ---
        # スコア = 距離ペナルティ + 時間整合性ペナルティ (小さいほど良い)
        time_diff = abs(now_sec - min_stop["arrival_sec"])
        # 距離を0-1に正規化 (500m → 1.0)
        dist_score = min_dist / MAX_MATCH_DISTANCE_M
        # 時間差を0-1に正規化 (600秒 → 1.0)
        time_score = min(time_diff / TRIP_TIME_BUFFER_SEC, 2.0)
        score = dist_score * 0.6 + time_score * 0.4

        if score < best_score:
            best_score = score
            # 停車状態の判定
            if min_dist < STOP_NEAR_THRESHOLD_M:
                status = "STOPPED_AT"
            else:
                status = "IN_TRANSIT_TO"

            route_id = trip["route_id"]
            route_name = ROUTES.get(route_id, {}).get("route_long_name", "")
            stop_info = STOPS.get(min_stop["stop_id"], {})

            # 次の停留所を決定 (IN_TRANSIT_TO の場合)
            if status == "IN_TRANSIT_TO":
                # 進行方向を考慮: 最寄り停留所の前後で次の停留所を推定
                next_idx = _find_next_stop_idx(now_sec, stops, min_idx)
                current_stop_seq = stops[next_idx]["stop_sequence"]
            else:
                current_stop_seq = min_stop["stop_sequence"]

            best = {
                "trip_id": tid,
                "route_id": route_id,
                "route_name": route_name,
                "nearest_stop_id": min_stop["stop_id"],
                "nearest_stop_name": stop_info.get("name", ""),
                "nearest_stop_seq": min_stop["stop_sequence"],
                "distance_to_stop_m": round(min_dist, 1),
                "current_stop_sequence": current_stop_seq,
                "vehicle_stop_status": status,
                "delay_sec": delay_sec,
                "score": round(best_score, 4),
            }

    return best


def _find_next_stop_idx(now_sec: int, stops: list[dict], nearest_idx: int) -> int:
    """
    時刻に基づいて「次の停留所」のインデックスを返す。
    現在時刻より後の最初の停留所 = 次の停留所。
    """
    for i in range(len(stops)):
        if stops[i]["arrival_sec"] > now_sec:
            return i
    # 全停留所を過ぎている場合 → 最終停留所
    return len(stops) - 1


def _estimate_delay(
    lat: float, lon: float, now_sec: int,
    stops: list[dict], nearest_idx: int
) -> int:
    """
    遅延秒数を推定する。

    方式: 区間補間法
    1. バスが停留所i と i+1 の間にいると仮定
    2. 両停留所への距離比から、区間内の進捗率を算出
    3. 進捗率に基づき「今いるべき時刻」を線形補間
    4. 遅延 = 現在時刻 - 補間時刻
    """
    if len(stops) < 2:
        return now_sec - stops[0]["arrival_sec"] if stops else 0

    # --- 区間の特定 ---
    # nearest_idx の前後で区間を決める
    if nearest_idx == 0:
        seg_start_idx, seg_end_idx = 0, 1
    elif nearest_idx == len(stops) - 1:
        seg_start_idx, seg_end_idx = len(stops) - 2, len(stops) - 1
    else:
        # 前の停留所と次の停留所、どちらの区間にいるか判定
        # 時刻で判断: now が stops[nearest_idx]の時刻より前なら前区間
        if now_sec <= stops[nearest_idx]["arrival_sec"]:
            seg_start_idx = nearest_idx - 1
            seg_end_idx = nearest_idx
        else:
            seg_start_idx = nearest_idx
            seg_end_idx = nearest_idx + 1

    s_start = stops[seg_start_idx]
    s_end = stops[seg_end_idx]

    # 停留所の座標
    start_stop = STOPS.get(s_start["stop_id"])
    end_stop = STOPS.get(s_end["stop_id"])

    if not start_stop or not end_stop:
        # フォールバック: 最寄り停留所との差で推定
        return now_sec - stops[nearest_idx]["arrival_sec"]

    # 区間の総距離
    seg_dist = _haversine(start_stop["lat"], start_stop["lon"],
                          end_stop["lat"], end_stop["lon"])

    if seg_dist < 1.0:
        # 同一地点の場合
        return now_sec - s_start["arrival_sec"]

    # バスから両端への距離
    d_to_start = _haversine(lat, lon, start_stop["lat"], start_stop["lon"])
    d_to_end = _haversine(lat, lon, end_stop["lat"], end_stop["lon"])

    # 進捗率 (0.0 = start, 1.0 = end)
    progress = d_to_start / (d_to_start + d_to_end)
    progress = max(0.0, min(1.0, progress))

    # 時刻の線形補間
    sched_start = s_start["departure_sec"]
    sched_end = s_end["arrival_sec"]
    expected_sec = sched_start + progress * (sched_end - sched_start)

    delay = int(now_sec - expected_sec)
    return delay


# ============================================================
# 3. TripUpdate 生成用: 全停留所の遅延予測
# ============================================================

def build_stop_time_updates(trip_id: str, delay_sec: int, now_sec: int) -> list[dict]:
    """
    マッチした便の全停留所について、遅延を反映した StopTimeUpdate 用データを返す。
    
    現在のPoC方式:
    - まだ到着していない停留所に一律 delay_sec を適用 (伝搬遅延)
    - 通過済みの停留所はスキップ
    """
    stops = TRIP_STOP_TIMES.get(trip_id, [])
    updates = []

    for st in stops:
        # 未来の停留所のみ
        if st["arrival_sec"] + delay_sec < now_sec - 60:
            continue
        updates.append({
            "stop_id": st["stop_id"],
            "stop_sequence": st["stop_sequence"],
            "arrival_delay_sec": delay_sec,
            "departure_delay_sec": delay_sec,
            "arrival_sec": st["arrival_sec"],
            "departure_sec": st["departure_sec"],
        })

    return updates


# ============================================================
# 4. GTFS-Realtime feed 生成
# ============================================================

def build_gtfs_rt_feed(
    lat: float, lon: float,
    timestamp_unix: int,
    trip_match: dict | None,
    vehicle_id: str = VEHICLE_ID,
) -> bytes:
    """
    VehiclePosition + TripUpdate を含む GTFS-RT feed.pb を生成。
    """
    feed = gtfs_realtime_pb2.FeedMessage()

    # --- Header ---
    feed.header.gtfs_realtime_version = "2.0"
    feed.header.incrementality = gtfs_realtime_pb2.FeedHeader.FULL_DATASET
    feed.header.timestamp = timestamp_unix

    now_sec_of_day = None

    # --- Entity 1: VehiclePosition ---
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
        # schedule_relationship は SCHEDULED (デフォルト)

        vp.current_stop_sequence = trip_match["current_stop_sequence"]

        status_map = {
            "IN_TRANSIT_TO": gtfs_realtime_pb2.VehiclePosition.IN_TRANSIT_TO,
            "STOPPED_AT": gtfs_realtime_pb2.VehiclePosition.STOPPED_AT,
            "INCOMING_AT": gtfs_realtime_pb2.VehiclePosition.INCOMING_AT,
        }
        vp.current_status = status_map.get(
            trip_match["vehicle_stop_status"],
            gtfs_realtime_pb2.VehiclePosition.IN_TRANSIT_TO,
        )

        # --- Entity 2: TripUpdate ---
        now_sec_of_day = _seconds_since_midnight(
            datetime.fromtimestamp(timestamp_unix, tz=JST)
        )
        stu_list = build_stop_time_updates(
            trip_match["trip_id"],
            trip_match["delay_sec"],
            now_sec_of_day,
        )

        if stu_list:
            entity_tu = feed.entity.add()
            entity_tu.id = f"tu_{vehicle_id}"

            tu = entity_tu.trip_update
            tu.trip.trip_id = trip_match["trip_id"]
            tu.trip.route_id = trip_match["route_id"]
            tu.vehicle.id = vehicle_id
            tu.timestamp = timestamp_unix

            for stu_data in stu_list:
                stu = tu.stop_time_update.add()
                stu.stop_sequence = stu_data["stop_sequence"]
                stu.stop_id = stu_data["stop_id"]
                stu.arrival.delay = stu_data["arrival_delay_sec"]
                stu.departure.delay = stu_data["departure_delay_sec"]
                # schedule_relationship は SCHEDULED (デフォルト)

        # 遅延ログ
        delay_min = trip_match["delay_sec"] / 60
        logger.info(
            "Trip matched: %s (route %s), delay=%.1f min, dist=%.0f m, score=%.4f",
            trip_match["trip_id"],
            trip_match["route_id"],
            delay_min,
            trip_match["distance_to_stop_m"],
            trip_match["score"],
        )
    else:
        logger.info("No trip matched — VehiclePosition only (no TripUpdate)")

    return feed.SerializeToString()


# ============================================================
# 5. GPS受信データの検証
# ============================================================

# 瑞穂町の概略バウンディングボックス
LAT_MIN, LAT_MAX = 35.74, 35.80
LON_MIN, LON_MAX = 139.32, 139.38


def _validate_gps(data: dict) -> tuple[bool, str]:
    """受信JSONの検証。(accepted, reject_reason) を返す。"""

    lat = data.get("lat")
    lon = data.get("lon")
    accuracy = data.get("accuracy")

    if lat is None or lon is None:
        return False, "missing_lat_lon"

    try:
        lat = float(lat)
        lon = float(lon)
    except (ValueError, TypeError):
        return False, "invalid_lat_lon_type"

    if not (LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX):
        return False, "out_of_service_area"

    if accuracy is not None:
        try:
            accuracy = float(accuracy)
            if accuracy > 200:
                return False, "accuracy_too_low"
        except (ValueError, TypeError):
            pass  # accuracy が変換できなくても受け入れる

    return True, ""


# ============================================================
# 6. Cloud Functions エンドポイント
# ============================================================

@https_fn.on_request()
def gps(req):
    """
    POST /gps — 車載端末からGPSデータを受信し、
    便推定・遅延判定を行い、Firestoreに保存する。
    """
    # --- 認証 ---
    key = req.headers.get("X-API-KEY")
    if key != API_KEY:
        return ("Unauthorized", 401)

    if req.method != "POST":
        return ("Method Not Allowed", 405)

    # --- データ取得 ---
    try:
        data = req.get_json(force=True)
    except Exception:
        return ({"ok": False, "error": "invalid_json"}, 400)

    now = _now_jst()
    server_received_at = now.isoformat()

    # --- 検証 ---
    accepted, reject_reason = _validate_gps(data)

    vehicle_id = data.get("vehicle_id", VEHICLE_ID)
    lat = data.get("lat")
    lon = data.get("lon")

    # --- 便推定 (accepted時のみ) ---
    trip_match = None
    if accepted:
        try:
            trip_match = match_trip(float(lat), float(lon), now)
        except Exception as e:
            logger.error("Trip matching error: %s", e, exc_info=True)

    # --- Firestore: 生ログ保存 ---
    log_doc = {
        "vehicle_id": vehicle_id,
        "lat": float(lat) if lat is not None else None,
        "lon": float(lon) if lon is not None else None,
        "accuracy": data.get("accuracy"),
        "speed": data.get("speed"),
        "heading": data.get("heading"),
        "timestamp": data.get("timestamp"),
        "server_received_at": server_received_at,
        "accepted": accepted,
        "reject_reason": reject_reason,
        "raw": data,
    }

    # 便推定結果もログに含める
    if trip_match:
        log_doc["trip_match"] = {
            "trip_id": trip_match["trip_id"],
            "route_id": trip_match["route_id"],
            "delay_sec": trip_match["delay_sec"],
            "nearest_stop_id": trip_match["nearest_stop_id"],
            "distance_to_stop_m": trip_match["distance_to_stop_m"],
            "score": trip_match["score"],
        }

    db.collection("gps_logs").add(log_doc)

    # --- Firestore: 最新状態を更新 (accepted時のみ) ---
    if accepted:
        ts_unix = int(now.timestamp())
        latest_doc = {
            "vehicle_id": vehicle_id,
            "lat": float(lat),
            "lon": float(lon),
            "accuracy": data.get("accuracy"),
            "speed": data.get("speed"),
            "heading": data.get("heading"),
            "timestamp": data.get("timestamp"),
            "server_received_at": server_received_at,
            "updated_at": firestore.SERVER_TIMESTAMP,
            "timestamp_unix": ts_unix,
        }

        # 便推定結果
        if trip_match:
            latest_doc["trip"] = {
                "trip_id": trip_match["trip_id"],
                "route_id": trip_match["route_id"],
                "route_name": trip_match["route_name"],
                "delay_sec": trip_match["delay_sec"],
                "nearest_stop_id": trip_match["nearest_stop_id"],
                "nearest_stop_name": trip_match["nearest_stop_name"],
                "current_stop_sequence": trip_match["current_stop_sequence"],
                "vehicle_stop_status": trip_match["vehicle_stop_status"],
                "distance_to_stop_m": trip_match["distance_to_stop_m"],
                "score": trip_match["score"],
            }
        else:
            latest_doc["trip"] = None

        db.document(f"vehicles/{vehicle_id}/state/latest").set(latest_doc)

    # --- レスポンス ---
    resp = {"ok": True, "accepted": accepted}
    if trip_match:
        delay_min = round(trip_match["delay_sec"] / 60, 1)
        resp["trip"] = {
            "trip_id": trip_match["trip_id"],
            "route_id": trip_match["route_id"],
            "route_name": trip_match["route_name"],
            "delay_min": delay_min,
            "nearest_stop": trip_match["nearest_stop_name"],
            "status": trip_match["vehicle_stop_status"],
        }
    if not accepted:
        resp["reject_reason"] = reject_reason

    return (resp, 200)


@https_fn.on_request()
def gtfs_rt(req):
    """
    GET /gtfs_rt — GTFS-Realtime (VehiclePosition + TripUpdate) を返す。

    content-type: application/x-protobuf
    """
    # --- Firestoreから最新状態を取得 ---
    doc_ref = db.document(f"vehicles/{VEHICLE_ID}/state/latest")
    doc = doc_ref.get()

    if not doc.exists:
        # データなし → 空feed
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.header.gtfs_realtime_version = "2.0"
        feed.header.incrementality = gtfs_realtime_pb2.FeedHeader.FULL_DATASET
        feed.header.timestamp = int(time.time())
        return (
            feed.SerializeToString(),
            200,
            {"Content-Type": "application/x-protobuf"},
        )

    state = doc.to_dict()
    lat = state.get("lat")
    lon = state.get("lon")
    ts_unix = state.get("timestamp_unix", int(time.time()))

    # --- 便推定を再実行 (鮮度保証) ---
    now = _now_jst()
    trip_match = None
    if lat is not None and lon is not None:
        try:
            trip_match = match_trip(float(lat), float(lon), now)
        except Exception as e:
            logger.error("Trip matching error in gtfs_rt: %s", e, exc_info=True)
            # Firestoreに保存済みの情報をフォールバック
            trip_info = state.get("trip")
            if trip_info and trip_info.get("trip_id"):
                trip_match = trip_info

    # --- feed 生成 ---
    pb = build_gtfs_rt_feed(
        lat=float(lat) if lat else 0.0,
        lon=float(lon) if lon else 0.0,
        timestamp_unix=ts_unix,
        trip_match=trip_match,
        vehicle_id=VEHICLE_ID,
    )

    return (pb, 200, {"Content-Type": "application/x-protobuf"})
