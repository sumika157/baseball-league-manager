"""主要画面の応答時間・クエリ数を実測する。

データ投入のあとに使う。SQLite は件数が増えても SQL 自体は速いままなので、
**SQL 時間だけを見ても遅さに気づけない**（応答4.8秒のうち SQL は 38ms だった
ことがある）。応答時間とクエリ数を並べて見るための道具。

    docker compose exec web python manage.py measure_pages --workspace <key>
    docker compose exec web python manage.py measure_pages --workspace <key> --as admin --world 1
    MSYS_NO_PATHCONV=1 docker compose exec web python manage.py measure_pages --workspace <key> --profile /games/

- `--workspace <key>`（必須）: 測るワークスペース。画面の id（チーム・選手・試合・リーグ）はその実データから拾う。
- `--as <ユーザー名>`: ログインして測る（省けば未ログイン）。権限で出し分ける画面のクエリ数は、ログインした人で変わる。
- `--world <id>`: そのワークスペースのペナントの世界の10画面も測る。

測る URL は `reverse` で組む（`_url`）。W2 T3（#78）の時点では今の URL（`/teams/` など）のままで、今の URL が
開くのは id が最小のワークスペースだけ（ほかのワークスペースを指すと、拾った id が見つからず 404 になる）。
T4 で URL に key が入る（`_url` が key を先頭の引数に足す）。

--profile に渡す URL は先頭がスラッシュなので、Git Bash では `MSYS_NO_PATHCONV=1` を
前置しないと Windows パスに変換されて 404 になる。
"""

from __future__ import annotations

import cProfile
import pstats
import time
from typing import Any

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from myapp.domain.pennant.world import WorldScope
from myapp.infrastructure.scoping import games_in, leagues_in, stints_in, teams_in
from myapp.management.workspaces import add_workspace_argument, real_scope_for_key, world_scope

# 応答時間の目安。超えたら印を付ける（絶対の基準ではなく、気づくための線）
SLOW_MS = 500
VERY_SLOW_MS = 1500


def _url(key: str, name: str, *args: object) -> str:
    """ワークスペース key の中の画面の URL。T3 の時点では `reverse` と同じで、T4 で key を先頭の引数に足す。"""
    return reverse(name, args=args)


class Command(BaseCommand):
    help = "主要画面の応答時間・SQL時間・クエリ数を実測する"

    def add_arguments(self, parser: Any) -> None:
        add_workspace_argument(parser, help="測るワークスペースの key（必須）")
        parser.add_argument(
            "--as",
            dest="username",
            default=None,
            metavar="USERNAME",
            help="このユーザーでログインして測る（省けば未ログイン）",
        )
        parser.add_argument(
            "--world",
            type=int,
            default=None,
            metavar="ID",
            help="このペナントの世界の画面も測る（--workspace のワークスペースの世界）",
        )
        parser.add_argument(
            "--profile",
            metavar="URL",
            help="この URL だけを cProfile にかけ、重い関数を出す",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        key = options["workspace"]
        scope = real_scope_for_key(key)
        world = None if options["world"] is None else self._world_scope(scope, options["world"])
        client = self._client(options["username"])

        if options.get("profile"):
            self._profile(client, options["profile"])
            return

        targets = self._targets(scope, key)
        if not targets:
            self.stderr.write("試合・チームのデータが無いため測れません。")
            return
        if world is not None:
            targets += self._world_targets(world, key)

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
    def _client(username: str | None) -> Client:
        """測るときの利用者。`--as` を省けば未ログイン。無いユーザー名は日本語の `CommandError`。"""
        client = Client(SERVER_NAME="localhost")
        if username is None:
            return client
        user = get_user_model().objects.filter(username=username).first()
        if user is None:
            raise CommandError(f"ユーザーが見つかりません: {username}")
        client.force_login(user)
        return client

    @staticmethod
    def _world_scope(real: WorldScope, world_id: int) -> WorldScope:
        """`--world` の世界の範囲。`--workspace` のワークスペースの世界でなければ `CommandError`（混ぜて測らない）。"""
        scope = world_scope(world_id)
        if scope.workspace_id != real.workspace_id:
            raise CommandError(f"世界 {world_id} は、指定したワークスペースの世界ではありません。")
        return scope

    @staticmethod
    def _targets(scope: WorldScope, key: str) -> list[tuple[str, str]]:
        """実データの16画面。実在する id を拾って測る URL を組む。"""
        # ペナントの世界の行を拾うと、実データの画面を測ったことにならない
        stint = stints_in(scope).select_related("player", "team").first()
        game = games_in(scope).order_by("-played_on").first()
        league = leagues_in(scope).filter(teams__isnull=False).first()
        if stint is None or game is None or league is None:
            return []

        team_id, player_id, year = stint.team_id, stint.player_id, game.year
        return [
            ("ダッシュボード", _url(key, "dashboard")),
            ("チーム一覧", _url(key, "team_list")),
            ("順位表", _url(key, "standings")),
            (f"順位表({year})", _url(key, "standings_by_year", year)),
            ("選手一覧(野手)", _url(key, "player_list", team_id) + "?pos=batter"),
            ("選手一覧(投手)", _url(key, "player_list", team_id) + "?pos=pitcher"),
            ("選手詳細", _url(key, "player_detail", team_id, player_id)),
            ("選手検索", _url(key, "player_search") + "?q=田"),
            ("リーグ詳細", _url(key, "league_detail", league.id)),
            ("リーグ成績", _url(key, "league_stats", league.id) + "?pos=batter"),
            ("リーグタイトル", _url(key, "league_titles", league.id)),
            ("試合一覧", _url(key, "game_list")),
            ("試合詳細", _url(key, "game_detail", game.id)),
            ("戦力分析", _url(key, "team_analysis", team_id)),
            ("戦力分析（起用マップ）", _url(key, "team_analysis", team_id) + "?tab=usage"),
            ("戦力分析（FA取得）", _url(key, "team_analysis", team_id) + "?tab=fa"),
        ]

    @staticmethod
    def _world_targets(scope: WorldScope, key: str) -> list[tuple[str, str]]:
        """ペナントの世界の10画面（設計書 14. の W1b T0 の表と同じ並び）。リーグ・球団は世界の最初の行。"""
        world_id = scope.world_id
        league = leagues_in(scope).order_by("id").first()
        team = teams_in(scope).order_by("id").first()
        if world_id is None or league is None or team is None:
            raise CommandError(f"世界 {world_id} にリーグ・球団がありません。")
        return [
            ("ペナント一覧", _url(key, "pennant_index")),
            ("世界ダッシュボード", _url(key, "pennant_world", world_id)),
            ("世界の順位表", _url(key, "pennant_standings", world_id)),
            ("世界のリーグ詳細", _url(key, "pennant_league_detail", world_id, league.id)),
            ("世界のリーグ成績", _url(key, "pennant_league_stats", world_id, league.id) + "?pos=batter"),
            ("世界のチーム一覧", _url(key, "pennant_team_list", world_id)),
            ("世界の選手一覧", _url(key, "pennant_player_list", world_id, team.id) + "?pos=batter"),
            ("世界の試合一覧", _url(key, "pennant_game_list", world_id)),
            ("世界の戦力分析", _url(key, "pennant_team_analysis", world_id, team.id)),
            ("世界の球団ホーム", _url(key, "pennant_club", world_id)),
        ]

    def _profile(self, client: Client, url: str) -> None:
        """1画面の中身を見る。自分の実行時間が長い順に myapp 側の関数を出す。"""
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
