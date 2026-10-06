"""仮想試合の投入コマンド（seed_virtual_games）。

明細を乱数で別々に引くと、スコアと打点・被安打と相手の安打が食い違う。
コマンドは片方から他方を導いて作っているので、保存された試合を集約として
読み戻し、スコアブックとして辻褄が合っているかを確かめる。
"""

from collections import defaultdict
from io import StringIO
from types import SimpleNamespace

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase

from myapp.domain.pennant.world import WorldScope
from myapp.domain.value_objects import FieldingPosition, Position
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoGameRepository
from myapp.management.commands.seed_virtual_games import MAX_INNINGS, Command, _Side

from .base import BaseCase

COMMAND = "seed_virtual_games"
YEAR = 2025
GAMES_PER_PAIR = 3
OUTS_PER_INNING = 3
REGULATION_INNINGS = 9

# 1チームぶんのロスター。先発ローテーション5人＋救援、守備9枠＋控え
ROSTER = (
    [Position.PITCHER] * 8
    + [Position.CATCHER] * 2
    + [Position.INFIELDER] * 5
    + [Position.OUTFIELDER] * 4
    + [Position.DESIGNATED_HITTER]
)


def run(*args) -> str:
    out = StringIO()
    call_command(
        COMMAND, "--seed", "7", "--year", str(YEAR), "--games-per-pair", str(GAMES_PER_PAIR), *args, stdout=out
    )
    return out.getvalue()


class SeedGamesBase(BaseCase):
    def setUp(self) -> None:
        super().setUp()
        # 出場枠1人。投手と野手に1人ずつ外国人を置き、両方は出られない状況にする
        self.league.foreign_player_game_limit = 1
        self.league.save()
        self.third = orm_models.Team.objects.create(league=self.league, name="三番目のチーム")
        self.teams = (self.team, self.rival, self.third)

        self.team_of: dict[int, int] = {}
        self.pitcher_ids: set[int] = set()
        self.foreign_ids: set[int] = set()
        for team in self.teams:
            for index, position in enumerate(ROSTER, start=1):
                is_foreign = index in (1, len(ROSTER))  # 先頭の投手と末尾の指名打者
                self._add(team, f"{team.name}{index}", position, number=index, is_foreign=is_foreign)

        # その年に在籍していない選手は出場させない
        self.departed = self._add(
            self.team, "退団済み", Position.INFIELDER, number=50, from_year=2020, to_year=YEAR - 1
        )
        self.newcomer = self._add(self.team, "翌年入団", Position.PITCHER, number=51, from_year=YEAR + 1)

    def _add(self, team, name, position, *, number, is_foreign=False, from_year=2020, to_year=None):
        player = orm_models.Player.objects.create(name=name, position=position.value, is_foreign_player=is_foreign)
        orm_models.PlayerStint.objects.create(
            player=player, team=team, number=number, from_year=from_year, to_year=to_year
        )
        if from_year <= YEAR and (to_year is None or to_year >= YEAR):
            self.team_of[player.id] = team.id
        if position == Position.PITCHER:
            self.pitcher_ids.add(player.id)
        if is_foreign:
            self.foreign_ids.add(player.id)
        return player


class SeedGamesTest(SeedGamesBase):
    def setUp(self) -> None:
        super().setUp()
        self.output = run()
        self.games = DjangoGameRepository(WorldScope.real()).find_all(YEAR)

    def test_creates_round_robin_within_season(self):
        pairs = len(self.teams) * (len(self.teams) - 1) // 2
        self.assertEqual(len(self.games), pairs * GAMES_PER_PAIR)
        self.assertIn(f"{pairs * GAMES_PER_PAIR}試合を投入しました", self.output)

        cards = defaultdict(int)
        for game in self.games:
            cards[frozenset((game.home_team_id, game.away_team_id))] += 1
            with self.subTest(game=str(game)):
                self.assertEqual(game.played_on.year, YEAR)
                self.assertTrue(4 <= game.played_on.month <= 9)
        self.assertEqual(set(cards.values()), {GAMES_PER_PAIR})

    def test_line_score_matches_final_score(self):
        for game in self.games:
            with self.subTest(game=str(game)):
                game.ensure_line_score_matches()
                away, home = len(game.line_score.away), len(game.line_score.home)
                # 延長は12回まで。ホームがリードしていれば最終回の裏は行わない（サヨナラも裏の途中で終わる）
                self.assertTrue(REGULATION_INNINGS <= away <= MAX_INNINGS, away)
                self.assertIn(home, (away, away - 1))
                if home == away - 1:
                    self.assertGreater(game.home_score, game.away_score)

    def test_runs_batted_in_never_exceed_team_score(self):
        """打点の合計は得点を超えない。失策・野選・暴投による得点は打点にならないので、等しいとは限らない。"""
        for game in self.games:
            rbi = defaultdict(int)
            for entry in game.batting:
                rbi[self.team_of[entry.player_id]] += entry.line.runs_batted_in
            with self.subTest(game=str(game)):
                self.assertLessEqual(rbi[game.home_team_id], game.home_score)
                self.assertLessEqual(rbi[game.away_team_id], game.away_score)

    def test_pitching_mirrors_opponent_batting(self):
        """被安打・被本塁打・与四死球は相手打線の記録と一致し、相手が攻めた回ぶんのアウトを取る。"""
        for game in self.games:
            batting = defaultdict(lambda: defaultdict(int))
            for entry in game.batting:
                side = batting[self.team_of[entry.player_id]]
                side["hits"] += entry.line.hits
                side["home_runs"] += entry.line.home_runs
                side["walks"] += entry.line.walks
                side["hit_by_pitch"] += entry.line.hit_by_pitch
            pitching = defaultdict(lambda: defaultdict(int))
            for entry in game.pitching:
                side = pitching[self.team_of[entry.player_id]]
                side["hits"] += entry.line.hits_allowed
                side["home_runs"] += entry.line.home_runs_allowed
                side["walks"] += entry.line.walks_allowed
                side["hit_by_pitch"] += entry.line.hit_by_pitch_allowed
                side["outs"] += entry.line.innings.outs
                side["earned_runs"] += entry.line.earned_runs

            for team_id, opponent_id, conceded, opponent_halves in (
                (game.home_team_id, game.away_team_id, game.away_score, len(game.line_score.away)),
                (game.away_team_id, game.home_team_id, game.home_score, len(game.line_score.home)),
            ):
                with self.subTest(game=str(game), team=team_id):
                    for key in ("hits", "home_runs", "walks", "hit_by_pitch"):
                        self.assertEqual(pitching[team_id][key], batting[opponent_id][key], key)
                    # 相手が攻めた回はすべて3アウトを取る。ただしサヨナラは最終回の裏の途中で終わる
                    full = opponent_halves * OUTS_PER_INNING
                    walk_off = team_id == game.away_team_id and game.home_score > game.away_score
                    if walk_off:
                        self.assertTrue(full - OUTS_PER_INNING <= pitching[team_id]["outs"] <= full)
                    else:
                        self.assertEqual(pitching[team_id]["outs"], full)
                    self.assertLessEqual(pitching[team_id]["earned_runs"], conceded)

    def test_decisions_follow_result(self):
        for game in self.games:
            wins = defaultdict(int)
            losses = defaultdict(int)
            saves = 0
            for entry in game.pitching:
                wins[self.team_of[entry.player_id]] += entry.line.wins
                losses[self.team_of[entry.player_id]] += entry.line.losses
                saves += entry.line.saves
            with self.subTest(game=str(game)):
                winner = game.winner_team_id
                if winner is None:
                    self.assertEqual((sum(wins.values()), sum(losses.values()), saves), (0, 0, 0))
                    continue
                loser = game.away_team_id if winner == game.home_team_id else game.home_team_id
                self.assertEqual((wins[winner], losses[winner]), (1, 0))
                self.assertEqual((wins[loser], losses[loser]), (0, 1))
                self.assertLessEqual(saves, 1)

    def test_lineup_and_staff_shape(self):
        """各チーム、先発投手は1人・打順は1〜9。投手は投手、野手は野手から出る。"""
        for game in self.games:
            for team_id in (game.home_team_id, game.away_team_id):
                pitchers = [e for e in game.pitching if self.team_of[e.player_id] == team_id]
                batters = [e for e in game.batting if self.team_of[e.player_id] == team_id]
                with self.subTest(game=str(game), team=team_id):
                    self.assertEqual(sum(1 for e in pitchers if e.appearance_order == 1), 1)
                    self.assertEqual({e.batting_order for e in batters if e.slot_sequence == 0}, set(range(1, 10)))
                    self.assertTrue(all(e.player_id in self.pitcher_ids for e in pitchers))
                    self.assertFalse(any(e.player_id in self.pitcher_ids for e in batters))

    def test_only_players_on_roster_that_year_appear(self):
        appeared = {e.player_id for game in self.games for e in [*game.batting, *game.pitching]}

        self.assertNotIn(self.departed.id, appeared)
        self.assertNotIn(self.newcomer.id, appeared)
        # 自チームの試合にだけ出る
        for game in self.games:
            for entry in [*game.batting, *game.pitching]:
                with self.subTest(game=str(game), player=entry.player_id):
                    self.assertIn(self.team_of[entry.player_id], (game.home_team_id, game.away_team_id))

    def test_foreign_players_stay_within_game_limit(self):
        for game in self.games:
            for team_id in (game.home_team_id, game.away_team_id):
                foreign = {
                    e.player_id
                    for e in [*game.batting, *game.pitching]
                    if self.team_of[e.player_id] == team_id and e.player_id in self.foreign_ids
                }
                with self.subTest(game=str(game), team=team_id):
                    self.assertLessEqual(len(foreign), 1)


class SeedGamesOptionsTest(SeedGamesBase):
    def test_refuses_when_season_already_has_games(self):
        run()
        count = orm_models.Game.objects.filter(year=YEAR).count()

        with self.assertRaisesMessage(CommandError, "--replace"):
            run()
        self.assertEqual(orm_models.Game.objects.filter(year=YEAR).count(), count)

    def test_replace_recreates_season(self):
        run()
        before = set(orm_models.Game.objects.filter(year=YEAR).values_list("id", flat=True))

        output = run("--replace")

        after = set(orm_models.Game.objects.filter(year=YEAR).values_list("id", flat=True))
        self.assertIn(f"既存の試合 {len(before)} 件を削除しました", output)
        self.assertEqual(len(after), len(before))
        self.assertFalse(before & after)
        # 明細も古い試合のぶんは残らない
        self.assertFalse(orm_models.GameBattingLine.objects.filter(game_id__in=before).exists())

    def test_dry_run_writes_nothing(self):
        output = run("--dry-run")

        self.assertIn("--dry-run のため未実行", output)
        self.assertFalse(orm_models.Game.objects.exists())

    def test_rejects_non_positive_games_per_pair(self):
        with self.assertRaisesMessage(CommandError, "--games-per-pair"):
            call_command(COMMAND, "--year", str(YEAR), "--games-per-pair", "0", stdout=StringIO())


class SeedGamesWithoutRostersTest(BaseCase):
    def test_refuses_when_no_league_can_play(self):
        with self.assertRaisesMessage(CommandError, "試合を作れるリーグがありません"):
            run()
        self.assertFalse(orm_models.Game.objects.exists())


class SeedGamesLeftHandersTest(SeedGamesBase):
    """左投げの内野手・捕手を多く置いても、先発・途中出場の捕・二・三・遊に就かない（#158）。"""

    def setUp(self) -> None:
        super().setUp()
        # 捕手2人のうち1人、内野手5人のうち3人を左投げにする（右投げの内野手は2人しかいない）
        for team in self.teams:
            players = orm_models.Player.objects.filter(stints__team=team)
            catcher = players.filter(position=Position.CATCHER.value).order_by("id").first()
            assert catcher is not None
            orm_models.Player.objects.filter(id=catcher.id).update(throws="左")
            infielder_ids = list(
                players.filter(position=Position.INFIELDER.value, stints__to_year__isnull=True)
                .order_by("id")
                .values_list("id", flat=True)[:3]
            )
            orm_models.Player.objects.filter(id__in=infielder_ids).update(throws="左")
        run()
        self.games = DjangoGameRepository(WorldScope.real()).find_all(YEAR)

    def test_no_left_hander_takes_catcher_second_third_or_short(self) -> None:
        left_ids = set(orm_models.Player.objects.filter(throws="左").values_list("id", flat=True))
        restricted = {
            FieldingPosition.CATCHER,
            FieldingPosition.SECOND_BASE,
            FieldingPosition.THIRD_BASE,
            FieldingPosition.SHORTSTOP,
        }
        self.assertTrue(self.games)
        checked = 0
        for game in self.games:
            for entry in game.batting:
                if entry.fielding_position in restricted:
                    checked += 1
                    self.assertNotIn(entry.player_id, left_ids, str(game))
        self.assertGreater(checked, 0)

    def test_every_team_starts_eight_fielders_and_one_designated_hitter(self) -> None:
        """先発は8つの守備位置と指名打者1人を重複なく埋める（指名打者が2人で守備位置が空く形を作らない）。"""
        expected = {FieldingPosition(label) for label in "捕一二三遊左中右指"}
        checked = 0
        for game in self.games:
            for team_id in (game.home_team_id, game.away_team_id):
                starters = [
                    e.fielding_position
                    for e in game.batting
                    if self.team_of[e.player_id] == team_id and e.slot_sequence == 0
                ]
                with self.subTest(game=str(game), team=team_id):
                    self.assertEqual(len(starters), 9)
                    self.assertEqual(set(starters), expected)
                checked += 1
        self.assertGreater(checked, 0)


def _player(player_id: int, position: Position, throws: str) -> SimpleNamespace:
    return SimpleNamespace(id=player_id, position=position.value, throws=throws)


class DefensiveAlignmentTest(SimpleTestCase):
    """守備位置の割り振り（#158）。行き場の無い左投げが何人いても、9枠を重複なく埋める。"""

    def lefty_heavy(self) -> list[SimpleNamespace]:
        # 捕右・内左左左右右・外右右右（遊撃を守れる右投げが足りない）
        return [
            _player(1, Position.CATCHER, "右"),
            *(_player(n, Position.INFIELDER, "左") for n in (2, 3, 4)),
            *(_player(n, Position.INFIELDER, "右") for n in (5, 6)),
            *(_player(n, Position.OUTFIELDER, "右") for n in (7, 8, 9)),
        ]

    def test_slots_are_filled_without_duplicates_even_when_the_rule_must_bend(self) -> None:
        regulars = self.lefty_heavy()
        positions, starters, bench = Command._defensive_alignment(regulars, [])
        self.assertEqual(
            sorted(positions.values(), key=lambda p: p.value),
            sorted((FieldingPosition(label) for label in "捕一二三遊左中右指"), key=lambda p: p.value),
        )
        self.assertEqual(len(positions), 9)
        self.assertEqual(starters, regulars)
        self.assertEqual(bench, [])

    def test_a_right_handed_reserve_takes_the_open_slot_before_the_rule_bends(self) -> None:
        regulars = self.lefty_heavy()
        reserves = [_player(10, Position.OUTFIELDER, "右"), _player(11, Position.INFIELDER, "右")]
        positions, starters, bench = Command._defensive_alignment(regulars, reserves)
        self.assertEqual(len(set(positions.values())), 9)
        restricted = {
            FieldingPosition.CATCHER,
            FieldingPosition.SECOND_BASE,
            FieldingPosition.THIRD_BASE,
            FieldingPosition.SHORTSTOP,
        }
        for player in starters:
            if positions[player.id] in restricted:
                self.assertEqual(player.throws, "右", player.id)
        self.assertEqual({p.id for p in starters} | {p.id for p in bench}, {p.id for p in [*regulars, *reserves]})
        self.assertEqual(len(starters), 9)

    def test_without_left_handers_nothing_changes(self) -> None:
        regulars = [_player(n, Position.INFIELDER, "右") for n in (2, 3, 4, 5)] + [
            _player(1, Position.CATCHER, "右"),
            *(_player(n, Position.OUTFIELDER, "右") for n in (7, 8, 9)),
            _player(6, Position.DESIGNATED_HITTER, "右"),
        ]
        positions, starters, _ = Command._defensive_alignment(regulars, [])
        self.assertEqual(len(set(positions.values())), 9)
        self.assertEqual(starters, regulars)


class WithinQuotaTest(SimpleTestCase):
    """外国人の出場枠が先で、投げる手は次（#158）。"""

    def side(self, foreign_ids: set[int], used: int) -> _Side:
        return _Side(team=None, roster={"foreign": foreign_ids}, is_home=True, foreign_used=used)

    def test_a_reserve_that_suits_the_position_is_preferred(self) -> None:
        wanted = _player(1, Position.INFIELDER, "右")
        left = _player(2, Position.INFIELDER, "左")
        right = _player(3, Position.INFIELDER, "右")
        side = self.side({1}, used=1)
        chosen = Command._within_quota(side, wanted, [left, right], 1, FieldingPosition.SHORTSTOP)
        self.assertEqual(chosen.id, 3)

    def test_the_quota_wins_over_the_throwing_hand(self) -> None:
        wanted = _player(1, Position.INFIELDER, "右")
        left = _player(2, Position.INFIELDER, "左")
        side = self.side({1}, used=1)
        chosen = Command._within_quota(side, wanted, [left], 1, FieldingPosition.SHORTSTOP)
        self.assertEqual(chosen.id, 2)
