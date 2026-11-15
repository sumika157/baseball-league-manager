"""シーズン中の外国人の獲得（#169 F3）: AI の規則（`decide_midseason_signings`）と、獲得した外国人の作り方
（`sign_midseason`）。DB 不要。

期待値の根拠: #169 のユーザーの決定 Q3 A・Q4 A（2026-10-25。期中の獲得は1球団1年1人・保有は6人まで・見せるのは
見立てだけ）と F3 の計画（2026-11-14。判定日に昇格の後で、自動の1軍で外国人枠に空きがあり、支配下が上限未満の
球団が、枠の空いた区分の候補から「見立て + 穴」で1人）。補強の窓は7月31日まで（#207・野球協約）。
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date

from myapp.domain.exceptions import InvalidSigning
from myapp.domain.pennant.foreign_market import (
    MAX_FOREIGN_HELD,
    CandidateKey,
    MidseasonForeignClub,
    SigningPhase,
    decide_midseason_signings,
    draw_candidates,
    ensure_signable,
    foreign_openings,
    midseason_key,
    sign_midseason,
)
from myapp.domain.simulation.manager import ClubRoster, SimBatter, SimPitcher
from myapp.domain.simulation.ratings import BatterRatings, PitcherRatings
from myapp.domain.value_objects import AcquisitionRoute, ContractStatus, ForeignPlayerRules, Position

YEAR = 2027
JUNE_1 = date(YEAR, 6, 1)
JULY_1 = date(YEAR, 7, 1)
SEED = 5
LIMIT = 70

PITCHERS_AND_BATTERS = frozenset({Position.PITCHER, Position.INFIELDER, Position.OUTFIELDER})


def pitching(value: int) -> PitcherRatings:
    return PitcherRatings(stuff=value, control=value, home_run_avoidance=value, stamina=50)


def batting(value: int) -> BatterRatings:
    return BatterRatings(contact=value, power=value, eye=value, speed=value, fielding=value)


def pool(
    team_id: int = 1,
    *,
    foreign_pitchers: int = 0,
    foreign_batters: int = 0,
    pitcher_value: int = 50,
    batter_value: int = 50,
) -> ClubRoster:
    """登録候補（投手15人・野手15人＝捕手2・内野7・外野6）。外国人は能力の高い（1軍に入る）選手として先頭に置く。"""
    pitchers = [
        SimPitcher(team_id * 1000 + n, f"投手{n}", pitching(70), counts_as_foreign=True)
        for n in range(foreign_pitchers)
    ]
    pitchers += [
        SimPitcher(team_id * 1000 + n, f"投手{n}", pitching(pitcher_value)) for n in range(foreign_pitchers, 15)
    ]
    positions = [Position.CATCHER] * 2 + [Position.INFIELDER] * 7 + [Position.OUTFIELDER] * 6
    batters = [
        SimBatter(
            team_id * 1000 + 100 + n,
            f"野手{n}",
            position,
            batting(70 if n - 2 < foreign_batters and n >= 2 else batter_value),
            counts_as_foreign=2 <= n < 2 + foreign_batters,
            throws=None,
        )
        for n, position in enumerate(positions)
    ]
    return ClubRoster(team_id=team_id, name=f"球団{team_id}", batters=tuple(batters), pitchers=tuple(pitchers))


def club(team_id: int = 1, *, roster: ClubRoster | None = None, **changes) -> MidseasonForeignClub:
    base = MidseasonForeignClub(
        team_id=team_id,
        is_managed=False,
        pool=pool(team_id) if roster is None else roster,
        foreign_rules=ForeignPlayerRules.NPB,
        held_foreign=0,
        registered_count=65,
        registered_limit=LIMIT,
        signed_on=(),
    )
    return replace(base, **changes)


def signed(clubs, *, on: date = JUNE_1, names=()) -> list[tuple[int, Position]]:
    chosen = decide_midseason_signings(clubs, on=on, world_seed=SEED, used_names=names)
    return [(candidate.team_id, candidate.position) for candidate in chosen]


class ForeignOpeningsTest(unittest.TestCase):
    def test_a_club_without_foreign_players_has_every_position_open_but_catcher(self):
        self.assertEqual(foreign_openings(pool(), ForeignPlayerRules.NPB), PITCHERS_AND_BATTERS)

    def test_a_full_quota_has_no_opening(self):
        """1軍で外国人枠に数える選手が計（4人）に達していれば空きは無い。"""
        full = pool(foreign_pitchers=2, foreign_batters=2)
        self.assertEqual(foreign_openings(full, ForeignPlayerRules.NPB), frozenset())

    def test_only_the_side_with_room_is_open(self):
        """投手の枠（3人）が埋まっていれば野手だけ、野手の枠が埋まっていれば投手だけ。"""
        self.assertEqual(
            foreign_openings(pool(foreign_pitchers=3), ForeignPlayerRules.NPB),
            frozenset({Position.INFIELDER, Position.OUTFIELDER}),
        )
        self.assertEqual(
            foreign_openings(pool(foreign_batters=3), ForeignPlayerRules.NPB), frozenset({Position.PITCHER})
        )

    def test_an_unlimited_quota_has_no_target_to_fill(self):
        self.assertEqual(foreign_openings(pool(), ForeignPlayerRules.UNLIMITED), frozenset())


class DecideMidseasonSigningsTest(unittest.TestCase):
    def test_a_club_with_room_signs_one_midseason_candidate(self):
        (candidate,) = decide_midseason_signings([club()], on=JUNE_1, world_seed=SEED, used_names=())
        self.assertEqual(candidate.key.phase, SigningPhase.MIDSEASON)
        self.assertEqual((candidate.key.year, candidate.key.nth), (YEAR, 0))
        self.assertIn(candidate.position, PITCHERS_AND_BATTERS)

    def test_no_signing_without_an_opening_on_the_active_roster(self):
        full = pool(foreign_pitchers=2, foreign_batters=2)
        self.assertEqual(signed([club(roster=full)]), [])
        self.assertEqual(signed([club(foreign_rules=ForeignPlayerRules.UNLIMITED)]), [])

    def test_only_positions_with_room_are_considered(self):
        """投手の枠が埋まっていれば野手、野手の枠が埋まっていれば投手を獲得する。"""
        for seed in range(5):
            with self.subTest(seed=seed):
                (batter,) = decide_midseason_signings(
                    [club(roster=pool(foreign_pitchers=3))], on=JUNE_1, world_seed=seed, used_names=()
                )
                self.assertIsNot(batter.position, Position.PITCHER)
                (pitcher,) = decide_midseason_signings(
                    [club(roster=pool(foreign_batters=3))], on=JUNE_1, world_seed=seed, used_names=()
                )
                self.assertIs(pitcher.position, Position.PITCHER)

    def test_the_managed_club_is_left_to_the_gm(self):
        self.assertEqual([team_id for team_id, _ in signed([club(1, is_managed=True), club(2)])], [2])

    def test_the_held_limit(self):
        """外国人の保有（育成を含む）が6人なら獲得しない。5人なら獲得する。"""
        self.assertEqual(MAX_FOREIGN_HELD, 6)
        self.assertEqual(signed([club(held_foreign=MAX_FOREIGN_HELD)]), [])
        self.assertEqual(len(signed([club(held_foreign=MAX_FOREIGN_HELD - 1)])), 1)

    def test_the_registered_limit(self):
        """支配下が上限なら獲得しない（期中の昇格が空けておいた1枠を使う）。上限が無ければ制限しない。"""
        self.assertEqual(signed([club(registered_count=LIMIT)]), [])
        self.assertEqual(len(signed([club(registered_count=LIMIT - 1)])), 1)
        self.assertEqual(len(signed([club(registered_count=LIMIT + 5, registered_limit=None)])), 1)

    def test_one_a_year(self):
        """1年1人まで。数えるのはその年の獲得（前の年の獲得は数えない）。"""
        self.assertEqual(signed([club(signed_on=(date(YEAR, 6, 2),))], on=JULY_1), [])
        self.assertEqual(len(signed([club(signed_on=(date(YEAR - 1, 6, 2),))], on=JULY_1)), 1)

    def test_a_review_date_already_done_is_not_repeated(self):
        """その判定日の獲得の登録がもうあれば、やり直しても2人目を獲得しない。"""
        self.assertEqual(signed([club(signed_on=(JUNE_1,))]), [])

    def test_nobody_is_signed_outside_the_window(self):
        self.assertEqual(len(signed([club()], on=date(YEAR, 7, 31))), 1)
        self.assertEqual(signed([club()], on=date(YEAR, 8, 1)), [])

    def test_the_hole_decides_the_side(self):
        """穴の基準は世界の全球団の平均。投手陣の弱い球団は投手を、打線の弱い球団は野手を取る
        （#169 F1b と同じ評価）。"""
        weak_pitching = club(1, roster=pool(1, pitcher_value=35))
        weak_batting = club(2, roster=pool(2, batter_value=35))
        result = dict(signed([weak_pitching, weak_batting, club(3, roster=pool(3))]))
        self.assertIs(result[1], Position.PITCHER)
        self.assertIsNot(result[2], Position.PITCHER)

    def test_the_choice_does_not_depend_on_the_order_of_clubs(self):
        clubs = [club(team_id) for team_id in (3, 1, 2)]
        forward = decide_midseason_signings(clubs, on=JUNE_1, world_seed=SEED, used_names=())
        backward = decide_midseason_signings(clubs[::-1], on=JUNE_1, world_seed=SEED, used_names=())
        self.assertEqual(forward, backward)
        self.assertEqual([candidate.team_id for candidate in forward], [1, 2, 3])

    def test_the_choice_is_a_signable_candidate(self):
        (candidate,) = decide_midseason_signings([club()], on=JUNE_1, world_seed=SEED, used_names=())
        drawn = draw_candidates(1, year=YEAR, phase=SigningPhase.MIDSEASON, world_seed=SEED, used_names=())
        self.assertIn(candidate, drawn)
        ensure_signable([candidate.key], drawn, held_foreign=0, phase=SigningPhase.MIDSEASON)


class SignMidseasonTest(unittest.TestCase):
    def candidate(self, team_id: int = 1, nth: int = 0):
        drawn = draw_candidates(
            team_id, year=YEAR, phase=SigningPhase.MIDSEASON, world_seed=SEED, used_names=(), nth=nth
        )
        return drawn[0]

    def sign(self, candidate, *, names: set[str] | None = None, numbers=frozenset({"1", "2"})):
        return sign_midseason(
            candidate,
            world_seed=SEED,
            used_names=set() if names is None else names,
            used_numbers=numbers,
            surname_counts={},
        )

    def test_a_new_foreign_player_on_the_registered_roster_with_the_candidate_ratings(self):
        candidate = self.candidate()
        signing = self.sign(candidate)
        self.assertIs(signing.acquisition_route, AcquisitionRoute.NEW_FOREIGN)
        self.assertIs(signing.contract, ContractStatus.REGISTERED)
        self.assertEqual(signing.ratings, candidate.ratings, "錨をかけない（締めるときに世界全体にかかる）")
        self.assertEqual(signing.position, candidate.position)
        self.assertTrue(signing.profile.is_foreign_player)

    def test_the_number_is_free_and_derived_again_the_same(self):
        taken = frozenset(str(n) for n in range(1, 60))
        signing = self.sign(self.candidate(), numbers=taken)
        self.assertNotIn(signing.number, taken)
        self.assertEqual(self.sign(self.candidate(), numbers=taken).number, signing.number)

    def test_a_clashing_name_is_drawn_again_and_recorded(self):
        candidate = self.candidate()
        names = {candidate.name}
        signing = self.sign(candidate, names=names)
        self.assertNotEqual(signing.name, candidate.name)
        self.assertIn(signing.name, names)
        self.assertEqual(signing.ratings, candidate.ratings)

    def test_only_midseason_candidates(self):
        offseason = draw_candidates(1, year=YEAR, phase=SigningPhase.OFFSEASON, world_seed=SEED, used_names=())[0]
        with self.assertRaises(ValueError):
            self.sign(offseason)

    def test_the_key_counts_the_signings_of_the_year(self):
        """候補の鍵の何回目はその年に獲得した人数（獲得するたびに別の候補。上限で2人目は獲得できない）。"""
        self.assertEqual(midseason_key(YEAR, 1, 3), CandidateKey(YEAR, SigningPhase.MIDSEASON, 1, 3))
        second = draw_candidates(1, year=YEAR, phase=SigningPhase.MIDSEASON, world_seed=SEED, used_names=(), nth=1)
        with self.assertRaises(InvalidSigning):
            ensure_signable(
                [second[0].key], second, held_foreign=0, phase=SigningPhase.MIDSEASON, signed_this_season=1
            )
