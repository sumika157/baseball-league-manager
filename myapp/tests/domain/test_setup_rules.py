"""リーグ・チーム・球場の登録の規則（W3。#79）。DB 不要・Django 非依存。

- 名前の検査（空でない・前後の空白を除く・長さの上限）は、作るときと `rename()` の両方で効く。
- 消してよいか（`domain/setup.py`）は、消せるなら None、消せなければ日本語の理由。0件の境界で切り替わる。
- 所属リーグの変更は、試合が1つも無いときだけ（2026-10-25 決定）。移す先の支配下選手の登録枠を超えるチームは
  移せない（W3 T3）。
- 表示順の並べ替え（`apply_order`）は識別子の並びで受け取り、行の集まりとちょうど一致しなければ何も変えない（W3 T8）。
"""

import unittest

from myapp.domain.entities import (
    LEAGUE_NAME_MAX_LENGTH,
    STADIUM_NAME_MAX_LENGTH,
    TEAM_NAME_MAX_LENGTH,
    League,
    Stadium,
    Team,
)
from myapp.domain.exceptions import (
    DomainError,
    InvalidName,
    InvalidOrder,
    LeagueChangeLocked,
    RegisteredPlayerLimitExceeded,
)
from myapp.domain.setup import (
    ORDER_MISMATCH,
    apply_order,
    ensure_league_change_allowed,
    league_change_blocker,
    league_deletion_blocker,
    stadium_deletion_blocker,
    team_deletion_blocker,
)
from myapp.domain.value_objects import ContractStatus, ForeignPlayerRules, JerseyNumber, Position, RosterLimits

# 文言に出る対象の名前 → (名前だけで作る関数, 名前の長さの上限)
KINDS = {
    "リーグ": (lambda name: League(name=name), LEAGUE_NAME_MAX_LENGTH),
    "チーム": (lambda name: Team(name=name), TEAM_NAME_MAX_LENGTH),
    "球場": (lambda name: Stadium(name=name), STADIUM_NAME_MAX_LENGTH),
}


class NameTest(unittest.TestCase):
    def test_the_limits_are_the_lengths_of_the_columns(self):
        """上限は今の列の長さと同じ（変えるとマイグレーションが出る）。ORM はこの定数を import する。"""
        self.assertEqual((LEAGUE_NAME_MAX_LENGTH, TEAM_NAME_MAX_LENGTH, STADIUM_NAME_MAX_LENGTH), (50, 100, 100))

    def test_a_name_is_stripped(self):
        for kind, (make, _) in KINDS.items():
            with self.subTest(kind=kind):
                self.assertEqual(make(f"  {kind}A  ").name, f"{kind}A")

    def test_an_empty_name_is_rejected_on_creation(self):
        for kind, (make, _) in KINDS.items():
            for name in ("", "   ", "　"):
                with self.subTest(kind=kind, name=repr(name)), self.assertRaisesRegex(InvalidName, f"{kind}名を入力"):
                    make(name)

    def test_the_length_limit_is_inclusive(self):
        for kind, (make, limit) in KINDS.items():
            with self.subTest(kind=kind):
                self.assertEqual(len(make("あ" * limit).name), limit)
                # 前後の空白は数えない
                self.assertEqual(len(make(" " + "あ" * limit + " ").name), limit)
                with self.assertRaisesRegex(InvalidName, f"{kind}名は{limit}文字以内"):
                    make("あ" * (limit + 1))

    def test_rename_checks_the_same_way(self):
        for kind, (make, limit) in KINDS.items():
            entity = make("元の名前")
            with self.subTest(kind=kind):
                entity.rename("  新しい名前 ")
                self.assertEqual(entity.name, "新しい名前")
                for bad in ("", " ", "あ" * (limit + 1)):
                    with self.assertRaises(InvalidName):
                        entity.rename(bad)
                self.assertEqual(entity.name, "新しい名前", "検査に落ちた改名で名前が変わった")

    def test_invalid_name_is_a_domain_error(self):
        """画面は DomainError を捕まえてフォームのエラーにする。"""
        self.assertTrue(issubclass(InvalidName, DomainError))


class DisplayOrderAndLimitsTest(unittest.TestCase):
    def test_the_display_order_must_be_a_non_negative_integer(self):
        for make in (
            lambda order: League(name="L", display_order=order),
            lambda order: Team(name="T", display_order=order),
        ):
            self.assertEqual(make(0).display_order, 0)
            for bad in (-1, True, "1"):
                with self.subTest(order=bad), self.assertRaisesRegex(DomainError, "表示順"):
                    make(bad)

    def test_reorder_checks_the_display_order(self):
        for entity in (League(name="L"), Team(name="T")):
            entity.reorder(3)
            self.assertEqual(entity.display_order, 3)
            with self.assertRaises(DomainError):
                entity.reorder(-1)
            self.assertEqual(entity.display_order, 3)

    def test_the_registered_limit_must_be_empty_or_non_negative(self):
        league = League(name="L", registered_player_limit=None)
        league.change_rules(ForeignPlayerRules.UNLIMITED, 0)
        self.assertEqual((league.foreign_rules, league.registered_player_limit), (ForeignPlayerRules.UNLIMITED, 0))
        with self.assertRaisesRegex(DomainError, "支配下選手の登録枠"):
            league.change_rules(ForeignPlayerRules.NPB, -1)
        self.assertEqual(league.registered_player_limit, 0, "検査に落ちた変更で枠が変わった")
        self.assertEqual(league.foreign_rules, ForeignPlayerRules.UNLIMITED)
        with self.assertRaises(DomainError):
            League(name="L", registered_player_limit=-1)


class DeletionBlockerTest(unittest.TestCase):
    def test_a_league_without_teams_can_be_deleted(self):
        self.assertIsNone(league_deletion_blocker(0))
        self.assertEqual(
            league_deletion_blocker(2),
            "このリーグにはチームが2つあるため削除できません。先にチームを削除するか、ほかのリーグへ移してください。",
        )

    def test_a_team_without_stints_captaincies_and_games_can_be_deleted(self):
        self.assertIsNone(team_deletion_blocker(0, 0, 0))
        self.assertEqual(team_deletion_blocker(3, 0, 0), "このチームには選手の在籍の記録が3件あるため削除できません。")
        self.assertEqual(team_deletion_blocker(0, 1, 0), "このチームには主将の在任の記録が1件あるため削除できません。")
        self.assertEqual(team_deletion_blocker(0, 0, 5), "このチームには試合が5試合あるため削除できません。")
        self.assertEqual(
            team_deletion_blocker(1, 1, 1),
            "このチームには選手の在籍の記録が1件・主将の在任の記録が1件・試合が1試合あるため削除できません。",
        )

    def test_a_stadium_nobody_calls_home_can_be_deleted(self):
        self.assertIsNone(stadium_deletion_blocker(0, 0))
        self.assertEqual(
            stadium_deletion_blocker(1, 0), "この球場を本拠地にしているチーム（1チーム）があるため削除できません。"
        )

    def test_the_teams_of_the_pennant_worlds_also_keep_a_stadium(self):
        """世界は球場を共有する（2026-10-25 決定 1）。世界の球団だけが本拠地にしていても消せない。"""
        self.assertEqual(
            stadium_deletion_blocker(0, 2),
            "この球場を本拠地にしているチーム（ペナントの世界の2球団）があるため削除できません。",
        )
        self.assertEqual(
            stadium_deletion_blocker(1, 2),
            "この球場を本拠地にしているチーム（1チーム・ペナントの世界の2球団）があるため削除できません。",
        )

    def test_counts_must_be_non_negative_integers(self):
        for call in (
            lambda: league_deletion_blocker(-1),
            lambda: team_deletion_blocker(0, -1, 0),
            lambda: stadium_deletion_blocker(True, 0),
            lambda: league_change_blocker(-1),
        ):
            with self.assertRaises(ValueError):
                call()


class LeagueChangeTest(unittest.TestCase):
    """所属リーグは試合が1つも無いときだけ変えられる（2026-10-25 決定 2）。"""

    def test_a_team_without_games_can_change_its_league(self):
        self.assertIsNone(league_change_blocker(0))
        ensure_league_change_allowed(0)

    def test_a_team_with_games_cannot(self):
        message = "このチームには試合が1試合あるため、所属リーグを変えられません。"
        self.assertEqual(league_change_blocker(1), message)
        with self.assertRaisesRegex(LeagueChangeLocked, message):
            ensure_league_change_allowed(1)
        self.assertTrue(issubclass(LeagueChangeLocked, DomainError))


class MoveToLeagueLimitTest(unittest.TestCase):
    """移す先のリーグの支配下選手の登録枠を超えるチームは移せない（W3 T3）。育成は数えない。"""

    def setUp(self):
        self.team = Team(name="T", league_id=1, id=10)
        for number in ("1", "2"):
            self.team.add_player(
                f"支配下{number}", JerseyNumber(number), Position.PITCHER, 2026, limits=RosterLimits.UNLIMITED
            )
        self.team.add_player(
            "育成",
            JerseyNumber("101"),
            Position.PITCHER,
            2026,
            ContractStatus.DEVELOPMENTAL,
            limits=RosterLimits.UNLIMITED,
        )

    def test_a_team_that_fits_the_new_leagues_limit_moves(self):
        for limit in (2, None):
            with self.subTest(limit=limit):
                self.team.league_id = 1
                self.team.move_to_league(2, limits=RosterLimits(registered=limit))
                self.assertEqual(self.team.league_id, 2)

    def test_a_team_over_the_new_leagues_limit_does_not_move(self):
        message = (
            "「T」の支配下選手（2人）が、移す先のリーグの支配下選手の登録枠（1人）を超えるため、"
            "所属リーグを変えられません。"
        )
        with self.assertRaisesRegex(RegisteredPlayerLimitExceeded, message):
            self.team.move_to_league(2, limits=RosterLimits(registered=1))
        self.assertEqual(self.team.league_id, 1, "拒んだときは所属を変えない")

    def test_staying_in_the_same_league_is_not_checked(self):
        """今のリーグの枠を後から下げて超えているチームも、所属を変えない保存では咎めない。"""
        self.team.move_to_league(1, limits=RosterLimits(registered=0))
        self.assertEqual(self.team.league_id, 1)


if __name__ == "__main__":
    unittest.main()


class ApplyOrderTest(unittest.TestCase):
    """表示順の並べ替え（W3 T8）。識別子の並びに 0, 1, 2 … を振り直す。"""

    def _leagues(self) -> list[League]:
        return [
            League(name=name, id=league_id, display_order=order)
            for name, league_id, order in (
                ("A", 1, 0),
                ("B", 2, 1),
                ("C", 3, 5),
            )
        ]

    def test_the_orders_follow_the_ids(self):
        leagues = self._leagues()
        ordered = apply_order(leagues, [3, 1, 2])
        self.assertEqual([league.id for league in ordered], [3, 1, 2])
        self.assertEqual({league.id: league.display_order for league in leagues}, {3: 0, 1: 1, 2: 2})

    def test_teams_are_ordered_the_same_way(self):
        teams = [Team(name="甲", id=10, display_order=0), Team(name="乙", id=11, display_order=0)]
        apply_order(teams, [11, 10])
        self.assertEqual([team.display_order for team in teams], [1, 0])

    def test_an_order_that_does_not_match_changes_nothing(self):
        cases = {
            "欠け": [1, 2],
            "余り": [1, 2, 3, 4],
            "入れ替え": [1, 2, 4],
            "重複": [1, 2, 3, 3],
            "重複で数だけ合う": [1, 1, 2],
            "空": [],
        }
        for label, ids in cases.items():
            with self.subTest(label):
                leagues = self._leagues()
                with self.assertRaises(InvalidOrder) as raised:
                    apply_order(leagues, ids)
                self.assertEqual(str(raised.exception), ORDER_MISMATCH)
                self.assertEqual([league.display_order for league in leagues], [0, 1, 5])

    def test_no_rows_and_no_ids_is_nothing_to_do(self):
        self.assertEqual(apply_order([], []), [])
