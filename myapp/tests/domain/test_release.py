"""AI の戦力外（`domain/pennant/release.py`）と、人数の出典（`RegisteredBand.excess`）。DB 不要。"""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date
from statistics import fmean

from myapp.domain.pennant import draft
from myapp.domain.pennant.draft_meeting import DraftContext
from myapp.domain.pennant.draft_order import DraftOrderRule, DraftStandings
from myapp.domain.pennant.foreign_market import SIGNED_RATING_MEAN, expected_signing_ages, expected_signing_ratings
from myapp.domain.pennant.offseason import OffseasonClub, OffseasonPlayer, plan_offseason, retirement_value
from myapp.domain.pennant.outlook import PEAK_AGE
from myapp.domain.pennant.ratings import PlayerRatings
from myapp.domain.pennant.release import (
    FIRST_YEAR_FOREIGN_MARGIN,
    YOUTH_CREDIT,
    ReleaseCounts,
    decide_releases,
    foreign_prospect_value,
    keep_value,
    release_counts,
    youth_credit,
)
from myapp.domain.pennant.retirement import PlayingTime
from myapp.domain.pennant.roster import OPENING_REGISTERED_BAND, RegisteredBand
from myapp.domain.simulation.randomness import make_random
from myapp.domain.simulation.ratings import BatterRatings, PitcherRatings
from myapp.domain.simulation.samples import draw_ratings
from myapp.domain.value_objects import ContractStatus, DraftPick, ForeignPlayerRules, Position, Profile

YEAR = 2027
START_YEAR = 2026
BENCH = PlayingTime(plate_appearances=50)
REGULAR = PlayingTime(plate_appearances=500)


def member(
    player_id: int,
    *,
    position: Position = Position.INFIELDER,
    value: int = 50,
    age: int = 30,
    foreign: bool = False,
    playing_time: PlayingTime = BENCH,
    contract: ContractStatus = ContractStatus.REGISTERED,
    entered_year: int = 2015,
) -> OffseasonPlayer:
    """能力の総合値が `value` の選手（全項目を同じ値にする。捕手は守備力も同じなので総合値どおり）。

    `entered_year` はプロ入りの年で、在籍の開始年も同じ。日本人はその前年のドラフト、外国人はドラフトなし。
    """
    ratings = PitcherRatings(value, value, value, 50) if position.is_pitcher else BatterRatings(*(value,) * 5)
    return OffseasonPlayer(
        player_id=player_id,
        position=position,
        profile=Profile(
            birth_date=date(YEAR - age, 1, 1),
            is_foreign_player=foreign,
            draft=None if foreign else DraftPick(entered_year - 1),
            back_name=f"N{player_id}",
        ),
        number=str(player_id),
        joined_year=entered_year,
        ratings=PlayerRatings(player_id, YEAR, ratings),
        contract=contract,
        playing_time=playing_time,
    )


def roster(size: int, *, start_id: int = 1, foreign: int = 0) -> list[OffseasonPlayer]:
    """位置の最低人数を十分に超える支配下 `size` 人（投手が半分、残りを内野・外野・捕手に）。全員が控えで同じ能力。"""
    positions = [Position.PITCHER, Position.INFIELDER, Position.OUTFIELDER, Position.PITCHER, Position.CATCHER]
    return [
        member(start_id + index, position=positions[index % len(positions)], foreign=index < foreign)
        for index in range(size)
    ]


def club(players: list[OffseasonPlayer], *, registered_limit: int | None = 70, foreign_limit: int | None = 4):
    return OffseasonClub(
        team_id=1,
        foreign_rules=ForeignPlayerRules(active=foreign_limit, pitchers=None, batters=None),
        registered_player_limit=registered_limit,
        players=tuple(players),
    )


def releases(players: list[OffseasonPlayer], **limits: int | None) -> tuple[int, ...]:
    return decide_releases(club(players, **limits), players, year=YEAR, start_year=START_YEAR)


class ExcessTest(unittest.TestCase):
    """帯の天井を超える人数（`RegisteredBand.excess`）。`gap` と同じ天井から導く。"""

    band = OPENING_REGISTERED_BAND

    def test_over_the_upper_end(self) -> None:
        self.assertEqual(self.band.excess(70, limit=None), 4)
        self.assertEqual(self.band.excess(66, limit=None), 0)
        self.assertEqual(self.band.excess(60, limit=None), 0, "帯の中・下は0")

    def test_the_league_limit_lower_than_the_upper_end_is_the_ceiling(self) -> None:
        self.assertEqual(self.band.excess(62, limit=60), 2)
        self.assertEqual(self.band.excess(62, limit=80), 0, "上限が上端より高ければ上端が天井")

    def test_expected_newcomers_need_room(self) -> None:
        self.assertEqual(self.band.excess(62, limit=None, expected=6), 2)
        self.assertEqual(self.band.excess(58, limit=None, expected=6), 0)

    def test_consistent_with_gap(self) -> None:
        """超えていれば空きは0、空きがあれば超過は0。空きと超過の差は天井との差そのもの。"""
        band = RegisteredBand(low=62, high=66)
        for registered in range(50, 80):
            for limit in (None, 60, 70):
                for expected in (0, 6):
                    with self.subTest(registered=registered, limit=limit, expected=expected):
                        excess = band.excess(registered, limit=limit, expected=expected)
                        vacancy = band.gap(registered, limit=limit, expected=expected).vacancy
                        self.assertEqual(min(excess, vacancy), 0)
                        self.assertEqual(vacancy - excess, band.ceiling(limit) - registered - expected)


class ReleaseCountsTest(unittest.TestCase):
    """支配下の空きの人数の式（ユーザー決定: 新人の余地まで）。"""

    def counts(self, registered: int, registered_limit: int | None = 70) -> ReleaseCounts:
        return release_counts(registered=registered, registered_limit=registered_limit)

    def test_room_for_the_usual_draftees(self) -> None:
        """引退の後に63人なら、ふだんの新人6人を迎えて上端66人に収まるよう3人外す。"""
        self.assertEqual(self.counts(63).registered, 63 + draft.USUAL_DRAFTEES - OPENING_REGISTERED_BAND.high)
        self.assertEqual(self.counts(60).registered, 0, "既に空きがあれば外さない")
        self.assertEqual(self.counts(72).registered, 12, "上端を超えた分と新人の空き")

    def test_the_league_limit_lowers_the_ceiling(self) -> None:
        self.assertEqual(self.counts(60, registered_limit=62).registered, 4)


class ForeignProspectTest(unittest.TestCase):
    """新しく迎える外国人に見込める価値（ユーザー決定 2026-10-12: 外国人は価値で入れ替える）。"""

    def test_the_prospect_is_the_mean_of_the_signed_foreigners(self) -> None:
        """出典は獲得した外国人の分布（平均 `foreign_market.SIGNED_RATING_MEAN`。#169）と候補の年齢の分布。
        投手は3項目の平均そのもの。若さの上乗せは前所属ごとの年齢の範囲から（マイナーの25・26歳だけが上乗せを持つ）。"""
        youth = sum(share * youth_credit(age) for age, share in expected_signing_ages().items())
        self.assertAlmostEqual(youth, 0.375)
        self.assertAlmostEqual(foreign_prospect_value(Position.PITCHER), SIGNED_RATING_MEAN + youth)

    def test_catchers_are_compared_with_infielders(self) -> None:
        """外国人は捕手を獲得しないので、捕手は内野手の見込みで比べる。"""
        self.assertEqual(foreign_prospect_value(Position.CATCHER), foreign_prospect_value(Position.INFIELDER))

    def test_the_mean_ratings_follow_the_draw(self) -> None:
        """見込み（ぶれを除いた平均）は、新人を引く分布の平均に近い（項目のずれを写していない）。"""
        rng = make_random(1)
        for position in (Position.PITCHER, Position.OUTFIELDER, Position.DESIGNATED_HITTER):
            drawn = [
                retirement_value(position, draw_ratings(rng, mean=50.0, sd=6.0, position=position))
                for _ in range(2000)
            ]
            with self.subTest(position=position):
                expected = retirement_value(position, expected_signing_ratings(position))
                self.assertAlmostEqual(fmean(drawn), expected, delta=0.5)


class KeepValueTest(unittest.TestCase):
    def test_young_players_get_credit(self) -> None:
        young = member(1, value=45, age=PEAK_AGE - 4)
        prime = member(2, value=45, age=PEAK_AGE)
        older = member(3, value=45, age=PEAK_AGE + 5)
        values = [keep_value(p, year=YEAR, start_year=START_YEAR) for p in (young, prime, older)]
        self.assertAlmostEqual(values[0] - values[1], 4 * YOUTH_CREDIT)
        self.assertEqual(values[1], values[2], "全盛を過ぎた年齢は総合値だけ")

    def test_catcher_uses_the_better_of_defense_and_batting(self) -> None:
        catcher = replace(
            member(1, position=Position.CATCHER, age=PEAK_AGE),
            ratings=PlayerRatings(1, YEAR, BatterRatings(30, 30, 30, 30, 70)),
        )
        self.assertEqual(keep_value(catcher, year=YEAR, start_year=START_YEAR), 70.0)


class DecideReleasesTest(unittest.TestCase):
    def test_a_club_with_room_releases_nobody(self) -> None:
        self.assertEqual(releases(roster(60)), ())

    def test_releases_exactly_the_room_for_the_usual_draftees(self) -> None:
        players = roster(70)
        self.assertEqual(len(releases(players)), 70 + draft.USUAL_DRAFTEES - OPENING_REGISTERED_BAND.high)

    def test_lowest_keep_value_goes_first(self) -> None:
        players = roster(62)
        for index, value in ((20, 35), (10, 30)):
            players[index] = member(players[index].player_id, position=players[index].position, value=value)
        self.assertEqual(releases(players), (players[10].player_id, players[20].player_id))

    def test_ties_break_by_player_id_and_input_order_does_not_matter(self) -> None:
        players = roster(64)
        forward = releases(players)
        self.assertEqual(forward, tuple(p.player_id for p in players[:4]))
        self.assertEqual(releases(list(reversed(players))), forward)

    def test_regulars_rookies_and_developmental_players_are_kept(self) -> None:
        players = roster(62)
        weak = [
            member(101, value=20, playing_time=REGULAR),
            member(102, value=20, entered_year=YEAR),
            member(103, value=20, contract=ContractStatus.DEVELOPMENTAL),
        ]
        chosen = releases([*players, *weak])
        self.assertEqual(len(chosen), 4, "支配下は 62 + 2 = 64人（育成は数えない）。6人の空きまで4人")
        self.assertFalse({101, 102, 103} & set(chosen))

    def test_a_forked_player_without_a_draft_is_not_a_first_year_player(self) -> None:
        """分岐した選手は在籍の開始年が一律に開幕年。ドラフトが無くても、最初のオフに1年目として守らない（再発防止）。"""
        players = roster(62)
        forked = replace(
            member(101, value=20), joined_year=START_YEAR, profile=replace(member(101).profile, draft=None)
        )
        self.assertIn(
            101, decide_releases(club([*players, forked]), [*players, forked], year=START_YEAR, start_year=START_YEAR)
        )

    def test_a_foreign_rookie_without_a_draft_is_a_first_year_player(self) -> None:
        """外国人の新人はドラフトを持たない。開幕年より後に在籍を始めた（世界の中で迎えた）ので1年目として守る（#108）。"""
        rookie = member(101, value=20, foreign=True, entered_year=YEAR)
        self.assertIsNone(rookie.profile.draft)
        self.assertTrue(rookie.is_first_year(YEAR, start_year=START_YEAR))
        forked = replace(rookie, joined_year=START_YEAR)
        self.assertFalse(forked.is_first_year(START_YEAR, start_year=START_YEAR))
        self.assertFalse(rookie.is_first_year(YEAR + 1, start_year=START_YEAR))

    def test_foreigners_below_the_prospect_are_replaced_even_if_regular(self) -> None:
        """外国人は、新しく迎える外国人の見込みを下回れば主力でも外し（1年目は余裕を引いても下回るとき）、上回れば残す。"""
        players = roster(55)
        weak_regular = member(101, position=Position.PITCHER, value=40, foreign=True, playing_time=REGULAR)
        weak_rookie = member(102, position=Position.OUTFIELDER, value=40, foreign=True, entered_year=YEAR)
        strong_bench = member(103, position=Position.PITCHER, value=60, foreign=True)
        self.assertEqual(releases([*players, weak_regular, weak_rookie, strong_bench]), (101, 102))

    def test_first_year_foreigners_get_a_margin(self) -> None:
        """1年目の外国人は、見込みから余裕（`FIRST_YEAR_FOREIGN_MARGIN`）を引いた値を大きく下回るときだけ外す。"""
        prospect = foreign_prospect_value(Position.PITCHER)
        slightly_below = round(prospect - FIRST_YEAR_FOREIGN_MARGIN / 2)
        far_below = round(prospect - FIRST_YEAR_FOREIGN_MARGIN - 2)
        rookie = member(101, position=Position.PITCHER, value=slightly_below, foreign=True, entered_year=YEAR)
        veteran = member(102, position=Position.PITCHER, value=slightly_below, foreign=True)
        weak_rookie = member(103, position=Position.PITCHER, value=far_below, foreign=True, entered_year=YEAR)
        self.assertEqual(releases([*roster(55), rookie, veteran, weak_rookie]), (103, 102))

    def test_all_foreigners_may_be_replaced(self) -> None:
        players = roster(55)
        foreigners = [member(101 + i, position=Position.PITCHER, value=40 + i, foreign=True) for i in range(4)]
        self.assertEqual(releases([*players, *foreigners]), (101, 102, 103, 104))

    def test_the_comparison_uses_the_keep_value_with_the_youth_credit(self) -> None:
        """若い外国人は若さの上乗せで見込みを上回ることがある（`keep_value` の規則はそのまま）。"""
        prospect = foreign_prospect_value(Position.PITCHER)
        young = member(101, position=Position.PITCHER, value=48, age=PEAK_AGE - 4, foreign=True)
        old = member(102, position=Position.PITCHER, value=48, age=PEAK_AGE + 3, foreign=True)
        self.assertGreater(keep_value(young, year=YEAR, start_year=START_YEAR), prospect)
        self.assertEqual(releases([*roster(55), young, old]), (102,))

    def test_foreign_turnover_is_not_filled_with_japanese_players(self) -> None:
        """外国人を外さなくても、支配下に空きがあれば日本人は外さない。"""
        strong = [member(101 + i, position=Position.PITCHER, value=60, foreign=True) for i in range(4)]
        self.assertEqual(releases([*roster(55), *strong]), ())

    def test_developmental_foreigners_are_not_released(self) -> None:
        weak = member(101, position=Position.PITCHER, value=30, foreign=True, contract=ContractStatus.DEVELOPMENTAL)
        self.assertEqual(releases([*roster(55), weak]), ())

    def test_positions_are_not_cut_below_the_minimum(self) -> None:
        """捕手が最低人数ちょうどなら、いちばん弱くても外さない。"""
        minimum = draft.MIN_BY_POSITION[Position.CATCHER]
        catchers = [member(i, position=Position.CATCHER, value=20) for i in range(1, minimum + 1)]
        others = [p for p in roster(80, start_id=100) if p.position is not Position.CATCHER]
        chosen = releases([*catchers, *others])
        self.assertTrue(chosen)
        self.assertFalse({c.player_id for c in catchers} & set(chosen))

    def test_releases_only_as_many_as_there_are_candidates(self) -> None:
        """候補が足りなければ外せる分だけ（例外にしない）。"""
        players = [replace(p, playing_time=REGULAR) for p in roster(70)]
        players[0] = replace(players[0], playing_time=BENCH)
        self.assertEqual(releases(players), (players[0].player_id,))


class PlanWithReleasesTest(unittest.TestCase):
    """締める計画の中の戦力外（引退 → 戦力外 → ドラフト → 成長 → 錨）。"""

    @staticmethod
    def plan(players: list[OffseasonPlayer], *, registered_limit: int | None = 70):
        clubs = [club(players, registered_limit=registered_limit)]
        return plan_offseason(
            clubs,
            year=YEAR,
            world_seed=1,
            start_year=START_YEAR,
            used_names=(),
            draft=DraftContext(DraftOrderRule.NPB, DraftStandings(leagues=(tuple(c.team_id for c in clubs),))),
        )

    def test_released_players_leave_without_next_year_ratings(self) -> None:
        result = self.plan(roster(72))
        self.assertTrue(result.released)
        retained = {change.player_id for change in result.retained}
        self.assertFalse(set(result.released) & retained)
        self.assertFalse(set(result.released) & set(result.retired), "引退した選手は戦力外にしない")
        self.assertFalse(set(result.released) & set(result.without_ratings))

    def test_the_draft_counts_the_roster_after_releases(self) -> None:
        players = roster(72)
        result = self.plan(players)
        left = set(result.retired) | set(result.released)
        remaining = sum(1 for p in players if p.player_id not in left)
        self.assertEqual(
            len(result.signings) + len([d for d in result.draftees if not d.is_development]),
            draft.draftee_count(remaining, registered_limit=70),
        )
        self.assertEqual(
            remaining + len(result.signings) + len([d for d in result.draftees if not d.is_development]),
            OPENING_REGISTERED_BAND.high,
        )

    def test_a_club_with_room_is_planned_as_before(self) -> None:
        """空きがある球団（引退の後 + 6人 ≤ 上端）で外国人が見込みを上回るなら、戦力外0人でドラフトは今までどおり。"""
        players = [
            *roster(48),
            *(member(201 + i, position=Position.PITCHER, value=60, foreign=True) for i in range(2)),
        ]
        result = self.plan(players)
        self.assertEqual(result.released, ())
        remaining = sum(1 for p in players if p.player_id not in result.retired)
        self.assertEqual(
            len(result.signings) + len([d for d in result.draftees if not d.is_development]),
            draft.draftee_count(remaining, registered_limit=70),
        )


if __name__ == "__main__":
    unittest.main()
