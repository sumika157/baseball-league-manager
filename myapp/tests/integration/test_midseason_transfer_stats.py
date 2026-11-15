"""年の途中で移った選手の成績が、球団ごとに分かれること（#396）。

同じ年に2球団の在籍を持つ選手（移る前の球団 A の在籍はその年で終わり、移った先の球団 B の在籍はその年から）。
球団で絞る成績は、明細のチーム（その試合でどちらの側に出たか）で引く。今の所属や「球団の試合（ホームか
ビジター）」で引くと、移る前の分が移った先に入り、A と B の対戦は両方に入っていた。

リーグの成績は年の終わりの球団の1行に年の合計のまま（NPB も同一リーグ内の移籍は合算）。
"""

from datetime import date

from myapp.domain.value_objects import BattingLine, InningsPitched, PitchingLine
from myapp.infrastructure import orm_models
from myapp.infrastructure.queries import DjangoPennantActivityQuery, DjangoPlayerStatsQuery
from myapp.infrastructure.repositories import DjangoGameRepository

from ..helpers import TEST_YEAR, play_game, real_scope
from .base import BaseCase


def _innings(notation: str) -> InningsPitched:
    return InningsPitched.from_notation(notation)


class MidseasonTransferStatsTest(BaseCase):
    """A（`rival`）から B（`team`）へ年の途中で移った野手と投手。C は第三の球団。"""

    a: orm_models.Team
    b: orm_models.Team
    c: orm_models.Team
    other: orm_models.Team
    batter: int
    pitcher: int

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.other = orm_models.Team.objects.create(league=cls.league, name="第三のチーム")
        cls.a, cls.b, cls.c = cls.rival, cls.team, cls.other
        cls.batter = cls._moved("移籍した野手", "内野手")
        cls.pitcher = cls._moved("移籍した投手", "投手")
        b, p = cls.batter, cls.pitcher
        on_a, on_b = {b: cls.a.id, p: cls.a.id}, {b: cls.b.id, p: cls.b.id}

        # 移る前（A）: C との試合と、B との試合
        play_game(cls.a, cls.c, day=1, batting={b: BattingLine(at_bats=4, singles=4)}, teams=on_a)
        cls.a_vs_b = play_game(
            cls.a,
            cls.b,
            day=2,
            batting={b: BattingLine(at_bats=4, singles=2)},
            pitching={p: PitchingLine(innings=_innings("6.0"), earned_runs=3, wins=1)},
            teams=on_a,
        )
        # 移った後（B）: A との試合と、C との試合
        play_game(cls.b, cls.a, month=7, day=1, batting={b: BattingLine(at_bats=3, singles=1)}, teams=on_b)
        play_game(
            cls.b,
            cls.c,
            month=7,
            day=2,
            pitching={p: PitchingLine(innings=_innings("9.0"), earned_runs=0, wins=1)},
            teams=on_b,
        )

    @classmethod
    def _moved(cls, name: str, position: str) -> int:
        player = orm_models.Player.objects.create(name=name, position=position)
        orm_models.PlayerStint.objects.create(
            player=player, team=cls.a, number="10", from_year=TEST_YEAR, to_year=TEST_YEAR
        )
        orm_models.PlayerStint.objects.create(player=player, team=cls.b, number="20", from_year=TEST_YEAR)
        return player.id

    def _row(self, rows, player_id):
        return next(row for row in rows if row.id == player_id)

    def test_club_roster_counts_only_the_games_for_that_club(self):
        """A の一覧には A で出た分（8打数6安打）だけ、B の一覧には B で出た分（3打数1安打）だけ。"""
        on_a = self._row(self.team_view.list_batters(self.a.id, year=TEST_YEAR).rows, self.batter)
        on_b = self._row(self.team_view.list_batters(self.b.id, year=TEST_YEAR).rows, self.batter)

        self.assertEqual((on_a.at_bats, on_a.hits), (8, 6))
        self.assertEqual((on_b.at_bats, on_b.hits), (3, 1))

    def test_club_pitchers_and_totals_count_only_the_games_for_that_club(self):
        on_b = self._row(self.team_view.list_pitchers(self.b.id, year=TEST_YEAR).rows, self.pitcher)

        self.assertEqual(on_b.innings_pitched, "9.0")
        # チームの合計も B で投げた分だけ（A で投げた 6回は B の投球回に入らない）
        self.assertEqual(self.team_view.get_team_totals(self.b.id, year=TEST_YEAR).innings_pitched, "9.0")
        self.assertEqual(self.team_view.get_team_totals(self.a.id, year=TEST_YEAR).innings_pitched, "6.0")

    def test_club_monthly_splits_count_only_the_games_for_that_club(self):
        """A と B の対戦で A の側に出た分（4打数2安打）は、B の4月に入らない（顔ぶれで絞っていた）。"""
        april = [row for row in self.team_view.list_team_monthly_splits(self.b.id, TEST_YEAR) if "4月" in row.label]

        self.assertEqual(len(april), 1)
        self.assertEqual(april[0].batting_average, 0.0)

    def test_league_stats_keep_one_row_with_the_season_total(self):
        """リーグの成績は年の終わりの球団（B）の1行に、年の合計（11打数7安打）。"""
        rows = [
            row
            for row in DjangoPlayerStatsQuery(real_scope()).list_season(self.league.id, TEST_YEAR)
            if row.player_id == self.batter
        ]

        self.assertEqual([row.team_id for row in rows], [self.b.id])
        self.assertEqual((rows[0].batting.at_bats, rows[0].batting.hits), (11, 7))

    def test_player_page_years_are_per_club(self):
        on_a = self.team_view.get_player_profile(self.a.id, self.batter).years
        on_b = self.team_view.get_player_profile(self.b.id, self.batter).years

        self.assertEqual([(row.year, row.at_bats, row.hits) for row in on_a or []], [(TEST_YEAR, 8, 6)])
        self.assertEqual([(row.year, row.at_bats, row.hits) for row in on_b or []], [(TEST_YEAR, 3, 1)])

    def test_player_page_fielding_years_are_per_club(self):
        """守備の年度別も明細のチームで分ける（A と B の対戦は、守った側だけ）。"""
        orm_models.GameFieldingLine.objects.create(
            game_id=self.a_vs_b.id, player_id=self.batter, team=self.a, putouts=3
        )
        on_a = self.team_view.get_player_profile(self.a.id, self.batter).fielding
        on_b = self.team_view.get_player_profile(self.b.id, self.batter).fielding

        assert on_a is not None
        self.assertEqual([(row.label, row.putouts) for row in on_a.years], [(f"{TEST_YEAR}年", 3)])
        assert on_b is not None
        self.assertEqual(on_b.years, [])
        # 通算は移籍前も含む
        self.assertEqual(on_b.career.putouts, 3)

    def test_period_pitching_counts_only_the_games_for_that_club(self):
        """GM ホームの期間の投手成績は、移る前の球団での勝ちを含まない（今の所属で引いていた）。"""
        activity = DjangoPennantActivityQuery(real_scope())
        season = {"after": date(TEST_YEAR, 3, 31), "through": date(TEST_YEAR, 12, 31)}

        on_b = [row for row in activity.pitching_between(self.b.id, **season) if row.player_id == self.pitcher]
        on_a = [row for row in activity.pitching_between(self.a.id, **season) if row.player_id == self.pitcher]

        self.assertEqual([row.wins for row in on_b], [1])
        self.assertEqual([row.wins for row in on_a], [1])

    def test_box_score_puts_the_player_on_the_side_he_played_for(self):
        """過去の試合のボックススコアは、移る前の球団（A）の側に載る（今の所属で振り分けていた）。"""
        detail = self.game_view.get_game_detail(self.a_vs_b.id)

        sides = {box.team_id: {row.player_id for row in [*box.batting, *box.pitching]} for box in detail.boxes}

        self.assertEqual(sides, {self.a.id: {self.batter, self.pitcher}})

    def test_repository_round_trips_the_side_of_pitching_lines(self):
        """投球の明細のチームは保存して読み戻せる（集計はこの列で球団を分ける）。"""
        game = DjangoGameRepository(real_scope()).find_by_id(self.a_vs_b.id)

        self.assertEqual([(outing.player_id, outing.team_id) for outing in game.pitching], [(self.pitcher, self.a.id)])
        self.assertEqual(
            orm_models.GamePitchingLine.objects.get(game_id=self.a_vs_b.id, player_id=self.pitcher).team_id, self.a.id
        )
