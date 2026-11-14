"""AI の期中の昇格（#170 P3b）: 判定日の規則（`decide_midseason_promotions`）と、日程の区切り（`split_at_reviews`）。

期待値の根拠: #170 のユーザーの決定 Q2 A（2026-11-12）。判定日は6月1日・7月1日、今の能力の総合値がその球団の
1軍の同じ位置の基準（自動編成で1軍に入る下位）以上の育成選手を上げる、1判定日1球団1人・1年2人まで、
支配下の上限70のうち #169 F3 の分を1人残す、受け持つ球団は対象にしない。補強の窓は7月31日まで（#207・野球協約）。
「1日ずつ進めてもまとめて進めても同じ」は、ペナントの進行の不変条件（Wiki「ペナントモードの仕組み」）。
"""

import unittest
from dataclasses import replace
from datetime import date, timedelta

from myapp.domain.pennant.development import (
    MIDSEASON_MAX_PROMOTIONS,
    MIDSEASON_RESERVED_SLOTS,
    MidseasonClub,
    MidseasonProspect,
    decide_midseason_promotions,
    midseason_promotion_number,
)
from myapp.domain.pennant.reinforcement import (
    MidseasonRegistration,
    PlaySegment,
    RegistrationKind,
    split_at_reviews,
)
from myapp.domain.simulation.manager import ClubRoster, SimBatter, SimPitcher
from myapp.domain.simulation.ratings import BatterRatings, PitcherRatings
from myapp.domain.value_objects import ForeignPlayerRules, Position

YEAR = 2027
JUNE_1 = date(YEAR, 6, 1)
SEED = 11
LIMIT = 70


def pitching(value: int) -> PitcherRatings:
    """抑える力（総合値）がちょうど `value` の投手の能力。"""
    return PitcherRatings(stuff=value, control=value, home_run_avoidance=value, stamina=50)


def batting(value: int, *, fielding: int = 50) -> BatterRatings:
    """打撃（総合値）がちょうど `value` の野手の能力。"""
    return BatterRatings(contact=value, power=value, eye=value, speed=value, fielding=fielding)


def pool() -> ClubRoster:
    """支配下の登録候補。投手15人（自動の1軍は14人なので、最も低い40の投手は1軍に入らない。1軍の投手の基準は45）、
    野手15人（全員が1軍。内野手の基準は50、外野手の基準は60、捕手の基準は50）。"""
    pitchers = [SimPitcher(100 + n, f"投手{n}", pitching(60)) for n in range(13)]
    pitchers.append(SimPitcher(113, "投手13", pitching(45)))
    pitchers.append(SimPitcher(114, "投手14", pitching(40)))
    batters = [SimBatter(200 + n, f"捕手{n}", Position.CATCHER, batting(50), throws=None) for n in range(2)]
    batters += [SimBatter(210 + n, f"内野{n}", Position.INFIELDER, batting(50), throws=None) for n in range(7)]
    batters += [SimBatter(220 + n, f"外野{n}", Position.OUTFIELDER, batting(60), throws=None) for n in range(6)]
    return ClubRoster(team_id=1, name="球団", batters=tuple(batters), pitchers=tuple(pitchers))


def club(
    *prospects: MidseasonProspect,
    team_id: int = 1,
    registrations: tuple[date, ...] = (),
    signings: tuple[date, ...] = (),
    **changes,
) -> MidseasonClub:
    """`registrations` は期中の昇格の登録日、`signings` は期中の外国人の獲得（#169 F3）の登録日。"""
    recorded = tuple(
        MidseasonRegistration(team_id, 800 + n, registered_on, kind)
        for n, (registered_on, kind) in enumerate(
            [(on, RegistrationKind.PROMOTION) for on in registrations]
            + [(on, RegistrationKind.SIGNING) for on in signings]
        )
    )
    base = MidseasonClub(
        team_id=team_id,
        is_managed=False,
        pool=pool(),
        foreign_rules=ForeignPlayerRules.NPB,
        development=prospects,
        registered_count=60,
        registered_limit=LIMIT,
        used_numbers=frozenset({"1", "2", "011"}),
        registrations=recorded,
    )
    return replace(base, **changes)


def prospect(player_id: int, position: Position, value: int | None) -> MidseasonProspect:
    if value is None:
        return MidseasonProspect(player_id, position, None)
    ratings = pitching(value) if position is Position.PITCHER else batting(value)
    return MidseasonProspect(player_id, position, ratings)


def promoted(clubs, *, on=JUNE_1) -> list[tuple[int, int]]:
    return [(p.team_id, p.player_id) for p in decide_midseason_promotions(clubs, on=on, world_seed=SEED)]


class MidseasonPromotionRuleTest(unittest.TestCase):
    def test_a_prospect_at_the_bar_of_the_active_roster_is_promoted(self):
        """基準は自動の1軍に入る下位（14人目の投手=45）。15人目（40）は1軍に入らないので基準にならない。「以上」で上げる。"""
        self.assertEqual(promoted([club(prospect(900, Position.PITCHER, 45))]), [(1, 900)])
        self.assertEqual(promoted([club(prospect(900, Position.PITCHER, 44))]), [])

    def test_the_bar_is_the_same_position(self):
        """外野手（基準60）は内野手の基準（50）を超えても、外野手の基準に届かなければ上げない。"""
        self.assertEqual(promoted([club(prospect(900, Position.OUTFIELDER, 55))]), [])
        self.assertEqual(promoted([club(prospect(900, Position.INFIELDER, 55))]), [(1, 900)])

    def test_a_catcher_is_measured_with_fielding_too(self):
        """捕手の総合値は守備力と打撃の大きい方（`retirement_value`。戦力外・引退と同じ物差し）。"""
        catcher = MidseasonProspect(900, Position.CATCHER, batting(30, fielding=55))
        self.assertEqual(promoted([club(catcher)]), [(1, 900)])

    def test_a_position_without_anyone_on_the_active_roster_has_no_bar(self):
        """1軍にその位置の選手がいない（指名打者）なら、比べる基準が無いので上げない。能力の無い育成も上げない。"""
        self.assertEqual(promoted([club(prospect(900, Position.DESIGNATED_HITTER, 90))]), [])
        self.assertEqual(promoted([club(prospect(900, Position.PITCHER, None))]), [])

    def test_one_per_club_per_review_date_with_the_largest_margin(self):
        """1判定日1球団1人。基準を上回る幅の大きい選手を上げる（同じ幅なら id の小さい選手）。"""
        clubs = [
            club(
                prospect(901, Position.PITCHER, 50),  # 投手の基準45を5上回る
                prospect(902, Position.INFIELDER, 58),  # 内野手の基準50を8上回る
                prospect(903, Position.OUTFIELDER, 68),  # 外野手の基準60を8上回る（同じ幅で id が大きい）
            )
        ]
        self.assertEqual(promoted(clubs), [(1, 902)])

    def test_the_managed_club_is_left_to_the_gm(self):
        """受け持つ球団は AI が触れない（GM が育成タブで決める）。"""
        clubs = [
            club(prospect(900, Position.PITCHER, 70), is_managed=True),
            club(prospect(901, Position.PITCHER, 70), team_id=2),
        ]
        self.assertEqual(promoted(clubs), [(2, 901)])

    def test_at_most_two_a_year(self):
        """1年2人まで。数えるのはその年の期中の登録（前の年の登録は数えない）。"""
        candidate = prospect(900, Position.PITCHER, 70)
        last_year = (date(YEAR - 1, 6, 2), date(YEAR - 1, 7, 2))
        self.assertEqual(MIDSEASON_MAX_PROMOTIONS, 2)
        self.assertEqual(promoted([club(candidate, registrations=last_year)]), [(1, 900)])
        self.assertEqual(
            promoted([club(candidate, registrations=(date(YEAR, 6, 2),))], on=date(YEAR, 7, 1)), [(1, 900)]
        )
        two = (date(YEAR, 4, 10), date(YEAR, 6, 2))
        self.assertEqual(promoted([club(candidate, registrations=two)], on=date(YEAR, 7, 1)), [])

    def test_a_review_date_already_done_is_not_repeated(self):
        """その判定日の登録（登録日が登録日以降）がもうあれば、やり直しても2人目を上げない。"""
        candidate = prospect(900, Position.PITCHER, 70)
        self.assertEqual(promoted([club(candidate, registrations=(JUNE_1,))]), [])

    def test_foreign_signings_are_not_counted_as_promotions(self):
        """期中の外国人の獲得（#169 F3）の登録は、1年2人にも、その判定日のやり直しにも数えない（同じ判定日に昇格の後で
        獲得しても、昇格は止まらない）。"""
        candidate = prospect(900, Position.PITCHER, 70)
        self.assertEqual(promoted([club(candidate, signings=(JUNE_1,))]), [(1, 900)])
        one_each = {"registrations": (date(YEAR, 6, 2),), "signings": (date(YEAR, 6, 2),)}
        self.assertEqual(promoted([club(candidate, **one_each)], on=date(YEAR, 7, 1)), [(1, 900)])

    def test_one_slot_is_left_under_the_limit_for_the_foreign_signing(self):
        """支配下の上限70のうち1人は #169 F3 の期中の獲得のために残す（69人までは上げる）。上限が無ければ
        制限しない。"""
        candidate = prospect(900, Position.PITCHER, 70)
        self.assertEqual(MIDSEASON_RESERVED_SLOTS, 1)
        self.assertEqual(promoted([club(candidate, registered_count=LIMIT - 2)]), [(1, 900)])
        self.assertEqual(promoted([club(candidate, registered_count=LIMIT - 1)]), [])
        self.assertEqual(promoted([club(candidate, registered_count=LIMIT + 5, registered_limit=None)]), [(1, 900)])

    def test_nobody_is_promoted_outside_the_window(self):
        """補強の期限（7月31日）の後の試合日からは上げない。"""
        candidate = prospect(900, Position.PITCHER, 70)
        self.assertEqual(promoted([club(candidate)], on=date(YEAR, 7, 31)), [(1, 900)])
        self.assertEqual(promoted([club(candidate)], on=date(YEAR, 8, 1)), [])

    def test_the_number_is_drawn_like_the_gm_promotion(self):
        """背番号は GM の期中の昇格と同じ引き方（`midseason_promotion_number`）。使われている番号と重ならない。"""
        used = frozenset({"1", "2", "011"})
        (promotion,) = decide_midseason_promotions(
            [club(prospect(900, Position.PITCHER, 70), used_numbers=used)], on=JUNE_1, world_seed=SEED
        )
        expected = midseason_promotion_number(set(used), world_seed=SEED, year=YEAR, team_id=1, player_id=900)
        self.assertEqual(promotion.number, expected)
        self.assertNotIn(promotion.number, used)


def day(month: int, dom: int) -> date:
    return date(YEAR, month, dom)


class SplitAtReviewsTest(unittest.TestCase):
    def test_a_range_across_june_1_is_split_before_the_first_game_on_or_after_it(self):
        dates = [day(5, 30), day(5, 31), day(6, 2), day(6, 3)]
        self.assertEqual(
            split_at_reviews(dates, last_played=day(5, 29)),
            (
                PlaySegment((), (day(5, 30), day(5, 31))),
                PlaySegment((day(6, 1),), (day(6, 2), day(6, 3))),
            ),
        )

    def test_a_range_starting_on_the_review_date_reviews_first(self):
        self.assertEqual(
            split_at_reviews([day(6, 1)], last_played=day(5, 31)), (PlaySegment((day(6, 1),), (day(6, 1),)),)
        )

    def test_a_review_already_passed_is_not_repeated(self):
        self.assertEqual(split_at_reviews([day(6, 2)], last_played=day(6, 1)), (PlaySegment((), (day(6, 2),)),))

    def test_both_review_dates_inside_one_gap_come_in_order(self):
        self.assertEqual(
            split_at_reviews([day(7, 2)], last_played=day(5, 30)),
            (PlaySegment((day(6, 1), day(7, 1)), (day(7, 2),)),),
        )

    def test_the_first_advance_of_a_world_has_no_last_game(self):
        self.assertEqual(split_at_reviews([day(3, 27)], last_played=None), (PlaySegment((), (day(3, 27),)),))

    def test_day_by_day_gives_the_same_reviews_as_all_at_once(self):
        """1日ずつ区切っても、まとめて区切っても、判定日はどれも同じ試合日の前に来る。"""
        dates = [day(5, 1) + timedelta(days=n) for n in range(90) if n % 7 != 0]

        def reviews_before(segments):
            return [(review, segment.dates[0]) for segment in segments for review in segment.reviews]

        at_once = reviews_before(split_at_reviews(dates, last_played=day(4, 30)))
        one_by_one = []
        previous = day(4, 30)
        for each in dates:
            one_by_one += reviews_before(split_at_reviews([each], last_played=previous))
            previous = each
        self.assertEqual(at_once, one_by_one)
        self.assertEqual([review for review, _ in at_once], [day(6, 1), day(7, 1)])
