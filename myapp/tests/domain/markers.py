"""domain のテストに付ける印。

`slow` は、数秒以上かかる統計・母集団・シミュレーションのテストに付ける。環境変数 `SKIP_SLOW_TESTS=1` のときだけ外れる
（`make test-domain-fast`。実装中に domain を何度も流すとき用）。フルスイートと CI では外さない（#385）。

付ける目安はクラス（準備を含む）で約1秒以上。1本ずつの時間は
`python -m unittest discover -s myapp/tests/domain -t . --durations 20` で出る（クラスの準備の時間は含まない）。
"""

import os
import unittest

slow = unittest.skipIf(
    os.environ.get("SKIP_SLOW_TESTS") == "1",
    "SKIP_SLOW_TESTS=1 のため外した（数秒かかる統計のテスト。フルスイートと CI では流れる）",
)
