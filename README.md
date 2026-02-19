# 瑞穂町コミュニティバス バスロケーションシステム（PoC）

## 概要
本プロジェクトは、瑞穂町コミュニティバスを対象にした  
**1台構成のバスロケーションシステムPoC**である。

目的は、清瀬市で採用されている  
**「GPS → GTFS-Realtime生成 → Google Maps等への提供」モデル**と同等の技術要件を満たしつつ、  
**1台構成の場合にいくらで実現できたか**を明確に示す  
**行政向け費用実証レポート**を作成することである。

---

## Scope / Out of Scope

### Scope（対象）
- バス **1台** を対象としたPoC
- Module A（車載）・Module B（サーバ）の設計・実装
- Google GTFS-Realtime 仕様に準拠した feed の生成
- 実証期間中のログ蓄積と、事後の集計・報告

### Out of Scope（非対象）
- 住民向け完成UIの提供（Module C は必須ではない）
- 複数台本格運用（将来スケールの検討はレポートで扱う）
- 運転士による操作を前提としたUI・機器操作

---

## Key Requirements

- **更新頻度**
  - 通常時：**約10秒間隔**
  - 通信劣化時でも **最大30秒以内**
- **運用**
  - 運転士操作なし（完全自動）
  - 視界・運転操作の妨げにならない
- **電源**
  - 車両24V → ヒューズボックス → DC/DCコンバータ → 機器給電
- **通信**
  - モバイル回線（LTE / Cat-M 等）
- **成果物**
  - Google要件を満たす GTFS-Realtime feed
  - 実証実験ログおよび費用・品質評価レポート

---

## System Architecture

### Module A（車載）
- GPS測位
- 約10秒間隔で現在位置をサーバへ HTTPS POST
- 運転士操作不要
- モジュール／スマートフォン等、方式は限定しない（再現性重視）

### Module B（サーバ）
- Module A からの位置情報を受信
- 生GPSログを保存
- GTFS-JP と照合し GTFS-Realtime feed を生成
- feed を外部から取得可能な形で公開

### Module C（任意）
- デモ・検証用途のフロントエンド
- 本PoCでは必須ではない

---

## Repository Structure

.
│
├─ README.md                
│
├─ backend/
│   └─ main.py
│
├─ frontend/
│
├─ firebase.json
├─ .firebaserc
└─ docs/
    ├─ architecture.md      ← 全体構成図
    ├─ api-spec.md          ← GPS API仕様
    └─ requirements.md      ← 要件定義

---

## Endpoints & Data Schema（A → B）

### POST /gps
車載モジュールから現在位置を送信するエンドポイント。

#### JSON例
```json
{
  "vehicle_id": "mizuho-bus-01",
  "lat": 35.771234,
  "lon": 139.353210,
  "accuracy": 8.5,
  "speed": 7.2,
  "heading": 180
}

```
## Technical Documentation

- API仕様: docs/api-spec.md
- データ構造: docs/data-model.md
- 開発ロードマップ: docs/roadmap.md


### 📖 ドキュメント

* **要件定義:** [docs/requirements_v1.1.md](docs/requirements_v1.1.md)
* **Firebase 実装手順:** [docs/firebase_setup.md](docs/firebase_setup.md)（環境構築〜デプロイの流れ）

## 👥 開発体制

Project Lead / Backend / Frontend: 戸谷 (@2u8a)

Edge Device: 関(@hbki3)
