"""ワークスペースの権限（`WorkspaceRole`・`WorkspaceAccess`）の業務ルール（DB 不要・Django 非依存）。

役割（持ち主・編集者・チーム担当・ログインした部外者・未ログイン）× 公開範囲 × 操作の真理値表を、
表そのものとして持つ（行を足し忘れると `test_the_table_covers_every_role_and_visibility` が落ちる）。
"""

import inspect
import unittest

from myapp.domain.exceptions import InvalidWorkspace
from myapp.domain.workspace import Visibility, WorkspaceAccess, WorkspaceRole

OWNER = 1
EDITOR = 2
MANAGER = 3
OUTSIDER = 4
MANAGED_TEAM = 10
OTHER_TEAM = 20
ANOTHER_TEAM = 30

PUBLIC = Visibility.PUBLIC
PRIVATE = Visibility.PRIVATE

# 利用者ごとの事実: (user_id, 編集者の行があるか, 担当チーム)
USERS = {
    "持ち主": (OWNER, False, ()),
    "編集者": (EDITOR, True, ()),
    "チーム担当": (MANAGER, False, (MANAGED_TEAM,)),
    "ログインした部外者": (OUTSIDER, False, ()),
    "未ログイン": (None, False, ()),
}

# 操作。担当のチーム・ほかのチーム・試合の2チーム（担当とほか）・どちらも担当でない試合の2チーム
OPERATIONS = {
    "閲覧": lambda a: a.can_view(),
    "全体の編集": lambda a: a.can_edit_all(),
    "担当のチームの編集": lambda a: a.can_edit_teams([MANAGED_TEAM]),
    "ほかのチームの編集": lambda a: a.can_edit_teams([OTHER_TEAM]),
    "試合（担当とほか）の編集": lambda a: a.can_edit_teams([OTHER_TEAM, MANAGED_TEAM]),
    "試合（どちらもほか）の編集": lambda a: a.can_edit_teams([OTHER_TEAM, ANOTHER_TEAM]),
    "世界の作成": lambda a: a.can_create_world(),
    "登録（リーグ・チーム・球場）": lambda a: a.can_set_up(),
    "メンバーの管理": lambda a: a.can_manage_members(),
    "削除": lambda a: a.can_delete(),
}
COLUMNS = tuple(OPERATIONS)

Y, N = True, False
# (利用者, 公開範囲) → (役割, 操作ごとの可否。COLUMNS の順)
TABLE = {
    ("持ち主", PUBLIC): (WorkspaceRole.OWNER, (Y, Y, Y, Y, Y, Y, Y, Y, Y, Y)),
    ("持ち主", PRIVATE): (WorkspaceRole.OWNER, (Y, Y, Y, Y, Y, Y, Y, Y, Y, Y)),
    ("編集者", PUBLIC): (WorkspaceRole.EDITOR, (Y, Y, Y, Y, Y, Y, Y, Y, N, N)),
    ("編集者", PRIVATE): (WorkspaceRole.EDITOR, (Y, Y, Y, Y, Y, Y, Y, Y, N, N)),
    ("チーム担当", PUBLIC): (WorkspaceRole.TEAM_MANAGER, (Y, N, Y, N, Y, N, N, N, N, N)),
    ("チーム担当", PRIVATE): (WorkspaceRole.TEAM_MANAGER, (Y, N, Y, N, Y, N, N, N, N, N)),
    ("ログインした部外者", PUBLIC): (WorkspaceRole.VIEWER, (Y, N, N, N, N, N, N, N, N, N)),
    ("ログインした部外者", PRIVATE): (WorkspaceRole.NONE, (N, N, N, N, N, N, N, N, N, N)),
    ("未ログイン", PUBLIC): (WorkspaceRole.VIEWER, (Y, N, N, N, N, N, N, N, N, N)),
    ("未ログイン", PRIVATE): (WorkspaceRole.NONE, (N, N, N, N, N, N, N, N, N, N)),
}


def access_for(user: str, visibility: Visibility) -> WorkspaceAccess:
    user_id, is_editor, managed = USERS[user]
    return WorkspaceAccess.of(
        owner_id=OWNER, visibility=visibility, user_id=user_id, is_editor=is_editor, managed_team_ids=managed
    )


class PermissionTableTest(unittest.TestCase):
    def test_the_table_covers_every_role_and_visibility(self):
        self.assertEqual(set(TABLE), {(user, visibility) for user in USERS for visibility in Visibility})
        for expected in TABLE.values():
            self.assertEqual(len(expected[1]), len(COLUMNS))

    def test_each_role_is_derived_from_the_facts(self):
        for (user, visibility), (role, _) in TABLE.items():
            with self.subTest(user=user, visibility=visibility.label):
                self.assertIs(access_for(user, visibility).role, role)

    def test_each_operation_follows_the_table(self):
        for (user, visibility), (_, allowed) in TABLE.items():
            access = access_for(user, visibility)
            for column, expected in zip(COLUMNS, allowed, strict=True):
                with self.subTest(user=user, visibility=visibility.label, operation=column):
                    self.assertIs(OPERATIONS[column](access), expected)

    def test_members_are_the_owner_the_editors_and_the_team_managers(self):
        for (user, visibility), (role, _) in TABLE.items():
            with self.subTest(user=user, visibility=visibility.label):
                expected = role in (WorkspaceRole.OWNER, WorkspaceRole.EDITOR, WorkspaceRole.TEAM_MANAGER)
                self.assertIs(access_for(user, visibility).is_member, expected)
                self.assertIs(role.is_member, expected)

    def test_creating_a_world_needs_the_right_to_edit_everything(self):
        """世界の作成は持ち主・編集者だけ（2026-10-24 決定）。公開ワークスペースの閲覧者・担当は作れない。"""
        for user, visibility in TABLE:
            access = access_for(user, visibility)
            with self.subTest(user=user, visibility=visibility.label):
                self.assertIs(access.can_create_world(), access.can_edit_all())

    def test_setting_up_leagues_needs_the_right_to_edit_everything(self):
        """登録画面（リーグ・チーム・球場）は見るのも書くのも持ち主・編集者だけ（W3。2026-10-25）。担当・閲覧には見せない。"""
        for user, visibility in TABLE:
            access = access_for(user, visibility)
            with self.subTest(user=user, visibility=visibility.label):
                self.assertIs(access.can_set_up(), access.can_edit_all())


class RolePriorityTest(unittest.TestCase):
    """役割の優先は 持ち主 > 編集者 > チーム担当 > 閲覧。"""

    def _role(self, *, user_id, is_editor=False, managed=(), visibility=PRIVATE):
        return WorkspaceAccess.of(
            owner_id=OWNER, visibility=visibility, user_id=user_id, is_editor=is_editor, managed_team_ids=managed
        )

    def test_an_owner_who_also_manages_a_team_is_the_owner(self):
        access = self._role(user_id=OWNER, managed=(MANAGED_TEAM,))
        self.assertIs(access.role, WorkspaceRole.OWNER)
        self.assertEqual(access.managed_team_ids, frozenset(), "全体を編集できる人は担当チームを持たない")
        self.assertTrue(access.can_edit_teams([OTHER_TEAM]))

    def test_an_owner_with_an_editor_row_is_the_owner(self):
        self.assertIs(self._role(user_id=OWNER, is_editor=True).role, WorkspaceRole.OWNER)

    def test_an_editor_who_also_manages_a_team_is_an_editor(self):
        access = self._role(user_id=EDITOR, is_editor=True, managed=(MANAGED_TEAM,))
        self.assertIs(access.role, WorkspaceRole.EDITOR)
        self.assertEqual(access.managed_team_ids, frozenset())
        self.assertTrue(access.can_edit_teams([OTHER_TEAM]))

    def test_a_manager_of_several_teams_can_edit_each_of_them_only(self):
        access = self._role(user_id=MANAGER, managed=[MANAGED_TEAM, ANOTHER_TEAM, MANAGED_TEAM])
        self.assertIs(access.role, WorkspaceRole.TEAM_MANAGER)
        self.assertEqual(access.managed_team_ids, frozenset({MANAGED_TEAM, ANOTHER_TEAM}))
        self.assertTrue(access.can_edit_teams([ANOTHER_TEAM]))
        self.assertFalse(access.can_edit_teams([OTHER_TEAM]))

    def test_a_manager_cannot_edit_when_no_team_is_given(self):
        """チームを渡さなければ、担当は何も編集できない（全体を編集できる人だけが可）。"""
        self.assertFalse(self._role(user_id=MANAGER, managed=(MANAGED_TEAM,)).can_edit_teams([]))
        self.assertTrue(self._role(user_id=OWNER).can_edit_teams([]))

    def test_the_team_ids_can_be_any_iterable(self):
        access = self._role(user_id=MANAGER, managed=(MANAGED_TEAM,))
        self.assertTrue(access.can_edit_teams(iter([OTHER_TEAM, MANAGED_TEAM])))
        self.assertTrue(access.can_edit_teams({MANAGED_TEAM}))


class NoStaffPrivilegeTest(unittest.TestCase):
    """サイト側の `is_staff` の特権は、入れようがない形にする（設計書 2.・4.）。"""

    def test_the_builders_take_no_staff_flag(self):
        for build in (WorkspaceAccess.of, WorkspaceRole.of):
            with self.subTest(build=build.__qualname__):
                names = set(inspect.signature(build).parameters)
                self.assertFalse({name for name in names if "staff" in name or "superuser" in name})

    def test_the_value_holds_no_staff_flag(self):
        names = set(inspect.signature(WorkspaceAccess).parameters)
        self.assertEqual(names, {"role", "visibility", "managed_team_ids"})


class RoleLabelTest(unittest.TestCase):
    def test_every_role_has_a_japanese_label(self):
        self.assertEqual(
            {role: role.label for role in WorkspaceRole},
            {
                WorkspaceRole.OWNER: "持ち主",
                WorkspaceRole.EDITOR: "編集者",
                WorkspaceRole.TEAM_MANAGER: "チーム担当",
                WorkspaceRole.VIEWER: "閲覧",
                WorkspaceRole.NONE: "関係なし",
            },
        )


class InvalidAccessTest(unittest.TestCase):
    """役割と公開範囲・担当チームの組み合わせが食い違う値は作れない。"""

    def test_an_anonymous_user_cannot_be_an_editor_or_a_manager(self):
        for is_editor, managed in ((True, ()), (False, (MANAGED_TEAM,))):
            with self.subTest(is_editor=is_editor, managed=managed), self.assertRaises(InvalidWorkspace):
                WorkspaceAccess.of(
                    owner_id=OWNER, visibility=PUBLIC, user_id=None, is_editor=is_editor, managed_team_ids=managed
                )

    def test_a_team_manager_needs_a_team(self):
        with self.assertRaises(InvalidWorkspace):
            WorkspaceAccess(role=WorkspaceRole.TEAM_MANAGER, visibility=PUBLIC)

    def test_only_a_team_manager_holds_teams(self):
        for role in (WorkspaceRole.OWNER, WorkspaceRole.EDITOR, WorkspaceRole.VIEWER):
            with self.subTest(role=role), self.assertRaises(InvalidWorkspace):
                WorkspaceAccess(role=role, visibility=PUBLIC, managed_team_ids=frozenset({MANAGED_TEAM}))

    def test_a_viewer_who_is_not_a_member_exists_only_in_a_public_workspace(self):
        with self.assertRaises(InvalidWorkspace):
            WorkspaceAccess(role=WorkspaceRole.VIEWER, visibility=PRIVATE)
        with self.assertRaises(InvalidWorkspace):
            WorkspaceAccess(role=WorkspaceRole.NONE, visibility=PUBLIC)

    def test_types_are_checked(self):
        for kwargs in (
            {"role": "owner", "visibility": PUBLIC},
            {"role": WorkspaceRole.OWNER, "visibility": "public"},
            {"role": WorkspaceRole.TEAM_MANAGER, "visibility": PUBLIC, "managed_team_ids": {MANAGED_TEAM}},
            {"role": WorkspaceRole.TEAM_MANAGER, "visibility": PUBLIC, "managed_team_ids": frozenset({True})},
            {"role": WorkspaceRole.TEAM_MANAGER, "visibility": PUBLIC, "managed_team_ids": frozenset({"10"})},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(InvalidWorkspace):
                WorkspaceAccess(**kwargs)  # type: ignore[arg-type]
        with self.assertRaises(InvalidWorkspace):
            WorkspaceRole.of(
                owner_id=OWNER,
                visibility="public",  # type: ignore[arg-type]
                user_id=OUTSIDER,
                is_editor=False,
                manages_a_team=False,
            )

    def test_the_value_is_frozen(self):
        access = WorkspaceAccess(role=WorkspaceRole.OWNER, visibility=PUBLIC)
        with self.assertRaises(AttributeError):
            access.role = WorkspaceRole.VIEWER  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
