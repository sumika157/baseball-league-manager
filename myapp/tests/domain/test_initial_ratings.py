"""世界の作成時の初期能力（`pennant/initial_ratings.py`）の単体テスト。Django も DB も使わない。

束ねる処理（選手全員の推定・成長型・年齢）を確かめる。あわせて、**推定した能力でシミュレーションすると
元の成績の水準が再現される**ことを、エンジン自身が作った成績（能力が分かっている）で確かめる。
"""

from datetime import date, timedelta
from unittest import TestCase

from myapp.domain.entities import Game
from myapp.domain.pennant.initial_ratings import age_at_season_start, estimate_initial_ratings
from myapp.domain.pennant.ratings import PlayerRatings
from myapp.domain.simulation.engine import simulate_game
from myapp.domain.simulation.estimate import CareerRecord
from myapp.domain.simulation.levels import LevelTally
from myapp.domain.simulation.manager import ClubRoster, PitchingHistory, SimBatter, SimPitcher, choose_active_roster
from myapp.domain.simulation.randomness import game_seed, make_random
from myapp.domain.simulation.ratings import BatterRatings, GrowthType, PitcherRatings
from myapp.domain.simulation.samples import round_robin_days, spread_league
from myapp.domain.value_objects import BattingLine, FieldingLine, InningsPitched, PitchingLine, Position, Profile

YEAR = 2027


def batter(player_id, **line):
    return CareerRecord(player_id, Position.INFIELDER, batting=BattingLine(**line))


def pitcher(player_id, **line):
    return CareerRecord(player_id, Position.PITCHER, pitching=PitchingLine(**line))


class EstimateInitialRatingsTest(TestCase):
    def setUp(self):
        self.records = [
            batter(1, at_bats=500, singles=130, home_runs=30, strikeouts=100, walks=50),
            pitcher(2, innings=InningsPitched(outs=450), strikeouts=140, hits_allowed=140, walks_allowed=40),
            CareerRecord(3, Position.CATCHER),
        ]

    def _estimate(self, records=None, seed=11):
        return estimate_initial_ratings(self.records if records is None else records, seed=seed, year=YEAR)

    def test_one_set_per_player_in_the_given_order(self):
        result = self._estimate()

        self.assertEqual([item.player_id for item in result], [1, 2, 3])
        self.assertTrue(all(item.year == YEAR for item in result))

    def test_position_decides_the_kind_of_ratings(self):
        batter_ratings, pitcher_ratings, rookie = self._estimate()

        self.assertIsInstance(batter_ratings.ratings, BatterRatings)
        self.assertIsInstance(pitcher_ratings.ratings, PitcherRatings)
        self.assertIsInstance(rookie.ratings, BatterRatings, "成績の無い野手も野手の能力を持つ")
        self.assertTrue(pitcher_ratings.is_pitcher)
        self.assertFalse(batter_ratings.is_pitcher)

    def test_a_good_record_gives_better_ratings_than_an_empty_one(self):
        regular, _, rookie = self._estimate()

        self.assertGreater(regular.ratings.power, rookie.ratings.power)

    def test_the_same_seed_gives_the_same_ratings(self):
        self.assertEqual(self._estimate(seed=3), self._estimate(seed=3))

    def test_the_growth_type_depends_on_the_seed_and_the_player(self):
        many = [CareerRecord(i, Position.OUTFIELDER) for i in range(1, 200)]

        first = [item.ratings.growth for item in self._estimate(many, seed=1)]
        second = [item.ratings.growth for item in self._estimate(many, seed=2)]

        self.assertNotEqual(first, second)
        self.assertEqual(set(first), set(GrowthType), "どの成長型も現れる")

    def test_the_growth_type_follows_the_seed_player_not_the_new_id(self):
        """写した先の id は世界ごとに違う。分岐元の選手の id で引けば、同じ分岐元から作った世界は同じになる。"""
        in_one_world = [CareerRecord(100 + i, Position.OUTFIELDER, seed_player_id=i) for i in range(1, 60)]
        in_another = [CareerRecord(900 + i, Position.OUTFIELDER, seed_player_id=i) for i in range(1, 60)]

        first = [item.ratings.growth for item in self._estimate(in_one_world)]
        second = [item.ratings.growth for item in self._estimate(in_another)]

        self.assertEqual(first, second)

    def test_adding_a_player_does_not_change_anyone_elses_growth_type(self):
        """成長型は選手ごとの乱数で決まる。選手を足し引きしても、他の選手の成長型は動かない。"""
        alone = self._estimate([CareerRecord(5, Position.OUTFIELDER)])
        crowd = self._estimate([CareerRecord(4, Position.OUTFIELDER), CareerRecord(5, Position.OUTFIELDER)])

        self.assertEqual(alone[0].ratings.growth, crowd[1].ratings.growth)

    def test_the_growth_type_is_carried_into_the_ratings(self):
        for item in self._estimate():
            self.assertIn(item.ratings.growth, set(GrowthType))

    def test_the_league_norms_use_every_player(self):
        """盗塁や失策の目印は選手全員から求める。他の選手が変わると、目印が変わって推定も動く。"""
        runner = batter(1, at_bats=450, singles=100, walks=40, stolen_bases=20, caught_stealing=5)
        slow_league = [runner] + [batter(i, at_bats=450, singles=100, walks=40, stolen_bases=40) for i in range(2, 12)]
        fast_league = [runner] + [batter(i, at_bats=450, singles=100, walks=40) for i in range(2, 12)]

        slow = self._estimate(slow_league)[0].ratings.speed
        fast = self._estimate(fast_league)[0].ratings.speed

        self.assertLess(slow, fast, "周りが盗塁しているほど、同じ盗塁数でも走力は高く見えない")

    def test_empty_input_gives_nothing(self):
        self.assertEqual(self._estimate([]), [])


class AgeAtSeasonStartTest(TestCase):
    def test_age_is_counted_on_the_first_of_april_of_the_world_year(self):
        profile = Profile(birth_date=date(1995, 3, 4))

        self.assertEqual(age_at_season_start(profile, 2030), 35)
        self.assertEqual(age_at_season_start(Profile(birth_date=date(1995, 4, 2)), 2030), 34)

    def test_no_birth_date_gives_none(self):
        self.assertIsNone(age_at_season_start(Profile(), 2030))

    def test_a_birth_date_after_the_season_start_gives_none(self):
        """世界の年より後に生まれたことになる選手は、年齢が決められない（エラーにしない）。"""
        self.assertIsNone(age_at_season_start(Profile(birth_date=date(2031, 1, 1)), 2030))


class PlayerRatingsTest(TestCase):
    def test_an_impossible_year_is_rejected(self):
        from myapp.domain.exceptions import InvalidSeason

        with self.assertRaises(InvalidSeason):
            PlayerRatings(player_id=1, year=1800, ratings=BatterRatings())


# --- 推定した能力で、元の水準が再現されるか ----------------------------------------------------


def _play(clubs, seed_key, days, seed=1):
    """日程どおりに試合をして、水準と選手ごとの成績を集める。"""
    history = PitchingHistory()
    tally = LevelTally()
    games: list[Game] = []
    for day, matches in enumerate(days):
        for home, away in matches:
            rng = make_random(game_seed(seed, YEAR, f"{seed_key}-{day}-{home}"))
            played = simulate_game(
                rng, clubs[home], clubs[away], played_on=date(YEAR, 4, 1) + timedelta(days=day), history=history
            )
            tally.add(played.game)
            games.append(played.game)
    return tally, games


def _records(pool: dict[int, ClubRoster], games: list[Game]) -> list[CareerRecord]:
    """試合の記録から、選手ごとの通算成績（推定の材料）を作る。出場の無い選手は成績が空。"""
    batting: dict[int, BattingLine] = {}
    pitching: dict[int, PitchingLine] = {}
    fielding: dict[int, FieldingLine] = {}
    for game in games:
        for at_bat in game.batting:
            batting[at_bat.player_id] = batting.get(at_bat.player_id, BattingLine()) + at_bat.line
        for outing in game.pitching:
            pitching[outing.player_id] = pitching.get(outing.player_id, PitchingLine()) + outing.line
        for chance in game.fielding:
            fielding[chance.player_id] = fielding.get(chance.player_id, FieldingLine()) + chance.line
    records: list[CareerRecord] = []
    for club in pool.values():
        records.extend(
            CareerRecord(
                hitter.player_id,
                hitter.position,
                batting=batting.get(hitter.player_id, BattingLine()),
                fielding=fielding.get(hitter.player_id, FieldingLine()),
            )
            for hitter in club.batters
        )
        records.extend(
            CareerRecord(
                thrower.player_id,
                Position.PITCHER,
                pitching=pitching.get(thrower.player_id, PitchingLine()),
            )
            for thrower in club.pitchers
        )
    return records


def _with_estimates(pool: dict[int, ClubRoster], estimated: list[PlayerRatings]) -> dict[int, ClubRoster]:
    """能力を推定した値に差し替えた球団（選手の顔ぶれと登録位置は同じ）。"""
    by_player = {item.player_id: item.ratings for item in estimated}
    clubs = {}
    for team_id, club in pool.items():
        batters = []
        for hitter in club.batters:
            ratings = by_player[hitter.player_id]
            assert isinstance(ratings, BatterRatings)
            batters.append(SimBatter(hitter.player_id, hitter.name, hitter.position, ratings, hitter.is_foreign))
        pitchers = []
        for thrower in club.pitchers:
            ratings = by_player[thrower.player_id]
            assert isinstance(ratings, PitcherRatings)
            pitchers.append(SimPitcher(thrower.player_id, thrower.name, ratings, thrower.is_foreign))
        clubs[team_id] = ClubRoster(team_id, club.name, tuple(batters), tuple(pitchers))
    return clubs


class EstimatedRatingsReproduceTheLevelTest(TestCase):
    """エンジンが作った1シーズンの成績（能力は分かっている）から能力を推定し、同じ顔ぶれで回し直す。

    推定した能力のリーグの水準が、元の水準に近いこと。出場の少ない選手（2軍の控え）を平均へ
    寄せると、控えが1軍に紛れ込んで本塁打が増え安打が減る、といった水準のずれが出る。
    """

    TEAMS = 6
    # 1球団 145 試合（総当たり 5 日 × 29 周）。割り当てが小さいと、規定打席に届く選手が足りない
    REPEATS = 29

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        pool_clubs = spread_league(make_random(5), cls.TEAMS)
        cls.pool = {club.team_id: club for club in pool_clubs}
        days = round_robin_days(list(cls.pool), cls.REPEATS)

        actives = {team_id: choose_active_roster(club, 4) for team_id, club in cls.pool.items()}
        cls.original, games = _play(actives, "original", days)

        records = _records(cls.pool, games)
        cls.estimated_ratings = estimate_initial_ratings(records, seed=3, year=YEAR)
        estimated_pool = _with_estimates(cls.pool, cls.estimated_ratings)
        estimated_actives = {team_id: choose_active_roster(club, 4) for team_id, club in estimated_pool.items()}
        cls.replayed, _ = _play(estimated_actives, "replayed", days)
        cls.original_rows = {row.key: row.value for row in cls.original.rows()}
        cls.replayed_rows = {row.key: row.value for row in cls.replayed.rows()}

    def _close(self, key, *, relative=None, absolute=None):
        original, replayed = self.original_rows[key], self.replayed_rows[key]
        if relative is not None:
            self.assertAlmostEqual(replayed / original, 1.0, delta=relative, msg=f"{key}: {original} → {replayed}")
        else:
            self.assertAlmostEqual(replayed, original, delta=absolute, msg=f"{key}: {original} → {replayed}")

    def test_batting_average_is_reproduced(self):
        self._close("batting_average", absolute=0.010)

    def test_home_runs_are_reproduced(self):
        self._close("home_runs_per_9", relative=0.20)

    # 防御率と得点の許容は、推定した能力のほうが元の水準より少し高く出る（元は調整用の分布のシーズン1回。
    # 推定した能力でシーズンの水準が目標帯に入るよう β を調整した結果、元の水準は帯の下端寄りになった）のを見込む
    def test_earned_run_average_is_reproduced(self):
        self._close("era", relative=0.15)

    def test_strikeouts_and_walks_are_reproduced(self):
        self._close("strikeouts_per_9", relative=0.08)
        self._close("walks_per_9", relative=0.10)

    def test_runs_are_reproduced(self):
        self._close("runs", relative=0.15)

    def test_the_ratings_are_centered_below_the_first_team_average(self):
        """平均は 50 より低い（控えを含む全員の平均。1軍の主力が 50 前後）。"""
        batters = [item.ratings for item in self.estimated_ratings if isinstance(item.ratings, BatterRatings)]
        average_contact = sum(r.contact for r in batters) / len(batters)

        self.assertLess(average_contact, 50)
        self.assertGreater(average_contact, 38)
