# 瑞穂町コミュニティバス バスロケーションシステム (PoC)

瑞穂町コミュニティバスのリアルタイム位置情報を収集・配信するシステムの実証実験（PoC）プロジェクトです。
運転手の操作を一切不要とする「完全自動運用」と、標準フォーマット「GTFS-RT」への準拠を特徴としています。

## 📅 プロジェクト概要
* **プロジェクト名:** Mizuho Bus Location PoC
* **バージョン:** 1.1
* **期間:** 2026年3月31日までの実証実験
* **目的:** 住民の利便性向上およびバス運行状況の可視化

## 🚀 主な特徴
1.  **完全自動化 (Zero Operation):**
    * エンジン連動による自動計測・送信を実現。
    * 運転手による端末操作を一切不要とし、現場負担をゼロにします。
2.  **標準規格準拠 (GTFS-RT):**
    * 世界標準フォーマットである GTFS Realtime 形式でデータを出力。
    * 将来的なGoogleマップ連携や他アプリへの展開を考慮しています。
3.  **サーバーサイド主導アーキテクチャ:**
    * 複雑な座標計算や遅延判定をサーバー（Module B）に集約。
    * フロントエンド（Module C）を軽量化し、メンテナンス性を向上させています。

## 🛠 システム構成

システムは以下の3つのモジュールで構成されています。

| モジュール | 役割 | 格納ディレクトリ | 技術スタック |
| :--- | :--- | :--- | :--- |
| **Module A (Edge)** | 車載器。GPS情報を収集し送信 | `/edge` | Android / IoT (ESP32) |
| **Module B (Server)** | 運行判定、遅延計算、GTFS-RT生成 | `/backend` | Python, Firebase Functions |
| **Module C (Frontend)** | 利用者への地図表示 | `/frontend` | HTML5, JS, TailwindCSS |

## 📂 ディレクトリ構造

```text
mizuho-basuroke/
├── backend/           # サーバーサイド (Firebase Functions / Python)
│   └── data/
│       └── gtfs/      # 静的バスデータ (GTFS-JP)
├── docs/              # 設計書、要件定義書、会議ログ
├── edge/              # 車載器用コード (Android/IoT)
├── frontend/          # 利用者向けWeb画面
└── README.md          # 本ファイル
```
## 📖 ドキュメント

* **要件定義:** [docs/requirements_v1.1.md](docs/requirements_v1.1.md)
* **Firebase 実装手順:** [docs/firebase_setup.md](docs/firebase_setup.md)（環境構築〜デプロイの流れ）

## 👥 開発体制

Project Lead / Backend / Frontend: 戸谷 (@2u8a)

Edge Device: 関(@hbki3)
