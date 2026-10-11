"""優勝の確定とマジック（`pennant_race`）の単体テスト。Django なしで通る。"""

import unittest

from myapp.domain.pennant.race import PennantRace, RaceTeam, pennant_race, season_champion


def _team(team_id: int, wins: int, losses: int, remaining: int) -> RaceTeam:
    return RaceTeam(team_id, wins, losses, remaining)


class PennantRaceTest(unittest.TestCase):
    def test_clinched_when_the_second_team_cannot_catch_up_even_by_winning_out(self) -> None:
        # 143試合制。首位 90勝50敗3残り。2位 82勝58敗3残り: 2位の最大 85勝、首位の最小 90勝
        teams = [_team(1, 90, 50, 3), _team(2, 82, 58, 3)]

        self.assertEqual(PennantRace(1, clinched=True, magic=None), pennant_race(1, teams))

    def test_magic_is_games_of_the_leader_plus_losses_of_the_rival_needed(self) -> None:
        # 首位 80勝50敗13残り、2位 75勝55敗13残り（どちらも143試合）。
        # 2位の最大 88勝。首位が 89 勝以上なら確実 → あと 9（勝ちと相手の負けの合計）
        teams = [_team(1, 80, 50, 13), _team(2, 75, 55, 13)]

        race = pennant_race(1, teams)

        assert race is not None
        self.assertFalse(race.clinched)
        self.assertEqual(9, race.magic)

    def test_the_magic_is_the_largest_against_all_rivals(self) -> None:
        teams = [_team(1, 80, 50, 13), _team(2, 75, 55, 13), _team(3, 78, 52, 13)]

        race = pennant_race(1, teams)

        assert race is not None
        self.assertEqual(12, race.magic, "3位（78勝・最大91勝）が最大の相手。2位（最大88勝）なら 9")

    def test_not_lit_while_the_magic_is_more_than_the_own_remaining_games(self) -> None:
        teams = [_team(1, 40, 30, 73), _team(2, 40, 30, 73)]

        race = pennant_race(1, teams)

        assert race is not None
        self.assertFalse(race.clinched)
        self.assertIsNone(race.magic)

    def test_the_magic_is_lit_when_it_equals_the_remaining_games(self) -> None:
        # 2位の最大 85勝。首位 80勝で 6 残り。必要 6（85 → 86 勝）= 残り全勝で確定
        teams = [_team(1, 80, 57, 6), _team(2, 79, 58, 6)]

        race = pennant_race(1, teams)

        assert race is not None
        self.assertEqual(6, race.magic)

    def test_a_tie_in_the_best_case_is_not_clinched(self) -> None:
        """同率の余地があれば確定にしない（同率は共同の首位で、単独の優勝ではない）。"""
        teams = [_team(1, 80, 60, 3), _team(2, 77, 63, 3)]  # 2位の最大 80勝63敗... 首位の最小 80勝63敗

        race = pennant_race(1, teams)

        assert race is not None
        self.assertFalse(race.clinched)
        self.assertEqual(1, race.magic)

    def test_no_games_left_decides_the_champion(self) -> None:
        teams = [_team(1, 85, 58, 0), _team(2, 80, 63, 0)]

        self.assertTrue(pennant_race(1, teams).clinched)  # type: ignore[union-attr]
        loser = pennant_race(2, teams)
        assert loser is not None
        self.assertFalse(loser.clinched)
        self.assertIsNone(loser.magic)

    def test_a_dead_heat_at_the_end_has_no_sole_champion(self) -> None:
        teams = [_team(1, 80, 63, 0), _team(2, 80, 63, 0)]

        for team_id in (1, 2):
            race = pennant_race(team_id, teams)
            assert race is not None
            self.assertFalse(race.clinched)
            self.assertIsNone(race.magic)

    def test_the_percentage_ignores_ties(self) -> None:
        """勝率は 勝 ÷ (勝 + 敗)。引分が多い球団の勝率は、試合数ではなく勝敗で比べる。"""
        # 1: 70勝50敗（引分 23 は数えない）= .583。2: 69勝50敗 → 最大 でも残り 0 なので .580
        teams = [_team(1, 70, 50, 0), _team(2, 69, 50, 0)]

        self.assertTrue(pennant_race(1, teams).clinched)  # type: ignore[union-attr]

    def test_a_team_behind_a_rival_who_cannot_be_caught_has_no_magic(self) -> None:
        teams = [_team(1, 60, 70, 5), _team(2, 80, 50, 5)]

        race = pennant_race(1, teams)

        assert race is not None
        self.assertEqual(PennantRace(1, clinched=False, magic=None), race)

    def test_a_direct_game_counts_for_both_sides(self) -> None:
        """直接対決の1勝は、自軍の勝ちと相手の負けの両方に数えられる（マジックが2進む）。"""
        before = pennant_race(1, [_team(1, 80, 50, 13), _team(2, 75, 55, 13)])
        after = pennant_race(1, [_team(1, 81, 50, 12), _team(2, 75, 56, 12)])

        assert before is not None and after is not None
        self.assertEqual((before.magic or 0) - 2, after.magic)

    def test_missing_team_or_no_rivals(self) -> None:
        self.assertIsNone(pennant_race(9, [_team(1, 1, 1, 1)]))
        self.assertIsNone(pennant_race(1, [_team(1, 1, 1, 1)]))


class SeasonChampionTest(unittest.TestCase):
    """シーズン終了時の同率首位は、NPB の規定どおり1球団に決める。"""

    ORDER = [1, 2, 3, 4]

    def test_a_single_leader_is_the_champion(self) -> None:
        self.assertEqual(3, season_champion([3], [], {}, self.ORDER))

    def test_the_head_to_head_record_decides(self) -> None:
        results = [(1, 2), (1, 2), (2, 1)]  # 1 の 2勝1敗

        self.assertEqual(
            1, season_champion([1, 2], results, {1: 2, 2: 1}, self.ORDER), "前年順位は2が上でも直接対決が先"
        )

    def test_only_games_between_the_tied_teams_count(self) -> None:
        results = [(2, 1), (1, 3), (1, 3), (1, 3)]  # 3 との対戦（3 は首位ではない）は数えない

        self.assertEqual(2, season_champion([1, 2], results, {}, self.ORDER))

    def test_a_dead_heat_in_the_head_to_head_falls_to_last_years_rank(self) -> None:
        results = [(1, 2), (2, 1)]

        self.assertEqual(2, season_champion([1, 2], results, {1: 3, 2: 1}, self.ORDER))

    def test_ties_in_the_head_to_head_are_not_counted(self) -> None:
        """引分は結果に含めない。対戦が無い・引分だけなら同率として次の規定へ。"""
        self.assertEqual(2, season_champion([1, 2], [], {1: 2, 2: 1}, self.ORDER))

    def test_the_opening_year_uses_the_team_order(self) -> None:
        self.assertEqual(2, season_champion([4, 2, 3], [], {}, self.ORDER))

    def test_the_same_last_year_rank_falls_to_the_team_order(self) -> None:
        self.assertEqual(1, season_champion([2, 1], [], {1: 2, 2: 2}, self.ORDER))

    def test_three_teams_tied(self) -> None:
        # 1・2・3 の対戦。1 は 3勝2敗（.600）、2 は 3勝3敗（.500）、3 は 2勝3敗（.400）
        results = [(1, 2), (1, 3), (1, 2), (3, 1), (2, 3), (2, 3), (2, 1), (3, 2)]

        self.assertEqual(1, season_champion([1, 2, 3], results, {1: 3, 2: 2, 3: 1}, self.ORDER))

    def test_a_three_way_tie_narrows_to_the_top_then_the_next_rule(self) -> None:
        # 1 と 2 が当該球団間の勝率で並び、3 は下。1 と 2 は前年順位で決まる
        results = [(1, 3), (2, 3), (1, 3), (2, 3), (1, 2), (2, 1)]

        self.assertEqual(2, season_champion([1, 2, 3], results, {1: 2, 2: 1, 3: 3}, self.ORDER))

    def test_no_leaders_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            season_champion([], [], {}, self.ORDER)
