# Module B – GPS Ingestion & GTFS-RT Generator

このモジュールは、バス車両から送信されるGPSデータを受信し、
Firestoreに保存し、GTFS-Realtime (VehiclePosition) を生成するAPIサーバーです。

---

## 🏗 全体構成

```

[Module A (Raspberry Pi)]
│
│ HTTP POST (GPS JSON)
▼
[Cloud Functions (Gen2)]
│
├─ 認証チェック（API_KEY）
├─ データ整形 / 異常値判定
├─ 生ログ保存
▼
[Firestore]
│
▼
[GET /gtfs_rt]
│
▼
GTFS-Realtime (feed.pb)

```

---

## 📡 API エンドポイント

### 1️⃣ GPS送信（POST）

```

[https://us-central1-mizuho-basuroke-f370e.cloudfunctions.net/gps](https://us-central1-mizuho-basuroke-f370e.cloudfunctions.net/gps)

```

#### Headers

```

Content-Type: application/json
X-API-KEY: <APIキー>

````

#### Body (JSON)

```json
{
  "vehicle_id": "mizuho-bus-01",
  "timestamp": "2026-02-19T18:45:00+09:00",
  "lat": 35.771234,
  "lon": 139.345678,
  "accuracy": 8.5,
  "speed": 7.2,
  "heading": 180
}
````

#### 動作仕様

* 送信間隔：10秒
* 失敗時：その回は破棄（再送キュー不要）
* 異常値は accepted=false で保存

---

### 2️⃣ GTFS-Realtime取得（GET）

```
https://us-central1-mizuho-basuroke-f370e.cloudfunctions.net/gtfs_rt
```

* `VehiclePosition` のみ出力
* content-type: `application/x-protobuf`
* Firestore の latest データを参照

---

## 🔐 認証方式

Cloud Run 環境変数に設定：

```
API_KEY=<任意のランダム文字列>
```

* ヘッダ `X-API-KEY` が一致しない場合 401 を返す

---

## 🗄 Firestore構造

### gps_logs（生ログ）

| フィールド              | 説明     |
| ------------------ | ------ |
| vehicle_id         | 車両ID   |
| lat/lon            | 緯度経度   |
| accuracy           | 精度(m)  |
| speed              | 速度     |
| heading            | 方位     |
| accepted           | 採用可否   |
| reject_reason      | 不採用理由  |
| server_received_at | 受信時刻   |
| raw                | 受信JSON |

---

### vehicles/{vehicle_id}/state/latest

最新の採用済みGPSデータのみ保存。

GTFS-RT生成時に参照する。

---

## 🧪 テスト方法

### GPS送信テスト（PowerShell）

```powershell
Invoke-RestMethod -Method Post `
  -Uri "https://us-central1-mizuho-basuroke-f370e.cloudfunctions.net/gps" `
  -Headers @{ "X-API-KEY" = "<APIキー>" } `
  -ContentType "application/json" `
  -Body '{"vehicle_id":"mizuho-bus-01","lat":35.7,"lon":139.3,"accuracy":8.5}'
```

### feed.pb確認

```powershell
Invoke-WebRequest `
  -Uri "https://us-central1-mizuho-basuroke-f370e.cloudfunctions.net/gtfs_rt" `
  -OutFile feed.pb
```

---

## ⚙ 技術スタック

* Firebase Cloud Functions (Gen2)
* Cloud Run
* Firestore
* firebase-admin
* gtfs-realtime-bindings

---

## 🎯 現在のステータス（Step1完了）

* GPS受信API完成
* 認証実装済み
* Firestore保存確認済み
* GTFS-RT生成確認済み

---

## 🚀 次ステップ

* JPFS-JP静的GTFSとの突合
* trip推定アルゴリズム実装
* 遅延判定ロジック
* リージョンを asia-northeast1 に統一

```
