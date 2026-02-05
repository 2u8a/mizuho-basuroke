# Firebase 実装手順

瑞穂町バスロケーション PoC を Firebase で動かすための、環境構築からデプロイまでの手順です。

---

## 1. 全体像

| 役割 | Firebase サービス | 本リポジトリ |
|------|-------------------|--------------|
| 位置データの受信・保存 | **Realtime Database** | Edge が JSON を書き込み |
| 運行判定・GTFS-RT 生成 | **Cloud Functions (Python)** | `backend/` |
| Web 公開・feed.pb 配信 | **Hosting** | `frontend/` |

```
[Edge] --POST/JSON--> [Realtime Database]
                            |
                            v
              [Cloud Functions] --feed.pb--> [Hosting]
                            |
[Frontend] <--Fetch feed.pb--+
```

---

## 2. 環境・ベースライン（開発の前提）

以下は現時点で構築済みの環境です。今後の開発のベースとして参照してください。

| 項目 | 内容 |
|------|------|
| **Node.js** | v24.13.0（nvm で管理） |
| **Firebase プロジェクトID** | `mizuho-basuroke-f370e` |
| **Hosting 公開フォルダ** | `frontend` |
| **Cloud Functions** | `backend` フォルダ、Python（firebase.json の `functions.source` = `backend`） |

- `.firebaserc` の `default` が上記プロジェクトIDであること。
- Hosting のデプロイは完了済み。Functions は `backend/` をソースとしてデプロイする。

---

## 3. 前提条件

- **Node.js** 18 以上（Firebase CLI 用）
- **Python** 3.11 以上（Cloud Functions のローカル開発・デプロイ用。現在の設定は 3.13）
- **Google アカウント**（Firebase プロジェクトのオーナー/編集者）
- リポジトリをクローン済みで、ルートで作業する想定

---

## 4. 手順

### ステップ 1: Firebase CLI のインストールとログイン

```bash
# CLI をグローバルにインストール
npm install -g firebase-tools

# Google アカウントでログイン
firebase login
```

ブラウザが開くので、プロジェクトのオーナー権限があるアカウントでログインしてください。

---

### ステップ 2: プロジェクトの確認

```bash
# 現在のプロジェクトを確認
firebase projects:list

# このリポジトリに紐づくプロジェクト（.firebaserc に記載）
cat .firebaserc
```

別のプロジェクトで試したい場合は、以下で切り替えます。

```bash
firebase use <プロジェクトID>
```

---

### ステップ 3: Realtime Database を有効化

1. [Firebase コンソール](https://console.firebase.google.com/) を開く
2. 対象プロジェクト（例: `mizuho-basuroke-f370e`）を選択
3. 左メニュー **「Build」→「Realtime Database」**
4. **「データベースを作成」** をクリック
   - リージョンは「asia-northeast1」（東京）を推奨
   - 本番前に **「ルール」** タブで `database.rules.json` の内容に合わせて読み書きを制限すること

開発中は `database.rules.json` のルールを緩め、本番では Edge 以外の書き込みを禁止するなど厳格にします。

---

### ステップ 4: Cloud Functions（Python）の開発とデプロイ

Cloud Functions のソースは **`backend/`** です（firebase.json の `source: "backend"`）。

#### 4-1. 仮想環境と依存関係（初回のみ）

```bash
cd backend
python3 -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cd ..
```

#### 4-2. ローカルで動作確認（任意）

```bash
# エミュレータで Functions のみ起動
firebase emulators:start --only functions
```

#### 4-3. デプロイ

```bash
# リポジトリルートで実行
firebase deploy --only functions
```

- GTFS-JP は **`backend/data/gtfs/`** に配置されています。デプロイ時には `backend/` 一式がアップロードされるため、Functions 内から `./data/gtfs/` で参照できます。

---

### ステップ 5: Hosting（フロントエンド）のデプロイ

```bash
# フロントエンドのみデプロイ
firebase deploy --only hosting
```

- 公開先は `frontend/` ディレクトリの内容です。
- デプロイ後、コンソールの **Hosting** に表示される URL（例: `https://mizuho-basuroke-f370e.web.app`）でアクセスできます。

---

### ステップ 6: 一括デプロイ（Functions + Hosting + Database ルール）

```bash
# すべてまとめてデプロイ
firebase deploy
```

- **Functions**・**Hosting**・**Database のルール**（`database.rules.json`）が一度に反映されます。
- Database のルールだけ更新したい場合は `firebase deploy --only database` を使います。

---

## 5. よく使うコマンド一覧

| 目的 | コマンド |
|------|----------|
| ログイン確認 | `firebase login:list` |
| 使用プロジェクト確認 | `firebase use` |
| Functions のみデプロイ | `firebase deploy --only functions` |
| Hosting のみデプロイ | `firebase deploy --only hosting` |
| Database ルールのみデプロイ | `firebase deploy --only database` |
| 全デプロイ | `firebase deploy` |
| エミュレータ（Functions） | `firebase emulators:start --only functions` |

---

## 6. 注意事項

- **Realtime Database のルール**  
  本番では、書き込みは認証済みの Edge 端末（または Cloud Functions 経由）に限定し、読み取りは必要最小限にしてください。
- **GTFS の参照**  
  Cloud Functions のソースは `backend/` です。`backend/data/gtfs/` はデプロイ時に一緒にアップロードされるため、コード内では `./data/gtfs/` で参照できます。
- **Blaze プラン**  
  Cloud Functions を使うには、Firebase の **Blaze（従量課金）** プランが必要です。無料枠内で収まるよう、関数の呼び出し回数・実行時間は設計で考慮してください。

---

## 7. 次のステップ（実装順の目安）

1. **B-01**: Realtime Database に Edge が書き込むデータ構造を決め、ルールを書く
2. **B-02〜B-04**: Cloud Functions で運行判定・遅延計算・GTFS-RT（feed.pb）生成を実装
3. **B-05**: Hosting で `feed.pb` を配信する URL を用意（Functions で生成した .pb を Storage に書き、Hosting から参照するか、Functions の HTTP エンドポイントで返すかは設計で決定）
4. **C-01〜C-04**: フロントエンドで `feed.pb` を取得・表示

要件の詳細は `docs/requirements_v1.1.md` を参照してください。
