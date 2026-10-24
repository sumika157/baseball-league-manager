"""世界・世界の範囲・分岐の業務ルール（DB 不要・Django 非依存）。"""

import unittest
from datetime import date

from myapp.domain.entities import League, Team
from myapp.domain.exceptions import DuplicateJerseyNumber, InvalidSeason, InvalidWorkspace, InvalidWorld
from myapp.domain.pennant.draft_order import DraftOrderRule
from myapp.domain.pennant.fork import fork_league, fork_roster, fork_team
from myapp.domain.pennant.postseason import PostseasonFormat
from myapp.domain.pennant.world import (
    MAX_SEASONS_PER_WORLD,
    MAX_SEED,
    MAX_START_YEAR,
    World,
    WorldScope,
    draft_order_rule_change_blocker,
    earliest_start_year,
    league_priority,
)
from myapp.domain.value_objects import (
    ContractStatus,
    DraftPick,
    ForeignPlayerRules,
    JerseyNumber,
    Position,
    Profile,
    RosterLimits,
    Season,
)

UNLIMITED = RosterLimits.UNLIMITED


class WorldScopeTest(unittest.TestCase):
    def test_real_scope_is_the_real_data_of_a_workspace(self):
        scope = WorldScope.real(2)

        self.assertTrue(scope.is_real)
        self.assertFalse(scope.is_pennant)
        self.assertIsNone(scope.world_id)
        self.assertEqual(scope.workspace_id, 2)

    def test_pennant_scope_points_to_a_world_of_a_workspace(self):
        scope = WorldScope.pennant(2, 3)

        self.assertTrue(scope.is_pennant)
        self.assertFalse(scope.is_real)
        self.assertEqual(scope.world_id, 3)
        self.assertEqual(scope.workspace_id, 2)

    def test_scopes_compare_by_value(self):
        self.assertEqual(WorldScope.real(1), WorldScope.real(1))
        self.assertEqual(WorldScope.pennant(1, 3), WorldScope.pennant(1, 3))
        self.assertNotEqual(WorldScope.pennant(1, 3), WorldScope.pennant(1, 4))
        self.assertNotEqual(WorldScope.real(1), WorldScope.pennant(1, 3))

    def test_the_real_data_of_different_workspaces_are_different_scopes(self):
        """実データは「そのワークスペースの実データ」。ほかのワークスペースの実データと同じ範囲にならない。"""
        self.assertNotEqual(WorldScope.real(1), WorldScope.real(2))
        self.assertNotEqual(WorldScope.pennant(1, 3), WorldScope.pennant(2, 3))

    def test_a_scope_is_immutable(self):
        with self.assertRaises(AttributeError):
            WorldScope.real(1).world_id = 1  # type: ignore[misc]
        with self.assertRaises(AttributeError):
            WorldScope.real(1).workspace_id = 2  # type: ignore[misc]

    def test_a_scope_cannot_be_built_without_saying_which(self):
        """既定値が無い。「指定しなければ実データ」「指定しなければ全ワークスペース」と読める形を作らない。"""
        with self.assertRaises(TypeError):
            WorldScope()  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            WorldScope.real()  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            WorldScope.pennant(3)  # type: ignore[call-arg]

    def test_the_workspace_must_be_a_workspace_id(self):
        for bad in (0, -1, None, True, False, "3", 1.0):
            with self.subTest(workspace_id=bad):
                with self.assertRaises(InvalidWorkspace):
                    WorldScope.real(bad)  # type: ignore[arg-type]
                with self.assertRaises(InvalidWorkspace):
                    WorldScope.pennant(bad, 3)  # type: ignore[arg-type]
                with self.assertRaises(InvalidWorkspace):
                    WorldScope(workspace_id=bad, world_id=None)  # type: ignore[arg-type]

    def test_pennant_scope_rejects_a_value_that_is_not_a_world_id(self):
        for bad in (0, -1, None, True, "3"):
            with self.subTest(world_id=bad), self.assertRaises(InvalidWorld):
                WorldScope.pennant(1, bad)  # type: ignore[arg-type]

    def test_the_text_names_the_workspace(self):
        self.assertEqual(str(WorldScope.real(2)), "ワークスペース 2 の実データ")
        self.assertEqual(str(WorldScope.pennant(2, 3)), "ワークスペース 2 の世界 3")


class WorldTest(unittest.TestCase):
    def test_a_world_keeps_what_was_decided_at_creation(self):
        world = World(name=" 2026年ペナント ", seed=42, start_year=2026, owner_id=5)

        self.assertEqual(world.name, "2026年ペナント")
        self.assertEqual(world.seed, 42)
        self.assertEqual(world.start_year, 2026)
        self.assertEqual(world.owner_id, 5)
        self.assertIsNone(world.managed_team_id)

    def test_the_workspace_is_optional_and_kept_when_given(self):
        self.assertIsNone(World(name="世界", seed=0, start_year=2026).workspace_id)
        self.assertEqual(World(name="世界", seed=0, start_year=2026, workspace_id=3).workspace_id, 3)

    def test_the_managed_team_and_owner_may_be_decided_later(self):
        world = World(name="世界", seed=0, start_year=2026)

        self.assertIsNone(world.owner_id)
        self.assertIsNone(world.managed_team_id)

    def test_a_blank_name_is_rejected(self):
        for name in ("", "   "):
            with self.subTest(name=name), self.assertRaises(InvalidWorld):
                World(name=name, seed=1, start_year=2026)

    def test_a_too_long_name_is_rejected(self):
        with self.assertRaises(InvalidWorld):
            World(name="あ" * 101, seed=1, start_year=2026)

    def test_the_seed_must_fit_the_storage(self):
        World(name="世界", seed=0, start_year=2026)
        World(name="世界", seed=MAX_SEED, start_year=2026)
        for seed in (-1, MAX_SEED + 1, True, 1.5):
            with self.subTest(seed=seed), self.assertRaises(InvalidWorld):
                World(name="世界", seed=seed, start_year=2026)  # type: ignore[arg-type]

    def test_the_postseason_format_defaults_to_none_and_must_be_a_format(self):
        """方式の既定は「なし」（方式を足す前に作った世界と同じ）。選択肢の外の値は弾く。"""
        self.assertIs(World(name="世界", seed=1, start_year=2026).postseason_format, PostseasonFormat.NONE)
        world = World(name="世界", seed=1, start_year=2026, postseason_format=PostseasonFormat.NPB)
        self.assertIs(world.postseason_format, PostseasonFormat.NPB)
        for bad in ("なし", None, PostseasonFormat.NPB.value):
            with self.subTest(postseason_format=bad), self.assertRaises(InvalidWorld):
                World(name="世界", seed=1, start_year=2026, postseason_format=bad)  # type: ignore[arg-type]

    def test_the_draft_order_rule_defaults_to_npb_and_must_be_a_rule(self):
        """指名順の既定は NPB 方式（#198。規則を足す前に作った世界も NPB 方式）。選択肢の外の値は弾く。"""
        self.assertIs(World(name="世界", seed=1, start_year=2026).draft_order_rule, DraftOrderRule.NPB)
        world = World(name="世界", seed=1, start_year=2026, draft_order_rule=DraftOrderRule.WAIVER)
        self.assertIs(world.draft_order_rule, DraftOrderRule.WAIVER)
        for bad in ("NPB", None, DraftOrderRule.NPB.value):
            with self.subTest(draft_order_rule=bad), self.assertRaises(InvalidWorld):
                World(name="世界", seed=1, start_year=2026, draft_order_rule=bad)  # type: ignore[arg-type]

    def test_the_draft_order_rule_can_change_until_the_world_is_over(self):
        self.assertIsNone(draft_order_rule_change_blocker(world_over=False))
        self.assertIn("最終シーズン", draft_order_rule_change_blocker(world_over=True) or "")

    def test_the_start_year_must_be_a_season(self):
        with self.assertRaises(InvalidSeason):
            World(name="世界", seed=1, start_year=1800)

    def test_the_latest_start_year_leaves_room_for_every_season(self):
        """上限の年に作った世界の最後のシーズンも `Season` の範囲に収まる。"""
        self.assertEqual(MAX_START_YEAR, Season.MAX_YEAR - MAX_SEASONS_PER_WORLD + 1)

        world = World(name="世界", seed=1, start_year=MAX_START_YEAR)

        self.assertEqual(Season(world.start_year + MAX_SEASONS_PER_WORLD - 1).year, Season.MAX_YEAR)

    def test_a_world_itself_only_checks_the_season_range(self):
        """上限は作成の経路が検査する。保存済みの世界の復元（World の生成）を止めない。"""
        self.assertEqual(World(name="世界", seed=1, start_year=Season.MAX_YEAR).start_year, Season.MAX_YEAR)

    def test_the_earliest_start_year_follows_the_latest_birth(self):
        self.assertEqual(earliest_start_year([date(1990, 5, 1), date(2004, 12, 31), None]), 2005)

    def test_the_earliest_start_year_is_the_first_season_without_births(self):
        self.assertEqual(earliest_start_year([]), Season.MIN_YEAR)
        self.assertEqual(earliest_start_year([None]), Season.MIN_YEAR)

    def test_an_unsaved_world_has_no_scope(self):
        with self.assertRaises(InvalidWorld):
            _ = World(name="世界", seed=1, start_year=2026).scope

    def test_a_saved_world_has_its_scope_with_its_workspace(self):
        world = World(name="世界", seed=1, start_year=2026, id=9, workspace_id=4)

        self.assertEqual(world.scope, WorldScope.pennant(4, 9))

    def test_a_world_without_a_workspace_has_no_scope(self):
        with self.assertRaises(InvalidWorld):
            _ = World(name="世界", seed=1, start_year=2026, id=9).scope


def _source_team() -> Team:
    """分岐元の球団。現在の選手2人と、退団済みの選手1人（過去の在籍のみ）がいる。"""
    team = Team(name="東京", league_id=1, home_stadium_id=7, id=10, display_order=2)
    first = team.add_player("山田", JerseyNumber("1"), Position.INFIELDER, from_year=2018, limits=UNLIMITED)
    first.profile = Profile(
        birth_date=None, is_foreign_player=False, birthplace="東京都", draft=DraftPick(2017, round=3)
    )
    second = team.add_player("スミス", JerseyNumber("99"), Position.PITCHER, from_year=2020, limits=UNLIMITED)
    second.profile = Profile(is_foreign_player=True, nationality="米国")
    gone = team.add_player("引退", JerseyNumber("5"), Position.OUTFIELDER, from_year=2010, limits=UNLIMITED)
    team.retire_player(gone, year=2020)
    team.appoint_captain(first, year=2024)
    return team


class ForkTest(unittest.TestCase):
    def setUp(self):
        self.source = _source_team()

    def test_fork_league_copies_the_rules_but_not_the_id(self):
        league = League(
            name="セ",
            id=3,
            foreign_rules=ForeignPlayerRules(active=3, pitchers=2, batters=None),
            display_order=5,
            registered_player_limit=65,
        )

        copy = fork_league(league, display_order=1)

        self.assertIsNone(copy.id)
        self.assertEqual(
            (
                copy.name,
                copy.foreign_rules,
                copy.display_order,
                copy.registered_player_limit,
            ),
            ("セ", ForeignPlayerRules(active=3, pitchers=2, batters=None), 1, 65),
        )

    def test_fork_team_copies_the_stadium_and_order_but_starts_empty(self):
        copy = fork_team(self.source, league_id=99)

        self.assertIsNone(copy.id)
        self.assertEqual(
            (copy.name, copy.league_id, copy.home_stadium_id, copy.display_order),
            ("東京", 99, 7, 2),
        )
        self.assertEqual(copy.players, [])

    def test_fork_roster_copies_only_current_players(self):
        target = fork_team(self.source, league_id=99)
        target.id = 20

        fork_roster(self.source, target, start_year=2026)

        self.assertEqual(sorted(p.name for p in target.players), ["スミス", "山田"])

    def test_the_joining_year_is_the_start_year(self):
        target = fork_team(self.source, league_id=99)
        target.id = 20

        fork_roster(self.source, target, start_year=2026)

        for player in target.players:
            stint = target.current_stint(player)
            self.assertIsNotNone(stint)
            assert stint is not None  # mypy 用
            self.assertEqual((stint.from_year, stint.to_year, stint.team_id), (2026, None, 20))
            self.assertEqual(len(player.career), 1, "過去の在籍は写さない")

    def test_number_and_position_and_profile_are_copied(self):
        target = fork_team(self.source, league_id=99)
        target.id = 20

        fork_roster(self.source, target, start_year=2026)

        by_name = {p.name: p for p in target.players}
        self.assertEqual(by_name["山田"].number, JerseyNumber("1"))
        self.assertEqual(by_name["山田"].position, Position.INFIELDER)
        self.assertEqual(by_name["山田"].profile.birthplace, "東京都")
        self.assertEqual(by_name["山田"].profile.draft, DraftPick(2017, round=3))
        self.assertIsNone(by_name["スミス"].profile.draft)
        self.assertEqual(by_name["スミス"].number, JerseyNumber("99"))
        self.assertTrue(by_name["スミス"].profile.is_foreign_player)

    def test_captaincy_is_not_copied(self):
        target = fork_team(self.source, league_id=99)
        target.id = 20

        fork_roster(self.source, target, start_year=2026)

        self.assertTrue(all(p.captaincies == [] for p in target.players))
        self.assertIsNone(target.current_captain)

    def test_the_copy_is_independent_of_the_source(self):
        target = fork_team(self.source, league_id=99)
        target.id = 20
        fork_roster(self.source, target, start_year=2026)

        target.players[0].rename("別人")

        self.assertIn("山田", [p.name for p in self.source.players])
        self.assertNotIn("別人", [p.name for p in self.source.players])

    def test_the_aggregate_still_guards_the_jersey_number(self):
        """名簿は集約の操作で作るので、背番号の重複は集約が弾く（検査を書き直していない）。"""
        source = _source_team()
        # 分岐元の側が壊れていた場合（同じ背番号が2人）でも、写し先の集約が弾く
        source.players[1].career[0].number = JerseyNumber("1")
        target = fork_team(source, league_id=99)
        target.id = 20

        with self.assertRaises(DuplicateJerseyNumber):
            fork_roster(source, target, start_year=2026)

    def test_any_number_of_foreign_players_is_copied(self):
        """外国人の保有は縛らない（協約第82条の2）ので、何人いてもそのまま写す（スナップショット）。"""
        source = Team(name="多国籍", league_id=1, id=11)
        for number in range(1, 8):
            player = source.add_player(
                f"外国人{number}", JerseyNumber(str(number)), Position.PITCHER, from_year=2020, limits=UNLIMITED
            )
            player.profile = Profile(is_foreign_player=True)
        target = fork_team(source, league_id=99)
        target.id = 21

        fork_roster(source, target, start_year=2026)

        self.assertEqual(sum(p.profile.is_foreign_player for p in target.active_players), 7)

    def test_the_contract_status_is_copied(self):
        """育成選手は育成のまま写す（育成の3桁の背番号を、支配下としては写せない）。昇格した選手は支配下。"""
        source = Team(name="育成あり", league_id=1, id=12)
        source.add_player(
            "育成",
            JerseyNumber("120"),
            Position.PITCHER,
            from_year=2024,
            contract=ContractStatus.DEVELOPMENTAL,
            limits=UNLIMITED,
        )
        promoted = source.add_player(
            "昇格",
            JerseyNumber("121"),
            Position.INFIELDER,
            from_year=2023,
            contract=ContractStatus.DEVELOPMENTAL,
            limits=UNLIMITED,
        )
        promoted.id = 2
        source.promote_player(2, JerseyNumber("40"), 2025, limits=UNLIMITED)
        target = fork_team(source, league_id=99)
        target.id = 22

        fork_roster(source, target, start_year=2026)

        by_name = {p.name: p for p in target.players}
        self.assertIs(target.contract_of(by_name["育成"]), ContractStatus.DEVELOPMENTAL)
        self.assertEqual(by_name["育成"].number, JerseyNumber("120"))
        self.assertIs(target.contract_of(by_name["昇格"]), ContractStatus.REGISTERED)
        self.assertEqual(by_name["昇格"].number, JerseyNumber("40"))


if __name__ == "__main__":
    unittest.main()


class LeaguePriorityTest(unittest.TestCase):
    """リーグの優先順（日本シリーズの本拠地と種の同率。#198 のドラフトの優先リーグも使う）。"""

    def test_two_leagues_alternate_every_year_starting_with_the_smaller_id(self):
        self.assertEqual([3, 8], league_priority([8, 3], year=2026, start_year=2026))
        self.assertEqual([8, 3], league_priority([8, 3], year=2027, start_year=2026))
        self.assertEqual([3, 8], league_priority([8, 3], year=2028, start_year=2026))

    def test_three_or_more_leagues_rotate_one_by_one(self):
        ids = [5, 1, 3]
        self.assertEqual([1, 3, 5], league_priority(ids, year=2030, start_year=2030))
        self.assertEqual([3, 5, 1], league_priority(ids, year=2031, start_year=2030))
        self.assertEqual([5, 1, 3], league_priority(ids, year=2032, start_year=2030))
        self.assertEqual([1, 3, 5], league_priority(ids, year=2033, start_year=2030))

    def test_a_single_league_and_no_league(self):
        self.assertEqual([4], league_priority([4], year=2029, start_year=2026))
        self.assertEqual([], league_priority([], year=2029, start_year=2026))
