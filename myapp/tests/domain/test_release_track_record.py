"""戦力外の「実績の加点」（`release.track_record_credit`）と、その材料（`TrackRecord.of`）。DB 不要。

2026-10-12 ユーザー決定: ベテランの能力で測れない価値（生え抜き・その球団での在籍年数・通算の実績・主将）を
残す価値に足す。効果は外されにくくなるだけ。
"""

from __future__ import annotations

import unittest
from dataclasses import replace

from myapp.domain.entities import Stint
from myapp.domain.pennant import release
from myapp.domain.pennant.offseason_player import TrackRecord
from myapp.domain.pennant.release import (
    CAPTAIN_CREDIT,
    CAREER_CREDIT,
    HITS_PER_POINT,
    HOMEGROWN_CREDIT,
    MAX_TRACK_RECORD_CREDIT,
    SAVES_PER_POINT,
    SEASON_CREDIT,
    WINS_PER_POINT,
    career_points,
    foreign_prospect_value,
    keep_value,
    track_record_credit,
)
from myapp.domain.value_objects import (
    AcquisitionRoute,
    BattingLine,
    ContractStatus,
    JerseyNumber,
    PitchingLine,
    Position,
)
from myapp.tests.domain.test_release import START_YEAR, YEAR, member, releases, roster

TEAM = 1
OTHER = 2


def stint(team_id: int, from_year: int, to_year: int | None = None, via: AcquisitionRoute | None = None) -> Stint:
    if via is AcquisitionRoute.DEVELOPMENTAL_DRAFT:
        return Stint(
            team_id=team_id,
            number=JerseyNumber("120"),
            from_year=from_year,
            to_year=to_year,
            signed_as=ContractStatus.DEVELOPMENTAL,
            acquired_via=via,
        )
    return Stint(team_id=team_id, number=JerseyNumber("10"), from_year=from_year, to_year=to_year, acquired_via=via)


def record_of(career: list[Stint], *, hits: int = 0, wins: int = 0, captain: bool = False) -> TrackRecord:
    return TrackRecord.of(
        team_id=TEAM,
        career=career,
        year=YEAR,
        batting=BattingLine(at_bats=hits, singles=hits),
        pitching=PitchingLine(wins=wins),
        is_captain=captain,
    )


class TrackRecordOfTest(unittest.TestCase):
    """材料は在籍・世界の通算成績・主将から導く（保存しない）。"""

    def test_seasons_count_the_years_with_the_club_up_to_the_closing_year(self) -> None:
        self.assertEqual(record_of([stint(TEAM, YEAR - 3)]).seasons, 4)
        self.assertEqual(record_of([stint(TEAM, YEAR)]).seasons, 1, "加入した年に締めれば1年")

    def test_seasons_add_up_separate_stints_with_the_club(self) -> None:
        career = [stint(TEAM, YEAR - 6, YEAR - 5), stint(OTHER, YEAR - 4, YEAR - 3), stint(TEAM, YEAR - 2)]
        self.assertEqual(record_of(career).seasons, 2 + 3, "ほかの球団にいた年は数えない")

    def test_homegrown_is_drafted_by_the_club_and_never_elsewhere(self) -> None:
        self.assertTrue(record_of([stint(TEAM, YEAR - 5, via=AcquisitionRoute.DRAFT)]).homegrown)
        self.assertTrue(record_of([stint(TEAM, YEAR - 5, via=AcquisitionRoute.DEVELOPMENTAL_DRAFT)]).homegrown)

    def test_not_homegrown(self) -> None:
        cases = {
            "入団の経路が不明（分岐した選手）": [stint(TEAM, YEAR - 5)],
            "新外国人": [stint(TEAM, YEAR - 5, via=AcquisitionRoute.NEW_FOREIGN)],
            "ほかの球団から来た": [stint(OTHER, YEAR - 8, YEAR - 6, AcquisitionRoute.DRAFT), stint(TEAM, YEAR - 5)],
            "ほかの球団へ出て戻った": [
                stint(TEAM, YEAR - 8, YEAR - 6, AcquisitionRoute.DRAFT),
                stint(OTHER, YEAR - 5, YEAR - 4),
                stint(TEAM, YEAR - 3),
            ],
        }
        for label, career in cases.items():
            with self.subTest(label):
                self.assertFalse(record_of(career).homegrown)

    def test_career_totals_and_captain_are_copied(self) -> None:
        record = record_of([stint(TEAM, YEAR)], hits=120, wins=7, captain=True)
        self.assertEqual((record.hits, record.wins, record.is_captain), (120, 7, True))


class TrackRecordCreditTest(unittest.TestCase):
    def test_no_record_no_credit(self) -> None:
        self.assertEqual(track_record_credit(TrackRecord()), 0.0)

    def test_a_regular_season_is_about_one_point(self) -> None:
        self.assertAlmostEqual(career_points(TrackRecord(hits=HITS_PER_POINT)), 1.0)
        self.assertAlmostEqual(career_points(TrackRecord(wins=WINS_PER_POINT, saves=SAVES_PER_POINT)), 2.0)

    def test_the_credit_adds_up_the_parts(self) -> None:
        record = TrackRecord(homegrown=True, seasons=2, hits=HITS_PER_POINT, is_captain=True)
        expected = CAREER_CREDIT + 2 * SEASON_CREDIT + HOMEGROWN_CREDIT + CAPTAIN_CREDIT
        self.assertAlmostEqual(track_record_credit(record), min(MAX_TRACK_RECORD_CREDIT, expected))

    def test_the_credit_is_capped(self) -> None:
        legend = TrackRecord(homegrown=True, seasons=15, hits=2000, home_runs=400, is_captain=True)
        self.assertEqual(track_record_credit(legend), MAX_TRACK_RECORD_CREDIT)


class CreditInReleasesTest(unittest.TestCase):
    """実績は外されにくくするだけ（同じ能力なら、実績のある選手が残る）。"""

    def test_keep_value_adds_the_credit(self) -> None:
        plain = member(1, value=45, age=33)
        veteran = replace(plain, track_record=TrackRecord(seasons=8, hits=900))
        self.assertAlmostEqual(
            keep_value(veteran, year=YEAR, start_year=START_YEAR)
            - keep_value(plain, year=YEAR, start_year=START_YEAR),
            track_record_credit(veteran.track_record),
        )

    def test_a_veteran_with_a_record_outlasts_an_equal_player_without(self) -> None:
        """能力が少し劣るベテランも、実績の加点で控えの平均（50）を上回れば残る。"""
        players = roster(61)
        plain = member(201, value=48, age=33)
        veteran = replace(member(202, value=48, age=33), track_record=TrackRecord(seasons=8, hits=900))
        self.assertGreater(track_record_credit(veteran.track_record), 2)
        self.assertEqual(releases([*players, plain, veteran]), (201, *(p.player_id for p in players[:2])))

    def test_the_credit_does_not_save_a_player_far_below(self) -> None:
        """頭打ちがあるので、能力の大きく落ちた選手は実績があっても外れる。"""
        players = roster(63)
        faded = replace(member(201, value=20, age=36), track_record=TrackRecord(seasons=15, hits=2000))
        self.assertIn(201, releases([*players, faded]))

    def test_the_foreign_replacement_ignores_the_record(self) -> None:
        """外国人の入れ替えは能力で測れる価値だけで比べる（実績があっても、見込みを下回れば入れ替える）。

        実績を足すと、世界の中で活躍した外国人がほとんど入れ替わらなくなる（実測で外国人の戦力外が約半分に減った）。
        """
        prospect = foreign_prospect_value(Position.PITCHER)
        value = round(prospect - min(1.0, MAX_TRACK_RECORD_CREDIT / 2))
        newcomer = member(101, position=Position.PITCHER, value=value, age=30, foreign=True)
        star = replace(
            member(102, position=Position.PITCHER, value=value, age=30, foreign=True),
            track_record=TrackRecord(seasons=5, wins=60),
        )
        self.assertGreater(keep_value(star, year=YEAR, start_year=START_YEAR), prospect)
        self.assertEqual(releases([*roster(55), newcomer, star]), (101, 102))


class CreditIsTheOnlyChangeTest(unittest.TestCase):
    """加点を0にすれば、段階A の規則と同じ選手を外す（加点は残す価値の上乗せだけ）。"""

    def test_zero_credit_means_the_stage_a_rule(self) -> None:
        players = [
            replace(p, track_record=TrackRecord(seasons=index % 7, hits=index * 40))
            for index, p in enumerate(roster(72))
        ]
        saved = release.MAX_TRACK_RECORD_CREDIT
        try:
            release.MAX_TRACK_RECORD_CREDIT = 0.0
            without = releases(players)
        finally:
            release.MAX_TRACK_RECORD_CREDIT = saved
        plain = [replace(p, track_record=TrackRecord()) for p in players]
        self.assertEqual(without, releases(plain))


if __name__ == "__main__":
    unittest.main()
