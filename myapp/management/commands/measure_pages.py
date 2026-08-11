"""主要画面の応答時間・クエリ数を実測する。

データ投入のあとに使う。SQLite は件数が増えても SQL 自体は速いままなので、
**SQL 時間だけを見ても遅さに気づけない**（応答4.8秒のうち SQL は 38ms だった
ことがある）。応答時間とクエリ数を並べて見るための道具。

    docker compose exec web python manage.py measure_pages
    MSYS_NO_PATHCONV=1 docker compose exec web python manage.py measure_pages --profile /games/

--profile に渡す URL は先頭がスラッシュなので、Git Bash では `MSYS_NO_PATHCONV=1` を
前置しないと Windows パスに変換されて 404 になる。
"""

from __future__ import annotations

import cProfile
import pstats
import time
from typing import Any

from django.core.management.base import BaseCommand
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from myapp.infrastructure import orm_models

# 応答時間の目安。超えたら印を付ける（絶対の基準ではなく、気づくための線）
SLOW_MS = 500
VERY_SLOW_MS = 1500


class Command(BaseCommand):
    help = "主要画面の応答時間・SQL時間・クエリ数を実測する"

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--profile",
            metavar="URL",
            help="この URL だけを cProfile にかけ、重い関数を出す",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        targets = self._targets()
        if not targets:
            self.stderr.write("試合・チームのデータが無いため測れません。")
            return

        if options.get("profile"):
            self._profile(options["profile"])
            return

        client = Client(SERVER_NAME="localhost")
        self.stdout.write(f"{'画面':22} {'状態':>4} {'応答':>9} {'SQL':>8} {'クエリ':>7}")
        self.stdout.write("-" * 58)

        results = []
        for label, url in targets:
            with CaptureQueriesContext(connection) as captured:
                started = time.perf_counter()
                response = client.get(url)
                elapsed_ms = (time.perf_counter() - started) * 1000
            sql_ms = sum(float(q["time"]) for q in captured.captured_queries) * 1000
            queries = len(captured.captured_queries)
            results.append((label, url, elapsed_ms, queries))

            mark = "!!" if elapsed_ms >= VERY_SLOW_MS else "!" if elapsed_ms >= SLOW_MS else ""
            self.stdout.write(
                f"{label:22} {response.status_code:>4} {elapsed_ms:>7.0f}ms {sql_ms:>6.0f}ms {queries:>6} {mark}"
            )

        slow = [r for r in results if r[2] >= SLOW_MS]
        if slow:
            self.stdout.write("")
            self.stdout.write(f"{SLOW_MS}ms を超えた画面（--profile URL で中身を見る）:")
            for label, url, elapsed_ms, queries in sorted(slow, key=lambda r: -r[2]):
                self.stdout.write(f"  {elapsed_ms:>7.0f}ms {queries:>4}クエリ  {label}  {url}")

    @staticmethod
    def _targets() -> list[tuple[str, str]]:
        """実在する id を拾って測る URL を組む。"""
        stint = orm_models.PlayerStint.objects.select_related("player", "team").first()
        game = orm_models.Game.objects.order_by("-played_on").first()
        league = orm_models.League.objects.filter(teams__isnull=False).first()
        if stint is None or game is None or league is None:
            return []

        team_id, player_id, year = stint.team_id, stint.player_id, game.year
        return [
            ("ダッシュボード", "/"),
            ("チーム一覧", "/teams/"),
            ("順位表", "/standings/"),
            (f"順位表({year})", f"/standings/{year}/"),
            ("選手一覧(野手)", f"/team/{team_id}/?pos=batter"),
            ("選手一覧(投手)", f"/team/{team_id}/?pos=pitcher"),
            ("選手詳細", f"/team/{team_id}/player/{player_id}/"),
            ("選手検索", "/players/?q=田"),
            ("リーグ詳細", f"/league/{league.id}/"),
            ("リーグ成績", f"/league/{league.id}/stats/?pos=batter"),
            ("リーグタイトル", f"/league/{league.id}/titles/"),
            ("試合一覧", "/games/"),
            ("試合詳細", f"/games/{game.id}/"),
        ]

    def _profile(self, url: str) -> None:
        """1画面の中身を見る。自分の実行時間が長い順に myapp 側の関数を出す。"""
        client = Client(SERVER_NAME="localhost")
        profiler = cProfile.Profile()
        profiler.enable()
        response = client.get(url)
        profiler.disable()

        stats = pstats.Stats(profiler)
        # total_tt と stats は pstats が実際に持つ属性だが、型スタブに載っていない。
        # print_stats() の出力を文字列で解析するより素直なので、そのまま使う
        total = stats.total_tt  # type: ignore[attr-defined]
        self.stdout.write(f"{url} → {response.status_code}  合計 {total:.2f}s（計測分を含む）")
        self.stdout.write("")

        rows = []
        for (path, lineno, name), (_cc, calls, own, cumulative, _callers) in stats.stats.items():  # type: ignore[attr-defined]
            where = path.split("/app/")[-1] if "/app/" in path else path.split("site-packages/")[-1]
            rows.append((own, cumulative, calls, f"{where}:{lineno}({name})"))
        for own, cumulative, calls, where in sorted(rows, reverse=True)[:15]:
            self.stdout.write(f"  自 {own:6.2f}s  累 {cumulative:6.2f}s  {calls:>8}回  {where}")
