"""ドラフトの新人（`domain/pennant/draft.py` の人数・位置・外国人で補う人数・背番号と、1球団だけの会議の結果）。
DB 不要。

会議の指名順・入札と抽選・AI は `test_draft_meeting.py` / `test_draft_order.py`、外国人の獲得は
`test_foreign_market.py` が見る。
"""

from __future__ import annotations

import random
import unittest
from collections import Counter
from dataclasses import replace
from datetime import date

from myapp.domain.pennant import draft
from myapp.domain.pennant.draft import DraftRoute, DraftTeam, draftee_count
from myapp.domain.pennant.draft_meeting import DraftContext, run_draft
from myapp.domain.pennant.draft_order import DraftOrderRule, DraftStandings
from myapp.domain.pennant.prospects import expected_prospect_ratings
from myapp.domain.pennant.roster import OPENING_REGISTERED_BAND, RegisteredBand
from myapp.domain.simulation.ratings import BatterRatings, PitcherRatings
from myapp.domain.value_objects import AcquisitionRoute, ForeignPlayerRules, Position, Profile
from myapp.domain.virtual_players import generator
from myapp.domain.virtual_players.generator import AmateurPath

YEAR = 2027


def team(
    counts: dict[Position, int] | None = None,
    *,
    foreign: int = 0,
    limit: int | None = 4,
    numbers: frozenset[str] = frozenset(),
    surnames: dict[str, int] | None = None,
    registered_limit: int | None = 70,
) -> DraftTeam:
    # 帯の中（開幕前の帯の上端 − 通常の人数 − 1 = 59人）。新人は通常の人数（6人）
    default = generator.largest_remainder(
        OPENING_REGISTERED_BAND.high - draft.USUAL_DRAFTEES - 1, generator.POSITION_RATIOS
    )
    return DraftTeam(
        team_id=1,
        position_counts=default if counts is None else counts,
        foreign_count=foreign,
        foreign_limit=limit,
        used_numbers=numbers,
        surname_counts=surnames or {},
        registered_limit=registered_limit,
    )


def _item_mean(ratings: BatterRatings | PitcherRatings) -> float:
    """項目（スタミナを含む全項目）の平均。"""
    return sum(getattr(ratings, name) for name in ratings.LABELS) / len(ratings.LABELS)


def run(target: DraftTeam, seed: int = 1, used: set[str] | None = None) -> tuple[draft.Draftee, ...]:
    """1球団だけの会議（世界のシード `seed`）で迎える新人。"""
    context = DraftContext(DraftOrderRule.NPB, DraftStandings(leagues=((target.team_id,),)))
    names = set() if used is None else used
    return run_draft([target], context, year=YEAR, world_seed=seed, used_names=names).draftees


class CountTest(unittest.TestCase):
    """指名数は、引退・退団の後の支配下の人数から、開幕前の支配下が帯（62〜66人）に入るように決める（#167）。"""

    def test_the_band_and_the_yearly_numbers(self) -> None:
        """帯の出典は roster.OPENING_REGISTERED_BAND だけ。

        値が変わったら Wiki「ペナントモードの仕組み」の「実測値」（#167）を取り直す。
        """
        self.assertEqual(OPENING_REGISTERED_BAND, RegisteredBand(low=62, high=66))
        self.assertEqual((draft.USUAL_DRAFTEES, draft.MAX_DRAFTEES), (6, 10))

    def test_a_club_inside_the_band_gets_the_usual_number_up_to_the_upper_end(self) -> None:
        self.assertEqual(draftee_count(56, registered_limit=None), draft.USUAL_DRAFTEES)  # 62人（下端ちょうど）
        self.assertEqual(draftee_count(60, registered_limit=None), draft.USUAL_DRAFTEES)  # 66人（上端ちょうど）
        self.assertEqual(draftee_count(61, registered_limit=None), 5)
        self.assertEqual(draftee_count(65, registered_limit=None), 1)

    def test_a_club_below_the_band_catches_up_to_the_lower_end_within_the_yearly_maximum(self) -> None:
        self.assertEqual(draftee_count(54, registered_limit=None), 8)  # 62人（下端）
        self.assertEqual(draftee_count(52, registered_limit=None), draft.MAX_DRAFTEES)
        self.assertEqual(draftee_count(34, registered_limit=None), draft.MAX_DRAFTEES)
        self.assertEqual(draftee_count(0, registered_limit=None), draft.MAX_DRAFTEES)

    def test_a_club_at_or_over_the_upper_end_gets_no_draftee(self) -> None:
        self.assertEqual(draftee_count(66, registered_limit=None), 0)
        self.assertEqual(draftee_count(67, registered_limit=None), 0)
        self.assertEqual(draftee_count(80, registered_limit=None), 0, "上限で失敗させない（負の人数にもしない）")

    def test_never_exceeds_the_league_limit_of_registered_players(self) -> None:
        self.assertEqual(draftee_count(40, registered_limit=42), 2)
        self.assertEqual(draftee_count(58, registered_limit=62), 4)
        self.assertEqual(draftee_count(62, registered_limit=62), 0)
        self.assertEqual(draftee_count(75, registered_limit=70), 0, "実データから写した上限超えの球団")
        self.assertEqual(draftee_count(56, registered_limit=70), draft.USUAL_DRAFTEES, "上限が帯より上なら帯で決まる")
        self.assertEqual(draftee_count(56, registered_limit=None), draft.USUAL_DRAFTEES)

    def test_the_draft_fills_what_other_acquisitions_leave(self) -> None:
        """ほかの獲得手段（未実装）で埋まる見込みを除いた残りを、ドラフトが埋める。"""
        # 56人として数える
        self.assertEqual(draftee_count(52, registered_limit=None, expected_other=4), draft.USUAL_DRAFTEES)
        self.assertEqual(draftee_count(60, registered_limit=None, expected_other=4), 2)  # 上端66人まで残り2人
        self.assertEqual(draftee_count(60, registered_limit=None, expected_other=8), 0)

    def test_draft_matches_the_count(self) -> None:
        self.assertEqual(len(run(team())), draft.USUAL_DRAFTEES)
        self.assertEqual(len(run(team(registered_limit=61))), 61 - 59)


class RegisteredBandTest(unittest.TestCase):
    def test_gap_to_the_band(self) -> None:
        band = RegisteredBand(low=62, high=66)
        self.assertEqual((band.gap(58, limit=None).shortfall, band.gap(58, limit=None).vacancy), (4, 8))
        self.assertEqual((band.gap(64, limit=None).shortfall, band.gap(64, limit=None).vacancy), (0, 2))
        self.assertEqual((band.gap(70, limit=None).shortfall, band.gap(70, limit=None).vacancy), (0, 0))
        # 上限が帯より低いときは上限が天井。不足は空きを超えない
        self.assertEqual((band.gap(58, limit=60).shortfall, band.gap(58, limit=60).vacancy), (2, 2))
        self.assertEqual(band.gap(58, limit=None, expected=3).vacancy, 5)


class PositionTest(unittest.TestCase):
    def test_minimums_are_filled_first(self) -> None:
        counts = {Position.PITCHER: 12, Position.CATCHER: 1, Position.INFIELDER: 6, Position.OUTFIELDER: 5}
        chosen = Counter(rookie.position for rookie in run(team(counts)))
        self.assertGreaterEqual(
            counts[Position.PITCHER] + chosen[Position.PITCHER], draft.MIN_BY_POSITION[Position.PITCHER]
        )
        self.assertGreaterEqual(counts[Position.CATCHER] + chosen[Position.CATCHER], 2)

    def test_surplus_positions_are_not_chosen(self) -> None:
        counts = {Position.PITCHER: 20, Position.CATCHER: 4, Position.INFIELDER: 3, Position.OUTFIELDER: 3}
        chosen = Counter(rookie.position for rookie in run(team(counts)))
        self.assertEqual(chosen[Position.PITCHER], 0)

    def test_the_target_composition_follows_the_size_after_the_draft(self) -> None:
        """目標の構成は指名後の支配下の人数（ここでは64人）で割り振る。足りない投手6人だけを補う。"""
        counts = dict(generator.largest_remainder(64, generator.POSITION_RATIOS))
        counts[Position.PITCHER] -= draft.USUAL_DRAFTEES
        chosen = Counter(rookie.position for rookie in run(team(counts)))
        self.assertEqual(chosen, Counter({Position.PITCHER: draft.USUAL_DRAFTEES}))

    def test_pitchers_get_pitcher_ratings(self) -> None:
        for rookie in run(team({Position.INFIELDER: 20})):
            expected = PitcherRatings if rookie.position.is_pitcher else BatterRatings
            self.assertIsInstance(rookie.ratings, expected)


class ForeignCountTest(unittest.TestCase):
    """外国人の獲得で補う人数（`foreign_count_to_add`）。獲得は会議の前（`foreign_market.py`。#169）。"""

    def foreign_count(self, target: DraftTeam) -> int:
        count, _ = draft.newcomer_plan(target)
        return draft.foreign_count_to_add(target, count)

    def test_adds_foreign_players_up_to_the_target(self) -> None:
        """戦力外で外した外国人を同じオフに埋めるので、年2人の上限は無い（#168。2026-10-12 ユーザー決定）。"""
        self.assertEqual(self.foreign_count(team(foreign=0, limit=None)), draft.FOREIGN_TARGET)
        self.assertEqual(self.foreign_count(team(foreign=1, limit=None)), 3)
        self.assertEqual(self.foreign_count(team(foreign=3, limit=None)), 1)
        self.assertEqual(self.foreign_count(team(foreign=4, limit=None)), 0)

    def test_does_not_add_to_a_full_roster_quota(self) -> None:
        self.assertEqual(self.foreign_count(team(foreign=2, limit=2)), 0)
        # 分岐元が枠を超えていても、さらには足さない
        self.assertEqual(self.foreign_count(team(foreign=6, limit=3)), 0)

    def test_small_quota_caps_the_target(self) -> None:
        self.assertEqual(self.foreign_count(team(foreign=0, limit=1)), 1)

    def test_the_target_is_what_the_agreement_lets_a_club_register(self) -> None:
        """協約は保有を縛らないので、目標は1軍に置ける外国人の計（第82条の2 の4人）。#204 の前の値と同じ。"""
        self.assertEqual(draft.foreign_target(ForeignPlayerRules.NPB.active), 4)

    def test_the_meeting_has_no_foreign_rookies(self) -> None:
        """会議の新人は日本人だけ（外国人は会議の前に獲得する）。"""
        for seed in range(10):
            for rookie in run(team(foreign=0, limit=None), seed):
                self.assertFalse(rookie.profile.is_foreign_player)
                self.assertEqual(rookie.profile.nationality, "")


class SignedForeignersTest(unittest.TestCase):
    """獲得した外国人は新しい支配下の選手の枠の内側（#169 Q2）: 日本人の新人は「新人の人数 − 獲得した外国人」。"""

    def test_japanese_positions_are_the_plan_without_the_signed_positions(self) -> None:
        target = team()
        count, planned = draft.newcomer_plan(target)
        self.assertEqual(count, draft.USUAL_DRAFTEES)
        signed = (planned[-1], planned[0])
        plan = draft.plan_team_draft(target, signed)
        self.assertEqual(plan.positions, planned)
        self.assertEqual(plan.foreign_positions, signed)
        self.assertEqual(plan.japanese_count, count - len(signed))
        self.assertEqual(Counter(plan.japanese_positions) + Counter(signed), Counter(planned))

    def test_no_signing_keeps_the_plan(self) -> None:
        target = team()
        _, planned = draft.newcomer_plan(target)
        plan = draft.plan_team_draft(target)
        self.assertEqual((plan.japanese_positions, plan.foreign_positions), (planned, ()))

    def test_a_signed_position_outside_the_plan_takes_a_later_non_catcher_place(self) -> None:
        counts = dict(generator.largest_remainder(64, generator.POSITION_RATIOS))
        counts[Position.PITCHER] -= draft.USUAL_DRAFTEES
        target = team(counts)
        _, planned = draft.newcomer_plan(target)
        self.assertEqual(set(planned), {Position.PITCHER}, "前提: 計画は投手だけ")
        plan = draft.plan_team_draft(target, (Position.OUTFIELDER,))
        self.assertEqual(plan.japanese_positions, planned[:-1])

    def test_the_japanese_minimum_drops_by_the_signed_foreigners(self) -> None:
        target = team(dict(generator.largest_remainder(52, generator.POSITION_RATIOS)))
        count, planned = draft.newcomer_plan(target)
        unsigned = draft.plan_team_draft(target)
        signed = draft.plan_team_draft(target, (Position.PITCHER, Position.PITCHER))
        self.assertEqual(signed.japanese_minimum, max(0, unsigned.japanese_minimum - 2))
        self.assertEqual(signed.japanese_count, count - 2)

    def test_signing_more_than_the_newcomers_fails(self) -> None:
        target = team(registered_limit=60)
        count, _ = draft.newcomer_plan(target)
        with self.assertRaises(ValueError):
            draft.plan_team_draft(target, (Position.PITCHER,) * (count + 1))

    def test_the_meeting_drafts_the_rest(self) -> None:
        target = team()
        context = DraftContext(DraftOrderRule.NPB, DraftStandings(leagues=((target.team_id,),)))
        rookies = run_draft(
            [target], context, year=YEAR, world_seed=1, used_names=set(), signed={1: (Position.PITCHER,)}
        ).draftees
        self.assertEqual(len(rookies), draft.USUAL_DRAFTEES - 1)


class NumberTest(unittest.TestCase):
    def test_numbers_avoid_current_roster_and_each_other(self) -> None:
        taken = frozenset(str(number) for number in range(1, 90))
        for rookie in run(team(numbers=taken)):
            self.assertNotIn(rookie.number, taken)
        numbers = [rookie.number for rookie in run(team(), seed=5)]
        self.assertEqual(len(numbers), len(set(numbers)))

    def test_falls_back_to_zero_and_double_zero_when_one_to_ninety_nine_are_full(self) -> None:
        """新人は支配下なので、育成の3桁の背番号には落とさない（尽きたら 0・00）。"""
        # 支配下59人・リーグの上限63人で、新人は4人
        rookies = run(team(numbers=frozenset(str(number) for number in range(1, 98)), registered_limit=63))
        self.assertEqual(len(rookies), 4, "前提: 新人は4人")
        self.assertEqual({rookie.number for rookie in rookies}, {"98", "99", "0", "00"})


class ProfileTest(unittest.TestCase):
    def test_age_follows_the_route(self) -> None:
        as_of = date(YEAR + 1, 4, 1)
        seen: set[DraftRoute] = set()
        for seed in range(60):
            for rookie in run(team(), seed):
                age = rookie.profile.age(as_of)
                seen.add(rookie.route)
                if rookie.route is DraftRoute.HIGH_SCHOOL:
                    self.assertEqual(age, 18)
                elif rookie.route is DraftRoute.UNIVERSITY:
                    self.assertEqual(age, 22)
                else:
                    self.assertIs(rookie.route, DraftRoute.CORPORATE)
                    self.assertTrue(age is not None and 23 <= age <= 26)
        self.assertEqual(seen, set(DraftRoute))

    def test_domestic_routes_follow_the_weights(self) -> None:
        """12球団の会議で数える（1球団だけの会議は候補が24人と少なく、高校の割合が 0.22 と許容の端にあった。
        1位の好み（#302）で1人動いて外れたため、合わせ込みの前提に近い多球団の会議にした）。"""
        teams = [replace(team(), team_id=team_id) for team_id in range(1, 13)]
        context = DraftContext(DraftOrderRule.NPB, DraftStandings(leagues=(tuple(range(1, 13)),)))
        counts = Counter(
            rookie.route
            for seed in range(1, 26)
            for rookie in run_draft(teams, context, year=YEAR, world_seed=seed, used_names=set()).draftees
        )
        domestic = counts[DraftRoute.HIGH_SCHOOL] + counts[DraftRoute.UNIVERSITY] + counts[DraftRoute.CORPORATE]
        self.assertAlmostEqual(counts[DraftRoute.UNIVERSITY] / domestic, 0.5, delta=0.08)
        self.assertAlmostEqual(counts[DraftRoute.HIGH_SCHOOL] / domestic, 0.3, delta=0.08)

    def test_rookies_get_stronger_along_the_route(self) -> None:
        by_route: dict[DraftRoute, list[float]] = {route: [] for route in DraftRoute}
        for seed in range(80):
            for rookie in run(team(), seed):
                ratings = rookie.ratings
                value = ratings.pitching_value if isinstance(ratings, PitcherRatings) else ratings.batting_value
                by_route[rookie.route].append(value)
        means = {route: sum(values) / len(values) for route, values in by_route.items() if values}
        self.assertLess(means[DraftRoute.HIGH_SCHOOL], means[DraftRoute.UNIVERSITY])
        self.assertLess(means[DraftRoute.UNIVERSITY], means[DraftRoute.CORPORATE])

    def test_corporate_rookies_are_stronger_the_older_they_enter_and_keep_the_route_mean(self) -> None:
        """社会人だけ入団の年齢で今の能力を傾ける（#308 Q2）: 23歳 約41.3 → 26歳 約44.8、範囲の中央で経路の平均 43。

        ほかの経路は年齢に依らず経路の平均。`draw_rookie` が引く能力の平均がこの傾きに従う。
        """
        corporate = DraftRoute.CORPORATE
        low, high = generator.CORPORATE_DEBUT_AGE_RANGE
        means = [corporate.rating_mean_at(age) for age in range(low, high + 1)]
        self.assertEqual(means, sorted(means))
        self.assertAlmostEqual(means[-1] - means[0], draft.CORPORATE_AGE_SPREAD)
        self.assertAlmostEqual(sum(means) / len(means), corporate.rating_mean)
        for route in (DraftRoute.HIGH_SCHOOL, DraftRoute.UNIVERSITY):
            self.assertEqual(route.rating_mean_at(30), route.rating_mean)
        values: dict[int, list[float]] = {age: [] for age in range(low, high + 1)}
        rng = random.Random(5)
        as_of = date(YEAR + 1, 4, 1)
        for _ in range(2000):
            rookie = draft.draw_rookie(
                rng, Position.OUTFIELDER, year=YEAR, used_names=set(), path=AmateurPath.CORPORATE
            )
            age = rookie.profile.age(as_of)
            assert age is not None
            values[age].append(_item_mean(rookie.ratings))
        for age, items in values.items():
            expected = _item_mean(
                expected_prospect_ratings(mean=corporate.rating_mean_at(age), position=Position.OUTFIELDER)
            )
            with self.subTest(age=age):
                self.assertAlmostEqual(sum(items) / len(items), expected, delta=0.8)

    def test_route_labels_are_the_screen_names(self) -> None:
        self.assertEqual([route.label for route in DraftRoute], ["高校", "大学", "社会人"])


class DraftPickTest(unittest.TestCase):
    """新人のドラフトの指名。年は締める年、順位は会議の巡（1球団だけの会議なら1巡に1人ずつ）。"""

    def test_japanese_rookies_are_drafted_in_the_closing_year_and_enter_the_next(self) -> None:
        for seed in range(20):
            for rookie in run(team(), seed):
                pick = rookie.profile.draft
                assert pick is not None
                self.assertEqual((pick.year, pick.entry_year, pick.is_development), (YEAR, YEAR + 1, False))

    def test_rounds_count_from_one_without_gaps_or_duplicates(self) -> None:
        for seed in range(20):
            rookies = run(team(foreign=0, limit=None), seed)
            rounds = [rookie.profile.draft.round for rookie in rookies if rookie.profile.draft is not None]
            self.assertEqual(rounds, list(range(1, len(rounds) + 1)))

    def test_acquisition_route_follows_the_draft(self) -> None:
        for rookie in run(team(foreign=0, limit=None)):
            pick = rookie.profile.draft
            assert pick is not None, "会議の新人はドラフトの指名を持つ"
            self.assertIs(rookie.acquisition_route, pick.acquisition_route)
            self.assertIs(rookie.acquisition_route, AcquisitionRoute.DRAFT)


class RouteMappingTest(unittest.TestCase):
    def test_every_amateur_path_has_a_route_with_the_same_label(self) -> None:
        """アマチュアの経路を足して、新人の経路を足し忘れると KeyError になる。ここで見張る。"""
        for path in AmateurPath:
            route = DraftRoute.from_amateur_path(path)
            self.assertEqual(route.label, path.value)


class NameTest(unittest.TestCase):
    def test_names_avoid_taken_ones_and_are_recorded(self) -> None:
        used: set[str] = set()
        first = run(team(), used=used)
        self.assertTrue({rookie.name for rookie in first} <= used)
        second = run(team(), used=used)
        self.assertFalse({rookie.name for rookie in first} & {rookie.name for rookie in second})

    def test_back_name_gets_an_initial_only_when_surname_is_shared(self) -> None:
        rookies = run(team(), seed=3)
        surnames = Counter(rookie.profile.back_name.split(".")[-1] for rookie in rookies)
        for rookie in rookies:
            shared = surnames[rookie.profile.back_name.split(".")[-1]] > 1
            self.assertEqual("." in rookie.profile.back_name, shared)
        # 現在の選手と重なる苗字にも頭文字が付く
        taken = rookies[0].profile.back_name.split(".")[-1]
        again = run(team(surnames={taken: 1}), seed=3, used=set())
        self.assertIn(".", again[0].profile.back_name)


class RouteFromProfileTest(unittest.TestCase):
    """経路は保存していないので、プロフィールから導く（画面がオフの結果を読むときに使う）。"""

    def test_each_route_is_derived_from_the_career(self) -> None:
        self.assertIs(DraftRoute.of_profile(Profile(high_school="A高")), DraftRoute.HIGH_SCHOOL)
        self.assertIs(DraftRoute.of_profile(Profile(high_school="A高", university="B大")), DraftRoute.UNIVERSITY)
        self.assertIs(DraftRoute.of_profile(Profile(high_school="A高", corporate_team="C社")), DraftRoute.CORPORATE)

    def test_the_route_of_every_drafted_rookie_matches_the_drawn_route(self) -> None:
        for seed in range(30):
            for rookie in run(team(), seed=seed, used=set()):
                self.assertIs(DraftRoute.of_profile(rookie.profile), rookie.route)


class DeterminismTest(unittest.TestCase):
    def test_same_random_gives_same_class(self) -> None:
        self.assertEqual(run(team(), seed=9), run(team(), seed=9))
        self.assertNotEqual(run(team(), seed=9), run(team(), seed=10))


if __name__ == "__main__":
    unittest.main()
