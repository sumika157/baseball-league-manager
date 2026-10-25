"""見込み（`domain/pennant/outlook.py`。#308 P3a）。伸びの出典・見込みの能力・在籍の選手の見立て。DB 不要。"""

from __future__ import annotations

import unittest
from dataclasses import replace
from statistics import fmean, pstdev

from myapp.domain.entities import Stint
from myapp.domain.pennant.offseason_player import TrackRecord, club_seasons, is_at_club, overall_value
from myapp.domain.pennant.outlook import (
    PEAK_AGE,
    SCOUT_STEP,
    ScoutingAccuracy,
    growth_to_peak,
    item_growth_to_peak,
    judged_items,
    outlook_grade,
    projected_ratings,
    roster_outlook_grade,
    roster_potential_estimate,
    roster_projection,
    with_filled_potential,
)
from myapp.domain.simulation.ratings import BatterRatings, GrowthType, PitcherRatings, Potential, RatingGrade
from myapp.domain.value_objects import BattingLine, JerseyNumber, PitchingLine, Position

SEED = 11
TEAM = 3
EXACT = ScoutingAccuracy(potential_sd=0.0)


class GrowthToPeakTest(unittest.TestCase):
    """伸びの出典（`item_growth_to_peak`）の上に作り直した `growth_to_peak` は、ドラフトの数値を1点も変えない。"""

    # #308 P2（main 9f6a8fb）の `draft_meeting.growth_to_peak` の値
    BEFORE = {
        (18, Position.PITCHER): 15.333333333333334,
        (18, Position.INFIELDER): 14.0,
        (18, Position.CATCHER): 14.0,
        (22, Position.PITCHER): 6.0,
        (22, Position.OUTFIELDER): 5.2,
        (24, Position.PITCHER): 2.3333333333333335,
        (24, Position.INFIELDER): 2.0,
        (26, Position.PITCHER): 0.3333333333333333,
        (26, Position.CATCHER): 0.4,
        (27, Position.PITCHER): 0.0,
        (27, Position.INFIELDER): 0.0,
    }

    def test_growth_to_peak_is_unchanged(self) -> None:
        for (age, position), expected in self.BEFORE.items():
            with self.subTest(age=age, position=position):
                self.assertEqual(growth_to_peak(age, position), expected)

    def test_growth_to_peak_is_the_mean_of_the_judged_items(self) -> None:
        for age in range(16, 30):
            for position in Position:
                growth = item_growth_to_peak(age, position)
                ratings = PitcherRatings() if position.is_pitcher else BatterRatings()
                names = judged_items(ratings)
                with self.subTest(age=age, position=position):
                    self.assertEqual(growth_to_peak(age, position), sum(growth[name] for name in names) / len(names))

    def test_no_growth_from_the_peak_age(self) -> None:
        for age in (PEAK_AGE, PEAK_AGE + 5):
            for position in (Position.PITCHER, Position.INFIELDER):
                with self.subTest(age=age, position=position):
                    self.assertTrue(all(value == 0 for value in item_growth_to_peak(age, position).values()))

    def test_younger_players_have_more_to_grow(self) -> None:
        for position in (Position.PITCHER, Position.OUTFIELDER):
            values = [growth_to_peak(age, position) for age in range(18, PEAK_AGE + 1)]
            with self.subTest(position=position):
                self.assertEqual(values, sorted(values, reverse=True))
                self.assertGreater(values[0], 10)


class ProjectedRatingsTest(unittest.TestCase):
    """見込みの能力: 今の値 + 普通の伸び × 推定の伸びしろの倍率。"""

    def test_an_average_potential_adds_the_usual_growth(self) -> None:
        ratings = BatterRatings(contact=40, power=35, eye=38, speed=50, fielding=45)
        projected = projected_ratings(ratings, age=18, position=Position.INFIELDER, potential_estimate=Potential(50))
        growth = item_growth_to_peak(18, Position.INFIELDER)
        for name in ratings.LABELS:
            with self.subTest(item=name):
                self.assertEqual(getattr(projected, name), getattr(ratings, name) + growth[name])

    def test_a_larger_estimate_never_projects_lower(self) -> None:
        ratings = PitcherRatings(stuff=40, control=35, home_run_avoidance=42, stamina=50)
        values = [
            overall_value(
                projected_ratings(ratings, age=19, position=Position.PITCHER, potential_estimate=Potential(value))
            )
            for value in range(5, 101, 5)
        ]
        self.assertEqual(values, sorted(values))
        self.assertGreater(values[-1], values[0])

    def test_from_the_peak_age_the_projection_is_the_current_ratings(self) -> None:
        ratings = BatterRatings(contact=60)
        for age in (PEAK_AGE, 33):
            with self.subTest(age=age):
                self.assertIs(
                    projected_ratings(
                        ratings, age=age, position=Position.INFIELDER, potential_estimate=Potential(100)
                    ),
                    ratings,
                )

    def test_hidden_values_are_carried_over(self) -> None:
        """成長型と伸びしろは作り直さずに写す（`replace`）。見込みは項目だけを変える。"""
        ratings = BatterRatings(growth=GrowthType.LATE, potential=Potential(80))
        projected = projected_ratings(ratings, age=20, position=Position.CATCHER, potential_estimate=Potential(30))
        self.assertIs(projected.growth, GrowthType.LATE)
        self.assertEqual(projected.potential, Potential(80))


class RosterPotentialEstimateTest(unittest.TestCase):
    """在籍の選手の伸びしろの見立て（2026-10-25 ユーザー決定 Q1 A）。"""

    def estimate(self, player_id: int, *, truth: int = 50, seasons: int = 1, team_id: int = TEAM) -> int:
        ratings = BatterRatings(potential=Potential(truth))
        return roster_potential_estimate(
            ratings, world_seed=SEED, team_id=team_id, player_id=player_id, seasons=seasons
        ).value

    def test_is_deterministic_and_rounded_to_steps(self) -> None:
        for player_id in range(1, 50):
            with self.subTest(player=player_id):
                value = self.estimate(player_id, truth=63)
                self.assertEqual(value, self.estimate(player_id, truth=63))
                self.assertEqual(value % SCOUT_STEP, 0)
                self.assertTrue(SCOUT_STEP <= value <= 100)

    def test_the_error_shrinks_with_the_seasons_at_the_club(self) -> None:
        """誤差の幅は SD 10/√在籍年数（1年目 10 → 4年目 5）。向きは同じなので、在籍が長いほど本当の値に近づく。"""
        first = [self.estimate(player_id, seasons=1) - 50 for player_id in range(1, 2001)]
        fourth = [self.estimate(player_id, seasons=4) - 50 for player_id in range(1, 2001)]
        self.assertAlmostEqual(fmean(first), 0.0, delta=0.6)
        self.assertAlmostEqual(pstdev(first), (10.0**2 + SCOUT_STEP**2 / 12) ** 0.5, delta=0.6)
        self.assertAlmostEqual(pstdev(fourth), (5.0**2 + SCOUT_STEP**2 / 12) ** 0.5, delta=0.6)
        for player_id in range(1, 300):
            errors = [abs(self.estimate(player_id, truth=47, seasons=seasons) - 47) for seasons in range(1, 16)]
            with self.subTest(player=player_id):
                self.assertEqual(errors, sorted(errors, reverse=True), "在籍が長くなって外れが広がらない")

    def test_zero_or_negative_seasons_count_as_the_first_year(self) -> None:
        self.assertEqual(self.estimate(5, seasons=0), self.estimate(5, seasons=1))

    def test_each_club_has_its_own_view(self) -> None:
        differ = sum(
            self.estimate(player_id, team_id=1) != self.estimate(player_id, team_id=2) for player_id in range(1, 201)
        )
        self.assertGreater(differ, 100)

    def test_an_exact_scout_sees_the_truth(self) -> None:
        for truth in (12, 50, 77, 90):
            ratings = BatterRatings(potential=Potential(truth))
            estimate = roster_potential_estimate(
                ratings, world_seed=SEED, team_id=TEAM, player_id=9, seasons=1, accuracy=EXACT
            )
            with self.subTest(truth=truth):
                self.assertEqual(estimate.value, max(SCOUT_STEP, SCOUT_STEP * round(truth / SCOUT_STEP)))

    def test_a_missing_potential_is_seen_as_the_value_closing_will_write(self) -> None:
        """伸びしろの無い行（#308 より前の世界）は、締める処理が翌年に書くのと同じ値を本当の値として見る
        （書き込みはしない）。"""
        missing = BatterRatings()
        for player_id in range(1, 30):
            filled = with_filled_potential(missing, player_id=player_id, world_seed=SEED)
            with self.subTest(player=player_id):
                self.assertEqual(
                    roster_potential_estimate(missing, world_seed=SEED, team_id=TEAM, player_id=player_id, seasons=3),
                    roster_potential_estimate(filled, world_seed=SEED, team_id=TEAM, player_id=player_id, seasons=3),
                )
        self.assertIsNone(missing.potential, "受け取った能力は書き換えない")


class RosterProjectionTest(unittest.TestCase):
    def test_from_the_peak_age_the_outlook_is_the_current_overall(self) -> None:
        ratings = PitcherRatings(stuff=72, control=70, home_run_avoidance=71, potential=Potential(90))
        projected = roster_projection(
            ratings, age=PEAK_AGE, position=Position.PITCHER, world_seed=SEED, team_id=TEAM, player_id=1, seasons=1
        )
        self.assertIs(projected, ratings)
        self.assertEqual(outlook_grade(projected), RatingGrade.from_value(round(overall_value(ratings))))

    def test_the_grade_is_none_from_the_peak_age(self) -> None:
        ratings = BatterRatings(contact=60, potential=Potential(90))

        def grade(age: int) -> RatingGrade | None:
            return roster_outlook_grade(
                ratings, age=age, position=Position.INFIELDER, world_seed=SEED, team_id=TEAM, player_id=1, seasons=1
            )

        self.assertIsNone(grade(PEAK_AGE))
        projected = roster_projection(
            ratings,
            age=PEAK_AGE - 1,
            position=Position.INFIELDER,
            world_seed=SEED,
            team_id=TEAM,
            player_id=1,
            seasons=1,
        )
        self.assertEqual(grade(PEAK_AGE - 1), outlook_grade(projected))

    def test_young_players_are_projected_with_the_scouts_estimate(self) -> None:
        ratings = BatterRatings(contact=38, power=36, eye=35, speed=45, fielding=40, potential=Potential(70))
        projected = roster_projection(
            ratings,
            age=18,
            position=Position.INFIELDER,
            world_seed=SEED,
            team_id=TEAM,
            player_id=4,
            seasons=2,
            accuracy=EXACT,
        )
        self.assertEqual(
            projected,
            projected_ratings(ratings, age=18, position=Position.INFIELDER, potential_estimate=Potential(70)),
        )
        self.assertGreater(overall_value(projected), overall_value(ratings))

    def test_a_larger_true_potential_tends_to_a_better_outlook(self) -> None:
        """見立ては誤差つきでも、本当の伸びしろが大きい選手の見込みは平均して高い。"""

        def mean_outlook(truth: int) -> float:
            ratings = BatterRatings(contact=40, power=40, eye=40, speed=40, fielding=40, potential=Potential(truth))
            return fmean(
                overall_value(
                    roster_projection(
                        replace(ratings),
                        age=19,
                        position=Position.OUTFIELDER,
                        world_seed=SEED,
                        team_id=TEAM,
                        player_id=player_id,
                        seasons=1,
                    )
                )
                for player_id in range(1, 400)
            )

        self.assertGreater(mean_outlook(80), mean_outlook(50) + 3)
        self.assertGreater(mean_outlook(50), mean_outlook(20) + 3)


class ClubSeasonsTest(unittest.TestCase):
    """在籍年数の数え方の出典（`club_seasons`）。戦力外の実績（`TrackRecord.of`）と同じ値。"""

    CAREER = (
        Stint(team_id=1, number=JerseyNumber("10"), from_year=2020, to_year=2022),
        Stint(team_id=2, number=JerseyNumber("10"), from_year=2023, to_year=2024),
        Stint(team_id=1, number=JerseyNumber("10"), from_year=2025),
    )

    def test_counts_the_years_at_the_club_up_to_the_year(self) -> None:
        self.assertEqual(club_seasons(self.CAREER, team_id=1, year=2026), 3 + 2)
        self.assertEqual(club_seasons(self.CAREER, team_id=2, year=2026), 2)
        self.assertEqual(club_seasons(self.CAREER, team_id=1, year=2021), 2)
        self.assertEqual(club_seasons(self.CAREER, team_id=3, year=2026), 0)

    def test_is_at_club_only_in_the_years_a_stint_covers(self) -> None:
        self.assertTrue(is_at_club(self.CAREER, team_id=1, year=2021))
        self.assertFalse(is_at_club(self.CAREER, team_id=1, year=2023), "他球団にいた年")
        self.assertTrue(is_at_club(self.CAREER, team_id=1, year=2030), "今の在籍")
        self.assertFalse(is_at_club(self.CAREER, team_id=2, year=2025), "退団した後")

    def test_matches_the_track_record(self) -> None:
        for year in range(2019, 2028):
            record = TrackRecord.of(
                team_id=1,
                career=self.CAREER,
                year=year,
                batting=BattingLine(),
                pitching=PitchingLine(),
                is_captain=False,
                titles=0,
            )
            with self.subTest(year=year):
                self.assertEqual(record.seasons, club_seasons(self.CAREER, team_id=1, year=year))


if __name__ == "__main__":
    unittest.main()
