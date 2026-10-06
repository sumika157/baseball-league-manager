"""シーズンを締める計算（`domain/pennant/offseason.py`）と、生年月日の推定（`fork.py`）。DB 不要。"""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date

from myapp.domain.entities import Team
from myapp.domain.pennant.fork import estimated_birth_date, fork_roster, with_birth_date
from myapp.domain.pennant.offseason import (
    OffseasonClub,
    OffseasonPlayer,
    overall_value,
    plan_offseason,
    rating_change,
    retirement_value,
)
from myapp.domain.pennant.ratings import PlayerRatings
from myapp.domain.pennant.retirement import MAX_PLAYING_AGE, ROOKIE_FACTOR, PlayingTime, retirement_chance
from myapp.domain.simulation.randomness import game_uniform
from myapp.domain.simulation.ratings import BatterRatings, PitcherRatings
from myapp.domain.value_objects import JerseyNumber, Position, Profile

YEAR = 2027
START_YEAR = 2026


def player(
    player_id: int,
    *,
    born: date | None = date(1999, 7, 1),
    position: Position = Position.INFIELDER,
    ratings: bool = True,
    foreign: bool = False,
) -> OffseasonPlayer:
    rating = PitcherRatings() if position.is_pitcher else BatterRatings()
    return OffseasonPlayer(
        player_id=player_id,
        position=position,
        profile=Profile(birth_date=born, is_foreign_player=foreign, back_name=f"NAME{player_id}"),
        number=player_id,
        joined_year=2020,
        ratings=PlayerRatings(player_id, YEAR, rating) if ratings else None,
        playing_time=PlayingTime(plate_appearances=400),
    )


def club(*players: OffseasonPlayer, team_id: int = 1) -> OffseasonClub:
    return OffseasonClub(team_id=team_id, foreign_roster_limit=4, players=players)


def plan(clubs: list[OffseasonClub], seed: int = 1):
    return plan_offseason(clubs, year=YEAR, world_seed=seed, start_year=START_YEAR, used_names=())


class EstimatedBirthDateTest(unittest.TestCase):
    def test_uses_debut_year_when_known(self) -> None:
        self.assertEqual(estimated_birth_date(Profile(debut_year=2018), 2026), date(1996, 7, 1))

    def test_falls_back_to_start_year(self) -> None:
        self.assertEqual(estimated_birth_date(Profile(), 2026), date(1999, 7, 1))

    def test_with_birth_date_keeps_a_known_one(self) -> None:
        known = Profile(birth_date=date(1990, 1, 1), debut_year=2018)
        self.assertIs(with_birth_date(known, 2026), known)
        self.assertEqual(with_birth_date(Profile(debut_year=2018), 2026).birth_date, date(1996, 7, 1))

    def test_fork_fills_the_birth_date_without_touching_the_source(self) -> None:
        source = Team(name="元", league_id=1, id=1)
        original = source.add_player("田中太郎", JerseyNumber(1), Position.PITCHER, from_year=2020)
        original.profile = Profile(debut_year=2020, throws=None)
        target = Team(name="先", league_id=2, id=2)
        ((_, copy),) = fork_roster(source, target, start_year=2026)
        self.assertEqual(copy.profile.birth_date, date(1998, 7, 1))
        self.assertIsNone(original.profile.birth_date)


class PlanTest(unittest.TestCase):
    def test_same_world_gives_same_plan(self) -> None:
        clubs = [club(*(player(i) for i in range(1, 31)))]
        self.assertEqual(plan(clubs), plan(clubs))

    def test_players_at_max_age_always_retire(self) -> None:
        born = date(YEAR - MAX_PLAYING_AGE, 1, 1)
        result = plan([club(player(1, born=born), player(2))])
        self.assertIn(1, result.retired)

    def test_retired_players_get_no_next_year_ratings(self) -> None:
        born = date(YEAR - MAX_PLAYING_AGE, 1, 1)
        result = plan([club(player(1, born=born), player(2), player(3))])
        retained = {change.player_id for change in result.retained}
        self.assertFalse(retained & set(result.retired))
        self.assertEqual(retained | set(result.retired), {1, 2, 3})
        for change in result.retained:
            self.assertEqual((change.before.year, change.after.year), (YEAR, YEAR + 1))

    def test_players_without_ratings_are_reported_not_given_ratings(self) -> None:
        result = plan([club(player(1, ratings=False), player(2))])
        self.assertIn(1, result.without_ratings)
        self.assertNotIn(1, {change.player_id for change in result.retained})

    def test_missing_birth_date_is_estimated(self) -> None:
        result = plan([club(player(1, born=None), player(2, born=None))])
        self.assertEqual(len(result.retained) + len(result.retired), 2)

    def test_draftees_refill_each_club_without_clashing_numbers(self) -> None:
        clubs = [
            club(*(player(i) for i in range(1, 31)), team_id=1),
            club(*(player(i) for i in range(101, 131)), team_id=2),
        ]
        result = plan(clubs)
        for team_id, members in ((1, range(1, 31)), (2, range(101, 131))):
            draftees = [d for d in result.draftees if d.team_id == team_id]
            self.assertTrue(2 <= len(draftees) <= 8)
            stay = {m for m in members if m not in result.retired}
            numbers = [d.number for d in draftees]
            self.assertEqual(len(numbers), len(set(numbers)))
            self.assertFalse(set(numbers) & stay)
        self.assertEqual(len({d.name for d in result.draftees}), len(result.draftees))

    def test_draftee_names_avoid_used_names(self) -> None:
        clubs = [club(*(player(i) for i in range(1, 31)))]
        first = plan(clubs)
        taken = {d.name for d in first.draftees}
        second = plan_offseason(clubs, year=YEAR, world_seed=1, start_year=START_YEAR, used_names=taken)
        self.assertFalse(taken & {d.name for d in second.draftees})

    def test_retirement_is_independent_of_other_players(self) -> None:
        """選手を足し引きしても、他の選手の引退は動かない（乱数の種が選手の id）。"""
        small = plan([club(*(player(i, born=date(1990, 1, 1)) for i in range(1, 11)))])
        large = plan([club(*(player(i, born=date(1990, 1, 1)) for i in range(1, 21)))])
        self.assertEqual([i for i in large.retired if i <= 10], list(small.retired))

    def test_anchor_correction_is_reported(self) -> None:
        result = plan(
            [club(*(player(i) for i in range(1, 41)), *(player(i, position=Position.PITCHER) for i in range(41, 81)))]
        )
        self.assertGreaterEqual(result.anchor.mean_shift, 0.0)


class HelperTest(unittest.TestCase):
    def test_rating_change_is_the_value_difference(self) -> None:
        self.assertAlmostEqual(
            rating_change(BatterRatings(50, 50, 50, 50, 50), BatterRatings(60, 50, 50, 50, 50)), 3.4
        )
        self.assertAlmostEqual(rating_change(PitcherRatings(50, 50, 50, 50), PitcherRatings(50, 60, 50, 50)), 3.5)

    def test_overall_value_is_batting_for_batters_and_pitching_for_pitchers(self) -> None:
        batter = BatterRatings(60, 50, 50, 50, 90)
        pitcher = PitcherRatings(60, 50, 50, 90)
        self.assertEqual(overall_value(batter), batter.batting_value, "守備力は総合に入れない")
        self.assertEqual(overall_value(pitcher), pitcher.pitching_value, "スタミナは総合に入れない")

    def test_rating_change_rejects_mixed_kinds(self) -> None:
        with self.assertRaises(ValueError):
            rating_change(BatterRatings(), PitcherRatings())

    def test_catcher_is_valued_by_the_better_of_defense_and_batting(self) -> None:
        ratings = BatterRatings(contact=40, power=40, eye=40, speed=40, fielding=70)
        self.assertEqual(retirement_value(Position.CATCHER, ratings), 70.0)
        self.assertLess(retirement_value(Position.INFIELDER, ratings), 70.0)
        self.assertEqual(retirement_value(Position.INFIELDER, None), 45.0)

    def test_game_uniform_is_stable_and_in_range(self) -> None:
        value = game_uniform(1, 2027, "retire-5")
        self.assertEqual(value, game_uniform(1, 2027, "retire-5"))
        self.assertTrue(0.0 <= value < 1.0)
        self.assertNotEqual(value, game_uniform(1, 2027, "retire-6"))


class RookieProtectionTest(unittest.TestCase):
    """若手の保護（入団2年以内）は、入団年（`debut_year`）から数える。分岐した選手の在籍の開始年は一律に開幕年。"""

    def test_the_entered_year_is_the_debut_year_when_known(self) -> None:
        forked = replace(player(1), joined_year=START_YEAR, profile=Profile(debut_year=2018))
        self.assertEqual(forked.entered_year, 2018)

    def test_the_entered_year_falls_back_to_the_start_of_the_stint(self) -> None:
        self.assertEqual(replace(player(1), joined_year=2020, profile=Profile()).entered_year, 2020)

    def test_a_forked_veteran_is_not_protected_as_a_rookie(self) -> None:
        """入団2021年の23歳の選手が、分岐した年（在籍の開始年 = 開幕年）を理由に保護されない。"""
        born = date(2003, 7, 1)  # YEAR の4月1日で23歳
        veteran = replace(
            player(1, born=born), joined_year=START_YEAR, profile=Profile(birth_date=born, debut_year=2021)
        )
        rookie = replace(
            player(2, born=born), joined_year=START_YEAR, profile=Profile(birth_date=born, debut_year=YEAR)
        )
        self.assertEqual((veteran.seasons_as_pro(YEAR), rookie.seasons_as_pro(YEAR)), (7, 1))
        chances = [
            retirement_chance(
                age=23,
                value=45.0,
                playing_time=PlayingTime(plate_appearances=400),
                is_foreign=False,
                seasons_as_pro=target.seasons_as_pro(YEAR),
            )
            for target in (veteran, rookie)
        ]
        self.assertAlmostEqual(chances[0], chances[1] / ROOKIE_FACTOR)


if __name__ == "__main__":
    unittest.main()
