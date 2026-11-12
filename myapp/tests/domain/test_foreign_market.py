"""外国人の獲得（`domain/pennant/foreign_market.py`。#169 F1）と、締める計算の中の順序（`offseason.py`）。DB 不要。

母集団での分布（獲得した外国人の平均・1球団の外国人の人数）は `test_offseason_population.py` が見る。
"""

from __future__ import annotations

import random
import unittest
from collections import Counter
from collections.abc import Sequence
from dataclasses import replace
from datetime import date
from statistics import fmean

from myapp.domain.exceptions import InvalidSigning
from myapp.domain.pennant import draft, foreign_market
from myapp.domain.pennant.draft import DraftTeam
from myapp.domain.pennant.draft_meeting import DraftContext, run_draft, scouting_value
from myapp.domain.pennant.draft_order import DraftOrderRule, DraftStandings
from myapp.domain.pennant.foreign_market import (
    BACKGROUNDS,
    CANDIDATES_BY_POSITION,
    HOLE_WEIGHT,
    MAX_FOREIGN_HELD,
    VACANT_RATING,
    CandidateKey,
    SigningPhase,
    SquadUnit,
    choose_signings,
    draw_candidates,
    ensure_signable,
    expected_signing_ages,
    expected_signing_ratings,
    offseason_signings,
    position_hole,
    squad_holes,
    squad_strength,
    world_strength,
)
from myapp.domain.pennant.offseason import OffseasonClub, OffseasonPlayer, plan_departures, plan_offseason
from myapp.domain.pennant.prospects import expected_prospect_ratings
from myapp.domain.pennant.ratings import PlayerRatings
from myapp.domain.pennant.retirement import PlayingTime
from myapp.domain.pennant.roster import OPENING_REGISTERED_BAND
from myapp.domain.simulation.ratings import BatterRatings, PitcherRatings
from myapp.domain.value_objects import (
    AcquisitionRoute,
    ContractStatus,
    DraftPick,
    ForeignBackground,
    ForeignPlayerRules,
    Position,
    Profile,
)
from myapp.domain.virtual_players import generator

YEAR = 2027
START_YEAR = 2026
SEED = 3


def team(team_id: int = 1, *, foreign: int = 0, size: int = 59, numbers: frozenset[str] = frozenset()) -> DraftTeam:
    """帯の中（59人。新人6人）の球団。外国人は `foreign` 人（目標は4人）。"""
    return DraftTeam(
        team_id=team_id,
        position_counts=generator.largest_remainder(size, generator.POSITION_RATIOS),
        foreign_count=foreign,
        foreign_limit=ForeignPlayerRules.NPB.active,
        used_numbers=numbers,
        surname_counts={},
        registered_limit=70,
    )


def candidates(team_id: int = 1, **overrides: object) -> tuple[foreign_market.ForeignCandidate, ...]:
    options: dict = {"year": YEAR, "phase": SigningPhase.OFFSEASON, "world_seed": SEED, "used_names": ()}
    options.update(overrides)
    return draw_candidates(team_id, **options)


def abilities(drawn: tuple[foreign_market.ForeignCandidate, ...]) -> list[tuple]:
    """名前を除いた候補（能力・年齢・位置・前所属）。"""
    return [(c.key, c.position, c.background, c.age, c.ratings, c.profile.birth_date) for c in drawn]


class CandidateTest(unittest.TestCase):
    def test_eight_candidates_by_position_and_no_catcher(self) -> None:
        drawn = candidates()
        self.assertEqual(len(drawn), 8)
        self.assertEqual(Counter(c.position for c in drawn), Counter(CANDIDATES_BY_POSITION))
        self.assertNotIn(Position.CATCHER, {c.position for c in drawn})
        self.assertEqual([c.key.index for c in drawn], list(range(8)))

    def test_candidates_are_derived_again_the_same(self) -> None:
        self.assertEqual(candidates(), candidates())

    def test_candidates_change_with_the_team_year_phase_and_turn(self) -> None:
        base = abilities(candidates())
        for changed in (
            candidates(2),
            candidates(year=YEAR + 1),
            candidates(phase=SigningPhase.MIDSEASON),
            candidates(nth=1),
            candidates(world_seed=SEED + 1),
        ):
            with self.subTest(key=changed[0].key):
                self.assertNotEqual([c.ratings for c in changed], [r[4] for r in base])

    def test_renaming_does_not_move_the_abilities(self) -> None:
        """名前は能力と別の流れ。既にいる名前を避けて引き直しても、能力・年齢・位置・前所属は変わらない。"""
        first = candidates()
        again = candidates(used_names={c.name for c in first})
        self.assertEqual(abilities(again), abilities(first))
        self.assertFalse({c.name for c in first} & {c.name for c in again})

    def test_names_are_unique_within_the_candidates(self) -> None:
        for seed in range(30):
            drawn = candidates(world_seed=seed)
            self.assertEqual(len({c.name for c in drawn}), len(drawn))

    def test_the_profile_is_a_foreign_player_with_the_background(self) -> None:
        as_of = date(YEAR + 1, 4, 1)
        for seed in range(30):
            for c in candidates(world_seed=seed):
                low, high = BACKGROUNDS[c.background].age_range
                self.assertTrue(low <= c.age <= high)
                self.assertEqual(c.profile.age(as_of), c.age)
                self.assertTrue(c.profile.is_foreign_player)
                self.assertIs(c.profile.foreign_background, c.background)
                self.assertEqual(c.profile.nationality, c.profile.birthplace)
                self.assertEqual((c.profile.high_school, c.profile.draft), ("", None))
                self.assertIsNotNone(c.ratings.potential)
                self.assertIsInstance(c.ratings, PitcherRatings if c.position.is_pitcher else BatterRatings)

    def test_midseason_candidates_enter_the_same_year(self) -> None:
        for c in candidates(phase=SigningPhase.MIDSEASON):
            self.assertEqual(c.profile.age(date(YEAR, 4, 1)), c.age)

    def test_the_backgrounds_mix_into_the_old_foreign_rookies(self) -> None:
        """混ぜた全体は今までの外国人の新人（平均のずらし0・25〜31歳・平均28歳）に近い。区分はどれも引かれる。"""
        total = sum(item.weight for item in BACKGROUNDS.values())
        shift = sum(item.weight * item.rating_shift for item in BACKGROUNDS.values()) / total
        self.assertLess(abs(shift), 0.5)
        ages = expected_signing_ages()
        self.assertAlmostEqual(sum(ages.values()), 1.0)
        self.assertEqual((min(ages), max(ages)), (25, 31))
        self.assertAlmostEqual(sum(age * share for age, share in ages.items()), 28.0, delta=0.3)
        seen = Counter(c.background for seed in range(40) for c in candidates(world_seed=seed))
        self.assertEqual(set(seen), set(ForeignBackground))
        self.assertGreater(seen[ForeignBackground.MINOR_LEAGUE], seen[ForeignBackground.ASIAN_LEAGUE])

    def test_the_background_shifts_the_ratings(self) -> None:
        values: dict[ForeignBackground, list[float]] = {item: [] for item in ForeignBackground}
        for seed in range(150):
            for c in candidates(world_seed=seed):
                values[c.background].append(scouting_value(c.ratings) - _expected_value(c.position))
        means = {item: fmean(items) for item, items in values.items()}
        self.assertGreater(means[ForeignBackground.MAJOR_LEAGUE], means[ForeignBackground.ASIAN_LEAGUE])
        self.assertGreater(means[ForeignBackground.ASIAN_LEAGUE], means[ForeignBackground.MINOR_LEAGUE])


def _expected_value(position: Position) -> float:
    return scouting_value(expected_prospect_ratings(mean=foreign_market.SIGNED_RATING_MEAN, position=position))


class ExpectedSigningTest(unittest.TestCase):
    """戦力外の見込み（`release.foreign_prospect_value`）の出典。"""

    def test_the_expected_ratings_are_the_signed_mean(self) -> None:
        for position in (Position.PITCHER, Position.INFIELDER, Position.OUTFIELDER, Position.DESIGNATED_HITTER):
            self.assertEqual(
                expected_signing_ratings(position),
                expected_prospect_ratings(mean=foreign_market.SIGNED_RATING_MEAN, position=position),
            )

    def test_catchers_are_not_signed(self) -> None:
        with self.assertRaises(ValueError):
            expected_signing_ratings(Position.CATCHER)


def squad(*, infield: int = 50, outfield: int = 50, pitching: int = 50) -> tuple:
    """内野6・外野5・投手15人が同じ能力の戦力（捕手3人は穴に効かない）。"""
    batter = {
        position: BatterRatings(contact=v, power=v, eye=v, speed=v)
        for position, v in ((Position.INFIELDER, infield), (Position.OUTFIELDER, outfield), (Position.CATCHER, 50))
    }
    pitcher = PitcherRatings(stuff=pitching, control=pitching, home_run_avoidance=pitching)
    return (
        *((Position.INFIELDER, batter[Position.INFIELDER]) for _ in range(6)),
        *((Position.OUTFIELDER, batter[Position.OUTFIELDER]) for _ in range(5)),
        *((Position.CATCHER, batter[Position.CATCHER]) for _ in range(3)),
        *((Position.PITCHER, pitcher) for _ in range(15)),
    )


def team_with(squad_: tuple, team_id: int = 1, *, foreign: int = 0) -> DraftTeam:
    return replace(team(team_id, foreign=foreign), squad=squad_)


AVERAGE = world_strength([squad()])


def values_of(drawn: Sequence[foreign_market.ForeignCandidate]) -> dict:
    return {c.key: scouting_value(foreign_market.scouting_report(c, world_seed=SEED).ratings) for c in drawn}


class SquadTest(unittest.TestCase):
    """戦力（単位ごとの上位の平均）と穴（#169 F1b）。"""

    def test_units_take_the_top_of_each_position(self) -> None:
        def batter(value: int) -> BatterRatings:
            return BatterRatings(contact=value, power=value, eye=value, speed=value)

        players = (
            *((Position.INFIELDER, batter(v)) for v in (60, 55, 50, 45, 40)),
            (Position.OUTFIELDER, batter(60)),
            (Position.OUTFIELDER, batter(50)),
            (Position.CATCHER, batter(80)),
            (Position.DESIGNATED_HITTER, batter(80)),
        )
        strength = squad_strength(players)
        self.assertAlmostEqual(strength[SquadUnit.INFIELD], (60 + 55 + 50 + 45) / 4)
        self.assertAlmostEqual(strength[SquadUnit.OUTFIELD], (60 + 50 + VACANT_RATING) / 3, msg="空きは2軍の層の水準")
        self.assertEqual(strength[SquadUnit.ROTATION], VACANT_RATING)
        self.assertEqual(strength[SquadUnit.BULLPEN], VACANT_RATING)

    def test_the_rotation_takes_the_starters_and_the_bullpen_the_rest(self) -> None:
        """先発はスタミナ込みの `starter_value` の上位6人、救援はそれを除いた抑える力の上位。"""
        workhorses = [PitcherRatings(stuff=50, control=50, home_run_avoidance=50, stamina=80) for _ in range(6)]
        closer = PitcherRatings(stuff=60, control=60, home_run_avoidance=60, stamina=10)
        strength = squad_strength([(Position.PITCHER, r) for r in (*workhorses, closer)])
        self.assertAlmostEqual(strength[SquadUnit.ROTATION], workhorses[0].starter_value)
        self.assertGreater(closer.pitching_value, workhorses[0].pitching_value)
        self.assertAlmostEqual(strength[SquadUnit.BULLPEN], (60 + VACANT_RATING * 5) / 6)

    def test_holes_are_the_world_average_minus_the_club(self) -> None:
        reference = world_strength([squad(pitching=40), squad(pitching=60)])
        holes = squad_holes(squad(pitching=40), reference)
        self.assertGreater(holes[SquadUnit.ROTATION], 0, "弱い側は + ")
        self.assertGreater(holes[SquadUnit.BULLPEN], 0)
        self.assertAlmostEqual(holes[SquadUnit.INFIELD], 0)
        self.assertLess(squad_holes(squad(pitching=60), reference)[SquadUnit.ROTATION], 0)
        self.assertAlmostEqual(
            position_hole(holes, Position.PITCHER), (holes[SquadUnit.ROTATION] + holes[SquadUnit.BULLPEN]) / 2
        )
        self.assertEqual(position_hole(holes, Position.CATCHER), 0.0, "外国人を獲得しない位置")


class ChooseTest(unittest.TestCase):
    def test_the_ai_takes_the_best_estimate_without_a_hole(self) -> None:
        drawn = candidates()
        values = values_of(drawn)
        (chosen,) = choose_signings(drawn, 1, team_with(squad()), reference=AVERAGE, world_seed=SEED)
        self.assertEqual(values[chosen.key], max(values.values()))

    def test_a_weak_pitching_staff_signs_pitchers_and_a_weak_lineup_signs_batters(self) -> None:
        """投手陣の弱い球団は投手を、打線の弱い球団は野手を取る（世界の平均と比べた穴）。"""
        weak_pitching = squad(pitching=35)
        weak_lineup = squad(infield=35, outfield=35)
        reference = world_strength([weak_pitching, weak_lineup])
        drawn = candidates()
        pitchers = choose_signings(drawn, 2, team_with(weak_pitching), reference=reference, world_seed=SEED)
        batters = choose_signings(drawn, 2, team_with(weak_lineup), reference=reference, world_seed=SEED)
        self.assertEqual({c.position for c in pitchers}, {Position.PITCHER})
        self.assertNotIn(Position.PITCHER, {c.position for c in batters})

    def test_each_signing_fills_the_hole_before_the_next(self) -> None:
        """1人獲得するごとに、その候補の見立てを戦力に入れて穴を数え直す（基準は動かさない）。"""
        for infield in (38, 42, 46, 50):
            drawn = candidates()
            values = values_of(drawn)
            club = team_with(squad(infield=infield))
            reference = world_strength([squad()])
            first, second = choose_signings(drawn, 2, club, reference=reference, world_seed=SEED)
            filled = (*club.squad, (first.position, foreign_market.scouting_report(first, world_seed=SEED).ratings))
            holes = squad_holes(filled, reference)
            rest = [c for c in drawn if c is not first]
            expected = max(
                rest, key=lambda c: (values[c.key] + HOLE_WEIGHT * position_hole(holes, c.position), -c.key.index)
            )
            with self.subTest(infield=infield):
                self.assertEqual(second, expected)

    def test_the_position_minimum_is_a_hard_limit(self) -> None:
        """位置の最低人数に届かなくなる獲得はしない（会議と同じ保証）。投手が新人の枠の分だけ足りなければ、
        打線に穴があっても投手だけを取る。捕手だけが足りないときは、捕手の分の枠を残して獲得を減らす。"""
        counts = {Position.PITCHER: 9, Position.CATCHER: 3, Position.INFIELDER: 25, Position.OUTFIELDER: 22}
        short_pitchers = replace(team_with(squad(infield=30, outfield=30)), position_counts=counts)
        slots, _ = draft.newcomer_plan(short_pitchers)
        self.assertEqual(slots, 6, "前提: 新人の枠6人・投手が6人足りない")
        chosen = choose_signings(candidates(), 4, short_pitchers, reference=AVERAGE, world_seed=SEED)
        self.assertEqual([c.position for c in chosen], [Position.PITCHER] * 4)

        counts = {Position.PITCHER: 27, Position.CATCHER: 0, Position.INFIELDER: 17, Position.OUTFIELDER: 15}
        short_catchers = replace(team_with(squad()), position_counts=counts)
        self.assertEqual(draft.newcomer_plan(short_catchers)[0], 6)
        self.assertEqual(len(choose_signings(candidates(), 4, short_catchers, reference=AVERAGE, world_seed=SEED)), 3)

    def test_the_choice_does_not_depend_on_the_order(self) -> None:
        drawn = list(candidates())
        shuffled = drawn[:]
        random.Random(1).shuffle(shuffled)
        club = team_with(squad(outfield=45))
        self.assertEqual(
            choose_signings(drawn, 3, club, reference=AVERAGE, world_seed=SEED),
            choose_signings(shuffled, 3, club, reference=AVERAGE, world_seed=SEED),
        )

    def test_more_than_the_candidates_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            choose_signings(candidates(), 9, team(), reference=AVERAGE, world_seed=SEED)


class EnsureSignableTest(unittest.TestCase):
    """ゲームの上限（#169 Q4）: 保有は6人まで、シーズン中は1年1人まで。"""

    def setUp(self) -> None:
        self.drawn = candidates()
        self.keys = [c.key for c in self.drawn]

    def test_a_valid_signing_passes(self) -> None:
        ensure_signable(self.keys[:2], self.drawn, held_foreign=MAX_FOREIGN_HELD - 2, phase=SigningPhase.OFFSEASON)
        ensure_signable([], self.drawn, held_foreign=MAX_FOREIGN_HELD, phase=SigningPhase.OFFSEASON)

    def test_the_held_limit(self) -> None:
        with self.assertRaisesRegex(InvalidSigning, "6人まで"):
            ensure_signable(self.keys[:2], self.drawn, held_foreign=MAX_FOREIGN_HELD - 1, phase=SigningPhase.OFFSEASON)

    def test_duplicates_and_unknown_candidates(self) -> None:
        with self.assertRaises(InvalidSigning):
            ensure_signable([self.keys[0]] * 2, self.drawn, held_foreign=0, phase=SigningPhase.OFFSEASON)
        with self.assertRaises(InvalidSigning):
            unknown = CandidateKey(YEAR, SigningPhase.OFFSEASON, 0, 99)
            ensure_signable([unknown], self.drawn, held_foreign=0, phase=SigningPhase.OFFSEASON)

    def test_one_midseason_signing_a_year(self) -> None:
        midseason = candidates(phase=SigningPhase.MIDSEASON)
        keys = [c.key for c in midseason]
        ensure_signable(keys[:1], midseason, held_foreign=0, phase=SigningPhase.MIDSEASON)
        with self.assertRaisesRegex(InvalidSigning, "1年に1人まで"):
            ensure_signable(keys[:1], midseason, held_foreign=0, phase=SigningPhase.MIDSEASON, signed_this_season=1)
        with self.assertRaises(InvalidSigning):
            ensure_signable(keys[:2], midseason, held_foreign=0, phase=SigningPhase.MIDSEASON)


class OffseasonSigningsTest(unittest.TestCase):
    def sign(self, teams: list[DraftTeam], names: set[str] | None = None) -> foreign_market.OffseasonSignings:
        return offseason_signings(teams, year=YEAR, world_seed=SEED, used_names=set() if names is None else names)

    def test_signs_up_to_the_foreign_target_inside_the_newcomers(self) -> None:
        result = self.sign([team(1, foreign=1), team(2, foreign=4), team(3, foreign=0, size=65)])
        by_team = Counter(s.team_id for s in result.signings)
        self.assertEqual(by_team[1], 3)
        self.assertEqual(by_team[2], 0, "目標に達した球団は獲得しない")
        self.assertEqual(by_team[3], 1, "新しい支配下の選手の枠（上端までの1人）の内側")
        for signing in result.signings:
            self.assertIs(signing.acquisition_route, AcquisitionRoute.NEW_FOREIGN)
            self.assertIs(signing.contract, ContractStatus.REGISTERED)
            self.assertEqual(signing.profile.foreign_background, signing.candidate.background)
            self.assertNotIn(signing.position, (Position.CATCHER,))

    def test_teams_after_carry_the_signed_numbers_names_and_count(self) -> None:
        taken = frozenset(str(n) for n in range(1, 50))
        result = self.sign([team(1, foreign=1, numbers=taken), team(2, foreign=4)])
        after = {t.team_id: t for t in result.teams}
        numbers = [s.number for s in result.signings]
        self.assertEqual(len(set(numbers)), len(numbers))
        self.assertFalse(set(numbers) & taken)
        self.assertEqual(after[1].used_numbers, taken | set(numbers))
        self.assertEqual(after[1].foreign_count, 4)
        self.assertEqual(sum(after[1].surname_counts.values()), 3)
        self.assertEqual(after[2], team(2, foreign=4), "獲得しない球団はそのまま")
        self.assertEqual(result.signed_positions, {1: tuple(s.position for s in result.signings)})

    def test_names_never_clash_across_clubs(self) -> None:
        for seed in range(10):
            names: set[str] = {"既存"}
            result = offseason_signings(
                [team(t, foreign=0) for t in range(1, 13)], year=YEAR, world_seed=seed, used_names=names
            )
            signed = [s.name for s in result.signings]
            self.assertEqual(len(set(signed)), len(signed))
            self.assertTrue(set(signed) <= names)

    def test_another_club_does_not_change_a_clubs_signings(self) -> None:
        """候補は球団ごとの種なので、穴の基準（世界の平均）が同じなら、他球団の有無で獲得する選手（能力・年齢・位置）は
        変わらない。"""
        alone = self.sign([team(2, foreign=2)]).signings
        together = [s for s in self.sign([team(1, foreign=0), team(2, foreign=2)]).signings if s.team_id == 2]
        self.assertEqual([s.candidate.key for s in alone], [s.candidate.key for s in together])
        self.assertEqual([s.ratings for s in alone], [s.ratings for s in together])

    def test_a_renamed_signing_keeps_its_name_back_name_and_nationality_together(self) -> None:
        """他球団が同じ名前を先に獲得したときは、名前・読み・国・背ネームをまとめて引き直す（能力は動かない）。"""
        (candidate,) = choose_signings(candidates(), 1, team(), reference=AVERAGE, world_seed=SEED)
        taken = {candidate.name}
        (signing,) = foreign_market._sign(team(), [candidate], year=YEAR, world_seed=SEED, used_names=taken)
        renamed = foreign_market._resolved_name(candidate, 1, world_seed=SEED, used_names={candidate.name})
        self.assertNotEqual(signing.name, candidate.name)
        self.assertEqual((signing.name, signing.profile.name_kana), (renamed.name, renamed.kana))
        self.assertEqual(signing.profile.back_name, renamed.surname_romaji)
        self.assertEqual((signing.profile.nationality, signing.profile.birthplace), (renamed.country, renamed.country))
        self.assertEqual(signing.ratings, candidate.ratings)
        self.assertIn(signing.name, taken)

    def test_the_hole_is_measured_against_the_whole_world(self) -> None:
        """穴の基準は渡した全球団の平均（#169 F1b）。投手陣の弱い球団は投手から、打線の弱い球団は野手から取る。"""
        result = self.sign([team_with(squad(pitching=35), 1), team_with(squad(infield=35, outfield=35), 2)])
        first: dict[int, Position] = {}
        for signing in result.signings:
            first.setdefault(signing.team_id, signing.position)
        self.assertIs(first[1], Position.PITCHER)
        self.assertIsNot(first[2], Position.PITCHER)

    def test_a_club_whose_newcomers_must_all_be_catchers_signs_nobody(self) -> None:
        """新人の枠を捕手の不足だけで使い切る球団は獲得せず、球団もそのまま（会議が捕手を指名する）。"""
        counts = {Position.PITCHER: 31, Position.CATCHER: 0, Position.INFIELDER: 17, Position.OUTFIELDER: 15}
        club = replace(team(1, foreign=0, size=63), position_counts=counts)
        self.assertEqual(draft.newcomer_plan(club)[0], 3, "前提: 新人の枠3人・捕手が3人足りない")
        result = self.sign([club])
        self.assertEqual(result.signings, ())
        self.assertEqual(result.teams, (club,))

    def test_the_held_limit_is_never_crossed_by_the_ai(self) -> None:
        result = self.sign([team(1, foreign=0)])
        self.assertLessEqual(len(result.signings), MAX_FOREIGN_HELD)


def offseason_player(player_id: int, position: Position, *, foreign: bool = False) -> OffseasonPlayer:
    # 外国人は見込み（50）を上回る投手（戦力外の入れ替えで外れない）
    if foreign:
        ratings: PitcherRatings | BatterRatings = PitcherRatings(stuff=70, control=70, home_run_avoidance=70)
    else:
        ratings = PitcherRatings() if position.is_pitcher else BatterRatings()
    return OffseasonPlayer(
        player_id=player_id,
        position=position,
        profile=Profile(birth_date=date(1998, 7, 1), is_foreign_player=foreign, back_name=f"N{player_id}"),
        number=str(player_id),
        joined_year=2020,
        ratings=PlayerRatings(player_id, YEAR, ratings),
        contract=ContractStatus.REGISTERED,
        playing_time=PlayingTime(plate_appearances=400),
    )


def club(team_id: int, *, foreign: int) -> OffseasonClub:
    """支配下59人（帯の中）。先頭の `foreign` 人の投手が外国人。"""
    counts = generator.largest_remainder(59, generator.POSITION_RATIOS)
    positions = [position for position, count in counts.items() for _ in range(count)]
    players = tuple(
        offseason_player(team_id * 100 + index, position, foreign=index < foreign and position.is_pitcher)
        for index, position in enumerate(positions)
    )
    return OffseasonClub(
        team_id=team_id, foreign_rules=ForeignPlayerRules.NPB, registered_player_limit=70, players=players
    )


class PlanOffseasonTest(unittest.TestCase):
    """締める計算の順序（引退 → 戦力外 → 外国人の獲得 → ドラフト会議 → 成長 → 錨）と、
    日本人＝新人 − 獲得した外国人。"""

    @staticmethod
    def context(clubs: list[OffseasonClub]) -> DraftContext:
        return DraftContext(DraftOrderRule.NPB, DraftStandings(leagues=(tuple(c.team_id for c in clubs),)))

    def plan(self, clubs: list[OffseasonClub]):
        return plan_offseason(
            clubs, year=YEAR, world_seed=SEED, start_year=START_YEAR, used_names=(), draft=self.context(clubs)
        )

    def test_japanese_rookies_are_the_newcomers_minus_the_signed_foreigners(self) -> None:
        clubs = [club(1, foreign=0), club(2, foreign=4)]
        result = self.plan(clubs)
        departures = plan_departures(clubs, year=YEAR, world_seed=SEED, start_year=START_YEAR)
        for draft_team in departures.draft_teams:
            count, _ = draft.newcomer_plan(draft_team)
            signed = [s for s in result.signings if s.team_id == draft_team.team_id]
            japanese = [d for d in result.draftees if d.team_id == draft_team.team_id and not d.is_development]
            with self.subTest(team=draft_team.team_id):
                self.assertEqual(len(signed), draft.foreign_count_to_add(draft_team, count))
                self.assertEqual(len(japanese), count - len(signed))
                remaining = draft_team.registered_count
                self.assertLessEqual(remaining + count, OPENING_REGISTERED_BAND.high)

    def test_signed_foreigners_are_anchored_with_the_rest(self) -> None:
        result = self.plan([club(1, foreign=0)])
        self.assertTrue(result.signings, "前提: 外国人を獲得する")
        for signing in result.signings:
            self.assertNotEqual(signing.ratings, signing.candidate.ratings, "錨をかけた後の能力")
            self.assertEqual(signing.ratings.potential, signing.candidate.ratings.potential)

    def test_numbers_do_not_clash_between_signings_and_rookies(self) -> None:
        result = self.plan([club(1, foreign=0)])
        numbers = [s.number for s in result.signings] + [d.number for d in result.draftees]
        self.assertEqual(len(numbers), len(set(numbers)))
        current = {p.number for p in club(1, foreign=0).players if p.player_id not in result.retired}
        self.assertFalse(set(numbers) & current)

    def test_signing_first_does_not_change_the_meeting(self) -> None:
        """獲得は会議の前だが、会議の候補と指名は獲得した外国人の名前・背番号で変わらない（会議が見るのは人数と位置）。"""
        clubs = [club(1, foreign=0), club(2, foreign=4)]
        departures = plan_departures(clubs, year=YEAR, world_seed=SEED, start_year=START_YEAR)
        names: set[str] = set()
        market = offseason_signings(departures.draft_teams, year=YEAR, world_seed=SEED, used_names=names)
        signed = market.signed_positions
        with_market = run_draft(
            market.teams, self.context(clubs), year=YEAR, world_seed=SEED, used_names=names, signed=signed
        )
        without = run_draft(
            departures.draft_teams, self.context(clubs), year=YEAR, world_seed=SEED, used_names=set(), signed=signed
        )
        self.assertEqual(with_market.state.pool.signature, without.state.pool.signature)
        self.assertEqual(
            [(d.name, d.ratings) for d in with_market.draftees], [(d.name, d.ratings) for d in without.draftees]
        )

    def test_a_club_without_a_shortfall_has_the_same_meeting_as_before(self) -> None:
        """外国人が目標に達した球団は獲得せず、会議の人数と位置は獲得の前と同じ（`plan_team_draft` の計画のまま）。"""
        clubs = [club(2, foreign=4)]
        departures = plan_departures(clubs, year=YEAR, world_seed=SEED, start_year=START_YEAR)
        (draft_team,) = departures.draft_teams
        result = self.plan(clubs)
        self.assertEqual(result.signings, ())
        planned = draft.plan_team_draft(draft_team)
        positions = tuple(d.position for d in result.draftees if not d.is_development)
        self.assertEqual(Counter(positions), Counter(planned.japanese_positions))

    def test_the_squad_is_the_registered_players_with_ratings(self) -> None:
        """戦力（穴を測る材料）は引退・戦力外の後の支配下で、能力のある選手だけ（育成は1軍に出られない）。"""
        base = club(1, foreign=0)
        development = replace(offseason_player(998, Position.PITCHER), contract=ContractStatus.DEVELOPMENTAL)
        unrated = replace(offseason_player(999, Position.INFIELDER), ratings=None)
        clubs = [replace(base, players=(*base.players, development, unrated))]
        departures = plan_departures(clubs, year=YEAR, world_seed=SEED, start_year=START_YEAR)
        (draft_team,) = departures.draft_teams
        expected = [
            (p.position, p.ratings.ratings) for p in departures.survivors if p.is_registered and p.ratings is not None
        ]
        self.assertEqual(list(draft_team.squad), expected)

    def test_same_world_gives_same_signings(self) -> None:
        clubs = [club(1, foreign=1)]
        self.assertEqual(self.plan(clubs).signings, self.plan(clubs).signings)


class RouteLabelTest(unittest.TestCase):
    """オフの結果の「経路」の出典（`newcomer_route_label`）。"""

    def test_signed_foreigners_show_the_background(self) -> None:
        signed = Profile(is_foreign_player=True, foreign_background=ForeignBackground.MINOR_LEAGUE)
        self.assertEqual(foreign_market.newcomer_route_label(signed), "新外国人（マイナー）")
        self.assertEqual(foreign_market.newcomer_route_label(Profile(is_foreign_player=True)), "新外国人")

    def test_drafted_players_show_the_draft_route_even_if_foreign(self) -> None:
        """日本の学校からドラフトされた外国人選手（実データから分岐しうる）は新外国人ではない。"""
        drafted = Profile(is_foreign_player=True, university="B大", draft=DraftPick(YEAR))
        self.assertEqual(foreign_market.newcomer_route_label(drafted), "大学")
        self.assertEqual(
            foreign_market.newcomer_route_label(Profile(high_school="A高", draft=DraftPick(YEAR))), "高校"
        )


class ReplaceTest(unittest.TestCase):
    def test_a_profile_keeps_the_background_through_replace(self) -> None:
        profile = replace(Profile(), foreign_background=ForeignBackground.ASIAN_LEAGUE)
        self.assertFalse(profile.is_empty)
        self.assertIs(ForeignBackground.from_label("アジアのリーグ"), ForeignBackground.ASIAN_LEAGUE)
        self.assertIsNone(ForeignBackground.from_label(""))


if __name__ == "__main__":
    unittest.main()
