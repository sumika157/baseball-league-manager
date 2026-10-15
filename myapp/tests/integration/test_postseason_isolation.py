"""ポストシーズンの試合が公式戦の集計に混ざらないことの検査（#39 段階 S1）。

ポストシーズンの試合は普通の `Game`（`Game.stage` がポストシーズン）として保存する。公式戦の成績・
順位・タイトル・FA の日数・戦力分析・オフの出場機会は**ポストシーズンを数えない**（NPB と同じ）。
試合を読む入口（`scoping.games_in` など）が区分を必須の引数で受け取り、呼ぶ側が決める。
**どこか1か所の区分の入れ忘れは、例外にならずに数字だけが変わる**ので、極端な成績のポストシーズンの
試合を世界に直接足し、世界の画面と参照クエリが1文字も変わらないことで機械的に捕まえる。

逆に、試合そのもの（試合詳細・保存）・世界の今日・直近の登板（疲労）・世界の削除は区分を問わない。
"""

import ast
import re
from datetime import date
from pathlib import Path

from django.contrib.auth.models import User
from django.test import SimpleTestCase
from django.urls import reverse

from myapp.application.dto import LineupSlot
from myapp.application.game_recording import GameRecordingService
from myapp.domain import services as domain_services
from myapp.domain.entities import Game
from myapp.domain.value_objects import FieldingPosition, GameHeader, GameStage, LineupEntry, Season
from myapp.infrastructure import orm_models
from myapp.infrastructure.queries import (
    DjangoFieldingTotalsQuery,
    DjangoGameListQuery,
    DjangoHomeStadiumQuery,
    DjangoPennantActivityQuery,
    DjangoPlayerFieldingQuery,
    DjangoPlayerStatsQuery,
    DjangoSeasonPlayingTimeQuery,
    DjangoSimulationContextQuery,
    DjangoTeamAnalysisQuery,
    DjangoWorldSummaryQuery,
)
from myapp.infrastructure.repositories import (
    DjangoGameRepository,
    DjangoLeagueRepository,
    DjangoTeamRepository,
    DjangoWorldRepository,
    batting_totals,
)
from myapp.infrastructure.scoping import StageFilter

from ..helpers import build_scorebook, to_plate_appearances
from .world_case import YEAR, WorldCase

# 世界の最後の公式戦の日（`WorldCase` の試合）。同じ日に足せば世界の今日は動かない
LAST_REGULAR_DAY = date(YEAR, 4, 2)
# 足すポストシーズンの試合の得点（回ごと）。公式戦の 2-5 と桁違いにして、混ざれば必ず数字が変わるようにする
POSTSEASON_AWAY_RUNS = [7]
POSTSEASON_HOME_RUNS = [20]

_CSRF = re.compile(r'name="csrfmiddlewaretoken" value="[^"]*"')


class PostseasonCase(WorldCase):
    """`WorldCase` の世界に、ポストシーズンの試合を直接作る道具。"""

    def postseason_game(self, played_on: date, *, year: int = YEAR) -> Game:
        """打席まで揃ったポストシーズンの試合（未保存）。本塁打だけで点が入る極端な試合。"""
        home = self.pennant_players(self.pennant_team)  # 背番号 1〜9 が打者、18 が投手
        away = self.pennant_players(self.pennant_rival)
        header = GameHeader(
            season=Season(year),
            played_on=played_on,
            home_team_id=self.pennant_team.id,
            away_team_id=self.pennant_rival.id,
            stage=GameStage.POSTSEASON,
        )
        lineup = [
            LineupEntry(
                team_id=team_id,
                player_id=player_id,
                batting_order=order,
                slot_sequence=0,
                fielding_position=FieldingPosition.DESIGNATED_HITTER,
            )
            for team_id, batters in ((self.pennant_team.id, home[:9]), (self.pennant_rival.id, away[:9]))
            for order, player_id in enumerate(batters, start=1)
        ]
        plate_appearances = to_plate_appearances(
            build_scorebook(
                away=POSTSEASON_AWAY_RUNS,
                home=POSTSEASON_HOME_RUNS,
                away_batters=away[:9],
                home_batters=home[:9],
                away_pitchers={1: away[9]},
                home_pitchers={1: home[9]},
            )
        )
        return domain_services.assemble_game(header, lineup, plate_appearances)

    def add_postseason_game(self, played_on: date, *, year: int = YEAR) -> Game:
        return DjangoGameRepository(self.scope).save(self.postseason_game(played_on, year=year))


class PostseasonGamesDoNotCountTest(PostseasonCase):
    """ポストシーズンの試合を足しても、公式戦の集計（画面と参照クエリ）は変わらない。"""

    def _pages(self, urls: list[str]) -> dict[str, str]:
        pages = {}
        for url in urls:
            response = self.client.get(url, follow=True)
            self.assertEqual(response.status_code, 200, url)
            # CSRF のトークンは要求ごとに変わるので、比べる前に外す
            pages[url] = _CSRF.sub("", response.content.decode())
        return pages

    def _facts(self) -> dict[str, object]:
        """公式戦だけを数える参照クエリとリポジトリの答え。"""
        scope = self.scope
        team, rival = self.pennant_team.id, self.pennant_rival.id
        batter = self.pennant_home_batters[0]
        players = self.pennant_players(self.pennant_team) + self.pennant_players(self.pennant_rival)
        stats = DjangoPlayerStatsQuery(scope)
        games = DjangoGameListQuery(scope)
        activity = DjangoPennantActivityQuery(scope)
        analysis = DjangoTeamAnalysisQuery(scope)
        repository = DjangoGameRepository(scope)
        whole_year = {"after": date(YEAR - 1, 12, 31), "through": date(YEAR, 12, 31)}
        roster = DjangoTeamRepository(scope).find_by_id(team).players
        return {
            "成績（その年・世界の全員）": stats.list_roster(year=YEAR, with_games=True),
            "成績（通算・世界の全員）": stats.list_roster(year=None, with_games=True),
            "成績（その年・球団）": stats.list_roster(year=YEAR, team_id=team, with_games=True),
            "成績（通算・球団）": stats.list_roster(year=None, team_id=team, with_games=True),
            "成績（リーグ）": stats.list_roster(year=YEAR, league_id=self.pennant_league.id, with_games=True),
            "成績（リーグごと）": stats.list_season_by_league(YEAR),
            "守備（選手ページ）": DjangoPlayerFieldingQuery(scope).for_player(batter, team),
            "守備（通算）": DjangoFieldingTotalsQuery(scope).totals_for(players),
            "試合一覧": games.list_rows(year=YEAR),
            "順位の材料": games.list_for_standings(year=YEAR),
            "選手の試合": games.list_for_player(team, batter),
            "年の選択肢": games.list_seasons(),
            "記録のある年": games.list_seasons(recorded_only=True),
            "規定の試合数": games.count_by_team(year=YEAR),
            "月の選択肢": games.list_months(year=YEAR),
            "最新の年": games.latest_year(),
            "期間の打撃": activity.batting_between(team, **whole_year),
            "期間の投球": activity.pitching_between(team, **whole_year),
            "オフの出場機会": DjangoSeasonPlayingTimeQuery(scope).for_year(YEAR),
            "戦力分析の年": analysis.list_years(team),
            "戦力分析の起用": analysis.load(team, YEAR),
            "FA の日数": analysis.load_service_history(players, YEAR),
            "選手の通算（集約）": sorted((p.id or 0, p.batting, p.pitching) for p in roster),
            "集約の試合": [game.id for game in repository.find_all(YEAR)],
            "球団の試合（集約）": [game.id for game in repository.find_by_team(team, YEAR)],
            "対戦の試合（集約）": [game.id for game in repository.find_between_teams({team, rival}, YEAR)],
        }

    def test_the_postseason_game_would_change_the_numbers_if_it_counted(self):
        """前提の確認。足す試合は、数えれば成績が必ず変わる（変わらないことの検査が空振りしない）。"""
        self.add_postseason_game(LAST_REGULAR_DAY)

        every_stage = batting_totals(self.scope, stages=StageFilter.ALL_STAGES)
        regular = batting_totals(self.scope, stages=StageFilter.REGULAR_ONLY)

        home_runs = sum(line.home_runs for line in every_stage.values()) - sum(
            line.home_runs for line in regular.values()
        )
        self.assertEqual(home_runs, sum(POSTSEASON_AWAY_RUNS) + sum(POSTSEASON_HOME_RUNS))

    def test_the_queries_do_not_change(self):
        before = self._facts()

        self.add_postseason_game(LAST_REGULAR_DAY)

        after = self._facts()
        for name, value in before.items():
            with self.subTest(name):
                self.assertEqual(after[name], value)

    def test_the_world_pages_do_not_change(self):
        """順位表・タイトル・成績一覧・選手ページ・試合一覧・GM ホーム・戦力分析・歴代・球団史。

        GM ホームは受け持ちの球団の期間の集計まで出るよう、オーナーとして開く。
        """
        owner = User.objects.create_user("gm", password="x")
        orm_models.PennantWorld.objects.filter(id=self.world_id).update(
            owner_id=owner.id, managed_team_id=self.pennant_team.id
        )
        self.client.force_login(owner)
        urls = [*self.world_urls(), reverse("pennant_world", args=[self.world_id]) + f"?since={YEAR}-04-01"]
        self.assertGreaterEqual(len(urls), 20, "画面の一覧を拾えていません")
        before = self._pages(urls)

        self.add_postseason_game(LAST_REGULAR_DAY)

        after = self._pages(urls)
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(after[url], before[url])

    def test_a_postseason_game_in_another_year_adds_no_season(self):
        """年の選択肢・既定の年はポストシーズンの試合だけの年を拾わない。"""
        games = DjangoGameListQuery(self.scope)

        def seasons() -> tuple[object, ...]:
            return (games.list_seasons(), games.latest_year(), games.list_months(), games.count_by_team())

        before = seasons()

        self.add_postseason_game(date(YEAR + 1, 10, 20), year=YEAR + 1)

        self.assertEqual(seasons(), before)
        self.assertEqual(DjangoGameRepository(self.scope).find_all(YEAR + 1), [])
        self.assertEqual(DjangoTeamAnalysisQuery(self.scope).list_years(self.pennant_team.id), [YEAR])


class PostseasonGamesAreStillGamesTest(PostseasonCase):
    """試合そのもの・世界の今日・直近の登板・削除は、区分を問わない。"""

    def test_the_game_detail_opens(self):
        game = self.add_postseason_game(LAST_REGULAR_DAY)

        response = self.client.get(reverse("pennant_game_detail", args=[self.world_id, game.id]))

        self.assertEqual(response.status_code, 200)

    def test_save_writes_the_stage_and_find_by_id_reads_it_back(self):
        repository = DjangoGameRepository(self.scope)
        game = self.add_postseason_game(LAST_REGULAR_DAY)
        assert game.id is not None

        self.assertEqual(orm_models.Game.objects.get(id=game.id).stage, "ポストシーズン")
        read = repository.find_by_id(game.id)
        self.assertIs(read.stage, GameStage.POSTSEASON)
        self.assertEqual((read.home_score, read.away_score), (sum(POSTSEASON_HOME_RUNS), sum(POSTSEASON_AWAY_RUNS)))

    def test_add_all_writes_the_same_stage_as_save(self):
        """一括の書き込み（`prepare_add_all`。ペナントの進行が使う）も区分を書く。"""
        repository = DjangoGameRepository(self.scope)
        game = self.postseason_game(date(YEAR, 10, 20))

        repository.add_all([game])

        assert game.id is not None
        self.assertIs(repository.find_by_id(game.id).stage, GameStage.POSTSEASON)

    def test_a_regular_game_stays_regular(self):
        assert self.pennant_game_id is not None
        self.assertIs(DjangoGameRepository(self.scope).find_by_id(self.pennant_game_id).stage, GameStage.REGULAR)

    def test_overwriting_with_the_scorebook_keeps_the_stage(self):
        """スコアブックの上書きは区分を入力に持たない。今の試合の区分を引き継ぐ（公式戦に戻さない）。"""
        game = self.add_postseason_game(LAST_REGULAR_DAY)
        assert game.id is not None
        home = self.pennant_players(self.pennant_team)
        away = self.pennant_players(self.pennant_rival)
        recording = GameRecordingService(
            games=DjangoGameRepository(self.scope),
            teams=DjangoTeamRepository(self.scope),
            leagues=DjangoLeagueRepository(self.scope),
            home_stadiums=DjangoHomeStadiumQuery(self.scope),
        )

        recording.record_scorebook(
            game.id,
            year=YEAR,
            played_on=LAST_REGULAR_DAY,
            home_team_id=self.pennant_team.id,
            away_team_id=self.pennant_rival.id,
            lineup=[
                LineupSlot(
                    team_id=team_id,
                    player_id=player_id,
                    batting_order=order,
                    slot_sequence=0,
                    fielding_position=FieldingPosition.DESIGNATED_HITTER,
                )
                for team_id, batters in ((self.pennant_team.id, home[:9]), (self.pennant_rival.id, away[:9]))
                for order, player_id in enumerate(batters, start=1)
            ],
            plate_appearances=to_plate_appearances(
                build_scorebook(
                    away=[1],
                    home=[0],
                    away_batters=away[:9],
                    home_batters=home[:9],
                    away_pitchers={1: away[9]},
                    home_pitchers={1: home[9]},
                )
            ),
            times=None,
            attendance=None,
        )

        read = DjangoGameRepository(self.scope).find_by_id(game.id)
        self.assertEqual((read.home_score, read.away_score), (0, 1), "上書きされている")
        self.assertIs(read.stage, GameStage.POSTSEASON)

    def test_the_world_today_counts_the_postseason(self):
        """世界の今日（最後の試合日）はポストシーズンの試合も含めて決まる。"""
        played_on = date(YEAR, 10, 20)
        self.add_postseason_game(played_on)

        self.assertEqual(DjangoSimulationContextQuery(self.scope).last_played_on(), played_on)
        self.assertEqual(DjangoWorldSummaryQuery().get(self.world_id).last_played_on, played_on)

    def test_recent_pitching_counts_the_postseason(self):
        """直近の先発・登板（疲労）はポストシーズンで投げた分も数える。"""
        played_on = date(YEAR, 10, 20)
        self.add_postseason_game(played_on)
        starters = {self.pennant_players(self.pennant_team)[9], self.pennant_players(self.pennant_rival)[9]}

        context = DjangoSimulationContextQuery(self.scope).load(before=date(YEAR, 10, 21))

        self.assertEqual({start.pitcher_id for start in context.last_starts if start.played_on == played_on}, starters)
        self.assertIn(played_on, [outing.played_on for outing in context.recent_outings])

    def test_game_notes_cover_a_postseason_game(self):
        """試合の見どころは渡した試合の id で決まる（区分で絞らない）。"""
        game = self.add_postseason_game(LAST_REGULAR_DAY)
        assert game.id is not None

        notes = DjangoPennantActivityQuery(self.scope).game_notes([game.id])

        self.assertEqual(len(notes[game.id].home_runs), 9 + 7, "ホームの9人全員とビジターの7人が本塁打を打っている")

    def test_deleting_the_world_removes_the_postseason_games(self):
        game = self.add_postseason_game(LAST_REGULAR_DAY)

        DjangoWorldRepository().delete(self.world_id)

        self.assertFalse(orm_models.Game.objects.filter(id=game.id).exists())
        self.assertFalse(orm_models.GameBattingLine.objects.filter(game_id=game.id).exists())


# 試合と明細のモデル。これを `.objects` で読む関数は、区分を指定しているか、許可リストに理由があること
_GAME_MODELS = frozenset(
    {"Game", "GameBattingLine", "GamePitchingLine", "GameFieldingLine", "GamePlateAppearance", "GameInningScore"}
)
# 関数の中に区分の指定があるか（`games_in(..., stages=...)`・`batting_totals(..., stages=...)` など）
_STAGE_MARKERS = ("stages=", "stage_condition(", "game_rows_condition(")
_REPOSITORIES = "infrastructure/repositories.py"
_SEED = "management/commands/seed_virtual_games.py"
# 区分を指定せずに試合の行を `.objects` で触ってよい関数（ファイル, クラス名つきの関数名）と、その理由
_ALLOWED_WITHOUT_STAGE = {
    (_REPOSITORIES, "DjangoGameRepository._plate_appearance_prefetch"): "find_by_id が選んだ試合の打席を引く",
    (_REPOSITORIES, "DjangoGameRepository.save"): "1試合の書き込み（行は保存する試合に結び付く）",
    (_REPOSITORIES, "DjangoGameRepository._insert_new"): "新しい試合の書き込み",
    (_REPOSITORIES, "DjangoGameRepository._save_fielding"): "1試合の書き込み",
    (_REPOSITORIES, "DjangoGameRepository._save_line_score"): "1試合の書き込み",
    (_REPOSITORIES, "DjangoGameRepository._save_plate_appearances"): "1試合の書き込み",
    (_REPOSITORIES, "DjangoGameRepository._create_plate_appearances"): "書き込んだ試合の打席の書き込み",
    ("infrastructure/queries.py", "recorded_games_filter"): "試合の行ごとの副問い合わせ（OuterRef で1試合に結び付く）",
    (_SEED, "Command._save"): "実データの試合の書き込み",
    (_SEED, "Command._save_advances_and_errors"): "書き込んだ試合の打席の読み直し",
}


def _source_files() -> list[tuple[str, str]]:
    """myapp の本体のソース（テスト・マイグレーション・`scoping.py` を除く）を (myapp からの相対パス, 中身) で。"""
    root = Path(__file__).resolve().parents[2]
    return [
        (path.relative_to(root).as_posix(), path.read_text(encoding="utf-8"))
        for path in sorted(root.rglob("*.py"))
        if not {"tests", "migrations"} & set(path.relative_to(root).parts) and path.name != "scoping.py"
    ]


def _found_by_function(source: str, wanted) -> list[tuple[str, str]]:
    """`wanted(node)` に当たる式を、いちばん内側の関数ごとに (クラス名つきの関数名, 関数のソース) で集める。"""
    found: list[tuple[str, str]] = []

    def visit(node: ast.AST, owner: str, function: ast.AST | None, name: str) -> None:
        if isinstance(node, ast.ClassDef):
            owner = f"{owner}{node.name}."
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            function, name = node, f"{owner}{node.name}"
        if wanted(node):
            body = ast.get_source_segment(source, function) if function is not None else None
            found.append((name, body or ""))
        for child in ast.iter_child_nodes(node):
            visit(child, owner, function, name)

    visit(ast.parse(source), "", None, "<module>")
    return found


def _is_game_manager(node: ast.AST) -> bool:
    """`Game.objects` / `orm_models.GameBattingLine.objects` のような、試合のモデルの `.objects`。"""
    if not (isinstance(node, ast.Attribute) and node.attr == "objects"):
        return False
    owner = node.value
    name = owner.id if isinstance(owner, ast.Name) else owner.attr if isinstance(owner, ast.Attribute) else ""
    return name in _GAME_MODELS


def _is_world_only_rows(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "game_rows_in_world"


class GameRowsGoThroughTheStageTest(SimpleTestCase):
    """試合を読むときは、区分を必ず指定する（入れ忘れはエラーにならず、数字だけが変わるため）。

    ソースを機械的に見る。**限界**: 関数の単位で判定する（関数の中で区分を1か所でも指定していれば通る）。
    `model._default_manager` 経由の読み（`repositories._line_rows`）と、逆向きの関連（`player.game_batting`
    など）は見ない。区分の値が正しいかと、これらの経路は、上の画面と参照クエリの比較が見る。
    """

    def test_no_code_reads_game_rows_by_the_world_alone(self):
        """`world_condition("game__…")` を直接書かない（範囲だけで読む世界の削除は、関連名を変数で組み立てる）。"""
        offenders = [
            path
            for path, source in _source_files()
            if re.search(r'world_condition\(\s*f?"(plate_appearance__)?game', source)
        ]
        self.assertEqual(offenders, [])

    def test_every_read_of_the_game_tables_names_a_stage(self):
        offenders = [
            f"{path}: {name}"
            for path, source in _source_files()
            for name, body in _found_by_function(source, _is_game_manager)
            if (path, name) not in _ALLOWED_WITHOUT_STAGE and not any(marker in body for marker in _STAGE_MARKERS)
        ]
        self.assertEqual(offenders, [], "区分（stages / stage_condition）を指定せずに試合の行を読んでいます")

    def test_the_allow_list_has_no_stale_entries(self):
        """許可リストの関数が消えたり区分を指定するようになったら、リストからも外す（古い許可を残さない）。"""
        used = {
            (path, name)
            for path, source in _source_files()
            for name, body in _found_by_function(source, _is_game_manager)
            if not any(marker in body for marker in _STAGE_MARKERS)
        }
        self.assertEqual(set(_ALLOWED_WITHOUT_STAGE) - used, set())

    def test_world_only_rows_are_used_only_by_the_totals(self):
        """範囲だけの条件（`game_rows_in_world`）は、区分を必須で足す成績の集計の入口（`_line_rows`）だけが使う。"""
        users = {
            (path, name)
            for path, source in _source_files()
            for name, _ in _found_by_function(source, _is_world_only_rows)
        }
        self.assertEqual(users, {(_REPOSITORIES, "_line_rows")})
