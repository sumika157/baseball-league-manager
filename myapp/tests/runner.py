"""テストランナー。標準の `DiscoverRunner` に、テストクラスごとの所要時間の一覧を足す。

`manage.py test --slowest-classes 20` で、終わった後に所要時間の長いクラスを上から20件出す。
Python 標準の `--durations` はテストメソッド単位で、`setUpClass`・`setUpTestData`（クラスで1回の
重い準備）の時間が入らない。遅さの多くはそこに出るので、クラス単位で測る。

クラスの時間は「前のテストが終わってから、そのクラスの最後のテストが終わるまで」の壁時計。
`setUpClass`・`setUpTestData` を含み、前のクラスの `tearDownClass` も少し混ざる（並べ替えの目安には足りる）。
`setUpClass`・`setUpTestData` が失敗したクラスはテストが1件も終わらないので、その時間は次のクラスに計上される。
並列実行（`--parallel`）では結果が子プロセスから届くため測れない（その場合は一覧を出さない）。
"""

import sys
import time
import unittest
from collections import defaultdict
from typing import Any

from django.test.runner import DiscoverRunner
from django.test.utils import override_settings


class _ClassTimingResult(unittest.TextTestResult):
    """テストが終わるたびに、前のテストの終わりからの時間をそのクラスに積む。"""

    class_seconds: dict[str, float]
    _last_stop: float | None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.class_seconds = defaultdict(float)
        self._last_stop = None

    def startTestRun(self) -> None:
        super().startTestRun()
        self._last_stop = time.perf_counter()

    def stopTest(self, test: unittest.TestCase) -> None:
        super().stopTest(test)
        now = time.perf_counter()
        if self._last_stop is not None:
            label = f"{type(test).__module__}.{type(test).__qualname__}"
            self.class_seconds[label] += now - self._last_stop
        self._last_stop = now


class TimingDiscoverRunner(DiscoverRunner):
    """`--slowest-classes N` を足した `DiscoverRunner`。指定しなければ標準と同じに動く。"""

    def __init__(self, slowest_classes: int = 0, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.slowest_classes = slowest_classes

    @classmethod
    def add_arguments(cls, parser: Any) -> None:
        super().add_arguments(parser)
        parser.add_argument(
            "--slowest-classes",
            type=int,
            default=0,
            metavar="N",
            help="所要時間の長いテストクラスを上から N 件表示する（0 は表示しない）。",
        )

    def setup_test_environment(self, **kwargs: Any) -> None:
        super().setup_test_environment(**kwargs)
        # 既定の PBKDF2 は1回のハッシュに約0.1秒かかる（反復100万回）。テストはユーザーを何百回も作るので、
        # ここだけ速いハッシュにする。パスワードの強さはテストの関心ではなく、本番の設定は変えない。
        self._fast_hasher = override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
        self._fast_hasher.enable()

    def teardown_test_environment(self, **kwargs: Any) -> None:
        self._fast_hasher.disable()
        super().teardown_test_environment(**kwargs)

    def get_resultclass(self) -> Any:
        if self.slowest_classes and self.parallel <= 1:
            # --debug-sql・--pdb と併用したときはそちらを優先する（測るのは単独で使う想定）。
            return super().get_resultclass() or _ClassTimingResult
        return super().get_resultclass()

    def suite_result(self, suite: Any, result: Any, **kwargs: Any) -> int:
        if isinstance(result, _ClassTimingResult):
            ranked = sorted(result.class_seconds.items(), key=lambda item: item[1], reverse=True)
            total = sum(result.class_seconds.values())
            # unittest の結果と同じ標準エラーに出す（print は ruff の T201 が止める）
            sys.stderr.write(f"\n所要時間の長いテストクラス（上位 {self.slowest_classes} 件・全体 {total:.1f}秒）:\n")
            for label, seconds in ranked[: self.slowest_classes]:
                sys.stderr.write(f"{seconds:8.2f}s  {seconds / total:5.1%}  {label}\n")
        return super().suite_result(suite, result, **kwargs)
