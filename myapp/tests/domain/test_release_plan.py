"""GM の戦力外の指名（`ReleasePlan`・`ensure_releasable`・締める計画での使い方）。DB 不要。

計画は Issue #168 の段階B: 手動なら指名どおり（引退した・支配下でなくなった選手は黙って除く。帯は見ない）、
自動なら段階A の規則。AI 球団は常に自動。
"""

from __future__ import annotations

import unittest
from dataclasses import replace

from myapp.domain.exceptions import InvalidReleasePlan
from myapp.domain.pennant import draft
from myapp.domain.pennant.club_plan import ClubMember
from myapp.domain.pennant.offseason import OffseasonClub, OffseasonPlayer, plan_offseason
from myapp.domain.pennant.release import ReleasePlan, decide_releases, ensure_releasable
from myapp.domain.value_objects import ContractStatus, ForeignPlayerRules, Position
from myapp.tests.domain.test_release import REGULAR, START_YEAR, YEAR, member, roster


def members(players: list[OffseasonPlayer]) -> list[ClubMember]:
    return [
        ClubMember(p.player_id, f"選手{p.player_id}", p.position, p.profile.is_foreign_player)
        for p in players
        if p.is_registered
    ]


def squad() -> list[OffseasonPlayer]:
    """位置ごとに最低人数より2人多い支配下（投手17・捕手5・内野手8・外野手7）。"""
    positions = [position for position, minimum in draft.MIN_BY_POSITION.items() for _ in range(minimum + 2)]
    return [member(player_id, position=position) for player_id, position in enumerate(positions, start=1)]


class ReleasePlanTest(unittest.TestCase):
    def test_none_is_automatic_and_empty_is_manual_zero(self) -> None:
        self.assertFalse(ReleasePlan(team_id=1, year=YEAR).is_manual)
        self.assertTrue(ReleasePlan(team_id=1, year=YEAR, player_ids=frozenset()).is_manual)


class EnsureReleasableTest(unittest.TestCase):
    def check(
        self,
        ids: list[int],
        players: list[OffseasonPlayer],
        *,
        foreign_rules: ForeignPlayerRules = ForeignPlayerRules.NPB,
    ) -> None:
        ensure_releasable(ids, members(players), foreign_rules=foreign_rules)

    def test_any_registered_player_can_be_chosen(self) -> None:
        """主力も入団1年目も GM は外せる（AI が外さないのは自動の規則）。0人も成り立つ。"""
        players = squad()
        players[0] = replace(players[0], playing_time=REGULAR)
        players[1] = member(players[1].player_id, position=players[1].position, debut_year=YEAR)
        self.check([players[0].player_id, players[1].player_id], players)
        self.check([], players)

    def test_duplicates_are_rejected(self) -> None:
        players = squad()
        with self.assertRaisesRegex(InvalidReleasePlan, "重複"):
            self.check([1, 1], players)

    def test_only_registered_players_on_the_club(self) -> None:
        players = squad()
        developmental = member(500, contract=ContractStatus.DEVELOPMENTAL)
        for unknown in (999, 500):
            with self.subTest(player_id=unknown), self.assertRaisesRegex(InvalidReleasePlan, "支配下の選手だけ"):
                self.check([unknown], [*players, developmental])

    def test_a_position_cannot_drop_below_the_minimum(self) -> None:
        players = squad()
        catchers = [p.player_id for p in players if p.position is Position.CATCHER]
        self.check(catchers[:2], players)
        with self.assertRaisesRegex(InvalidReleasePlan, "捕手は支配下に3人以上"):
            self.check(catchers[:3], players)

    def test_a_position_already_below_the_minimum_only_blocks_releasing_it(self) -> None:
        """分岐した球団のように最初から最低人数を下回る位置があっても、ほかの位置は外せる。"""
        players = [p for p in squad() if p.position is not Position.CATCHER or p.player_id % 2] + [
            member(900, position=Position.CATCHER)
        ]
        pitcher = next(p.player_id for p in players if p.position is Position.PITCHER)
        self.check([pitcher], players)

    def test_the_remaining_roster_must_field_a_lineup_within_the_foreign_limit(self) -> None:
        """外国人を除いた野手でスタメンを組めなくなる指名は弾く（試合が止まる）。"""
        players = [
            replace(p, profile=replace(p.profile, is_foreign_player=True))
            if not p.position.is_pitcher and p.player_id % 3 == 0
            else p
            for p in squad()
        ]
        japanese_batters = [
            p.player_id for p in players if not p.position.is_pitcher and not p.profile.is_foreign_player
        ]
        with self.assertRaises(InvalidReleasePlan):
            self.check(
                japanese_batters[:4], players, foreign_rules=ForeignPlayerRules(active=1, pitchers=None, batters=None)
            )


class ManualReleasesInThePlanTest(unittest.TestCase):
    """締める計画で、受け持つ球団（id=1）の手動の指名を使う。ほかの球団（id=2）は自動のまま。"""

    @staticmethod
    def plan(
        own: list[OffseasonPlayer], manual: dict[int, frozenset[int]], *, other: list[OffseasonPlayer] | None = None
    ):
        clubs = [
            OffseasonClub(
                team_id=1, foreign_rules=ForeignPlayerRules.NPB, registered_player_limit=70, players=tuple(own)
            )
        ]
        if other is not None:
            clubs.append(
                OffseasonClub(
                    team_id=2, foreign_rules=ForeignPlayerRules.NPB, registered_player_limit=70, players=tuple(other)
                )
            )
        return plan_offseason(
            clubs, year=YEAR, world_seed=1, start_year=START_YEAR, used_names=(), manual_releases=manual
        )

    def test_only_the_chosen_players_are_released(self) -> None:
        players = roster(72)
        chosen = frozenset({players[5].player_id, players[9].player_id})
        result = self.plan(players, {1: chosen})
        self.assertEqual(set(result.released), chosen - set(result.retired))
        self.assertTrue(chosen - set(result.retired), "抽選で全員が引退していない")

    def test_manual_zero_releases_nobody_even_over_the_band(self) -> None:
        """手動で0人なら帯を超えていても外さない（帯は見ない）。ドラフトは天井を超えない人数のまま。"""
        result = self.plan(roster(72), {1: frozenset()})
        self.assertEqual(result.released, ())
        remaining = 72 - len(result.retired)
        self.assertEqual(len(result.draftees), draft.draftee_count(remaining, registered_limit=70))

    def test_a_chosen_player_who_retires_is_recorded_as_retired(self) -> None:
        # 39歳は引退の確率が高い（9割）ので、このシードでも誰かが引退する
        players = [*roster(60), *(member(201 + i, age=39) for i in range(4))]
        automatic = self.plan(players, {})
        self.assertTrue(automatic.retired, "このシードでは誰かが引退する")
        retiring = automatic.retired[0]
        kept = next(p.player_id for p in players if p.player_id not in automatic.retired)
        result = self.plan(players, {1: frozenset({retiring, kept})})
        self.assertIn(retiring, result.retired)
        self.assertNotIn(retiring, result.released)
        self.assertEqual(result.released, (kept,))

    def test_players_no_longer_registered_are_dropped_silently(self) -> None:
        players = [*roster(60), member(300, contract=ContractStatus.DEVELOPMENTAL)]
        result = self.plan(players, {1: frozenset({300, 999})})
        self.assertEqual(result.released, ())

    def test_other_clubs_stay_automatic(self) -> None:
        other = roster(72, start_id=1000)
        result = self.plan(roster(60), {1: frozenset()}, other=other)
        remaining = [p for p in other if p.player_id not in result.retired]
        club = OffseasonClub(
            team_id=2, foreign_rules=ForeignPlayerRules.NPB, registered_player_limit=70, players=tuple(other)
        )
        expected = decide_releases(club, remaining, year=YEAR, start_year=START_YEAR)
        self.assertEqual(set(result.released), set(expected))
        self.assertTrue(expected)

    def test_automatic_when_no_manual_plan(self) -> None:
        players = roster(72)
        self.assertEqual(self.plan(players, {}).released, self.plan(players, {2: frozenset()}).released)


if __name__ == "__main__":
    unittest.main()
