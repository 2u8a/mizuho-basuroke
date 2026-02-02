# Module C 仕上げ 納品メモ

## 変更箇所のファイル一覧

| ファイル | 変更内容 |
|----------|----------|
| **frontend/index.html** | JS のみ変更。割当安定化・delay表示・proto失敗時・timerガード・route_colors 読み込みを追加。 |
| **frontend/assets/.gitkeep** | 新規。`/assets/route_colors.json` 配置用ディレクトリの存在確保。 |
| **docs/module_c_finish.md** | 本ファイル（納品メモ）。 |

---

## 主要関数の説明

### 1) 割当ロジック（`assignSlots(viewModels)`）

- **入力**: ViewModel の配列。
- **出力**: `{ orange: vm|null, blue: vm|null }`。各スロットに最大1台ずつ割り当て。
- **ルール**:
  1. **route_id 優先**: 全 ViewModel から非空の `route_id` を集め、ソートする。先頭を orange、2番目を blue に固定。同じ route_id の車両は同じスロット（先頭の1台のみ表示）。
  2. **route_id がない場合**: モジュール直下の `slotByVehicleId` に「初回出現した vehicle_id（または trip_id）→ orange / blue」を記録し、以降はその割当を固定。最初に見えた1台目を orange、2台目を blue に割り当て。
- **目的**: フィードの entity 順が変わっても、同じ route_id / vehicle_id は常に同じ色のマーカーになる。

### 2) delay 表示（`delayToLabelAndClass(delaySec)`）

- **入力**: 遅延秒数（数値 or `null`）。
- **出力**: `{ text, className, pulse }`。
- **ルール**:
  - `null` → 「情報なし」・`bg-gray-400`・pulse なし。
  - **-60 ≦ delaySec ≦ 60** → 「定刻」・`bg-green-500`・pulse なし（誤差吸収）。
  - **60 < delaySec < 300** → 「+N分遅れ」・`bg-yellow-500`・pulse あり。N は `Math.round(delaySec/60)`。
  - **delaySec ≧ 300** → 「+N分遅れ」・`bg-red-500`・pulse あり。
- **文言**: 「定刻」と「+N分遅れ」のみ。N 分は四捨五入で統一。

### 3) proto 失敗時処理

- **場所**: `loadProto().catch(...)` 内。
- **挙動**: 例外で止めず、`applyViewModels({ orange: null, blue: null })` を実行して両バスを非表示にする。`console.error('[Module C] proto load failed:', err)` でログ出力。画面は壊れない（404 と同等）。

### 4) timer ガード（`startPolling()`）

- **変数**: モジュール直下の `pollTimerId` を保持。
- **挙動**: `startPolling()` の先頭で `if (pollTimerId != null) return;` により、既に `setInterval` が動いていれば二重に開始しない。初回のみ `pollTimerId = setInterval(poll, POLL_MS)` を実行。

---

## 動作確認手順

### feed.pb が 404 の場合

1. `/feed.pb` を返さない（または 404）状態でページを開く。
2. **期待**: proto は読め、初回 poll で feed 取得に失敗 → `applyViewModels({ orange: null, blue: null })` が呼ばれ、両バスが非表示になる。エラーで止まらない。

### proto が 404 の場合

1. `proto/gtfs-realtime.proto` を削除またはリネームし、該当 URL が 404 になるようにする。
2. ページを開く。
3. **期待**: `loadProto()` が reject し、`catch` で `applyViewModels({ orange: null, blue: null })` が実行され、両バスが非表示。コンソールに `[Module C] proto load failed:` が出力される。画面は壊れない。

### entity 順を入れ替えた場合にマーカーが安定すること

1. ダミー feed.pb を2台分用意する（例: entity A に route_id "route_1"、entity B に route_id "route_2"）。
2. 一度目: entity 順が [A, B] の feed を配信 → オレンジに route_1、ブルーに route_2 が表示されることを確認。
3. 二度目: 同じ内容で entity 順を [B, A] にした feed を配信（または B 側の backend の並び順を変更）。
4. **期待**: オレンジは route_1、ブルーは route_2 のまま。並び順が変わっても色が入れ替わらない。
5. **route_id なしで確認**: 両 entity の route_id を外し、vehicle_id のみにする。1回目で「1台目→orange、2台目→blue」が記録され、2回目で entity 順を入れ替えても、同じ vehicle_id が同じ色のままになることを確認。

---

## route_colors.json（オプション）

- **パス**: `frontend/assets/route_colors.json`（配信時は `/assets/route_colors.json`）。
- **形式例**:
  ```json
  {
    "route_1": { "bg": "#f97316", "text": "#ffffff" },
    "route_2": { "bg": "#3b82f6", "text": "#ffffff" }
  }
  ```
- **挙動**: 存在する場合のみ fetch し、`route_id` → `{ bg, text }` をバッジ・マーカー色に反映。無い場合や 404 の場合は従来どおりオレンジ/ブルーでフォールバック。
