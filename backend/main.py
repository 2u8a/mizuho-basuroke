# Cloud Functions for Firebase (Python)
# 瑞穂町バスロケーション PoC - Module B
# デプロイ: firebase deploy --only functions

from firebase_functions import https_fn
from firebase_functions.options import set_global_options
from firebase_admin import initialize_app

set_global_options(max_instances=10)

# initialize_app()
#
# 実装予定:
# - 運行判定 (B-02)、遅延計算 (B-03)、GTFS-RT 生成 (B-04)
# - 生成した GTFS-RT のログは実証実験終了時のデータまとめ用に保存すること

