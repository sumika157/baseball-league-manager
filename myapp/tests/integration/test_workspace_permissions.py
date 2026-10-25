"""サイトの書き込みの権限の真理値表（W2 T5。#78）。役割 × 画面 × 公開範囲。

チームの書き込みは `workspace_view` が解決した `WorkspaceAccess` だけで判定する（持ち主・編集者は全体、チーム担当は
担当のチームが関わる範囲）。**`is_staff`・superuser はサイトでは何の権限も持たない**（管理画面に入れるだけ）。
特権を戻すと、staff の部外者・superuser の部外者の行が落ちる。

画面ごとに、書き込みの導線が出る／出ない（押しても弾かれる導線を見せない）と、書き込みが通る／403／ログインへ／404 を
同じ表で確かめる。公開のワークスペースと非公開のワークスペースで、メンバーの行は同じ。非公開では部外者は
どの画面も 404、未ログインはログインへ（API は 403 の JSON）。
"""

import itertools
import json

from django.contrib.auth.models import User

from myapp.domain.workspace import Visibility
from myapp.infrastructure import orm_models
from myapp.presentation.views import build_roster_service

from ..helpers import (
    GAME_DAY_UNRECORDED,
    make_editor,
    make_league,
    make_workspace,
    play_game,
    real_scope,
    ws_url,
)
from .base import BaseCase

# 確かめる項目（この順で、役割ごとの期待を並べる）
#   導線: Y（出る）・N（出ない。画面は 200）・404・login
#   GET の画面: 200・403・404・login
#   書き込み: ok（保存された）・refused（フォームのエラーで保存されない）・403・404・login
CHECKS = (
    "選手一覧の登録の導線",
    "選手詳細の編集の導線",
    "試合一覧の登録の導線",
    "試合詳細の編集の導線",
    "ペナントの作成フォーム",
    "試合の登録（GET）",
    "試合の編集（GET）",
    "選手の編集（GET）",
    "選手の登録（POST）",
    "選手の編集（POST）",
    "試合の登録（POST）",
    "スコアブックの保存",
    "ペナントの作成（POST）",
)

# 役割 → 期待（CHECKS の順）。公開のワークスペース
_MEMBERS = {
    "持ち主": "Y Y Y Y Y 200 200 200 ok ok ok ok ok",
    "編集者": "Y Y Y Y Y 200 200 200 ok ok ok ok ok",
    "担当": "Y Y Y Y N 200 200 200 ok ok ok ok 403",
    # ほかのチームの担当: 試合の登録の画面は開ける（担当のチームがある）が、担当していない2チームの試合は登録できない
    "ほかのチームの担当": "N N Y N N 200 403 403 403 403 refused 403 403",
}
_NO_RIGHTS = "N N N N N 403 403 403 403 403 403 403 403"
PUBLIC = {
    **_MEMBERS,
    "ログインした部外者": _NO_RIGHTS,
    "staff の部外者": _NO_RIGHTS,
    "superuser の部外者": _NO_RIGHTS,
    "未ログイン": "N N N N N login login login login login login 403 login",
}
# 非公開: メンバーは公開と同じ。部外者は存在を明かさない（404）、未ログインはログインへ（API は 403）
_HIDDEN = " ".join(["404"] * len(CHECKS))
PRIVATE = {
    **_MEMBERS,
    "ログインした部外者": _HIDDEN,
    "staff の部外者": _HIDDEN,
    "superuser の部外者": _HIDDEN,
    "未ログイン": " ".join(["login"] * (len(CHECKS) - 2) + ["403", "login"]),
}


class PermissionTableCase(BaseCase):
    """1つのワークスペースに、チーム3つ（担当のチーム・相手・別のリーグのほかのチーム）と選手・試合を置き、役割の人を作る。"""

    visibility = Visibility.PUBLIC
    expected: dict[str, str] = PUBLIC
    ws: orm_models.Workspace
    home: orm_models.Team
    away: orm_models.Team
    other_team: orm_models.Team
    league_id: int
    users: dict[str, User | None]

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        if cls.visibility is Visibility.PUBLIC:
            cls.ws = cls.workspace
            cls.home, cls.away = cls.team, cls.rival
            league = cls.league
        else:
            cls.ws = make_workspace(name="非公開のワークスペース", visibility=cls.visibility)
            league = make_league("非公開のリーグ", workspace=cls.ws)
            cls.home = orm_models.Team.objects.create(league=league, name="非公開のチーム")
            cls.away = orm_models.Team.objects.create(league=league, name="非公開の相手")
        cls.league_id = league.id
        # ほかのチームは別のリーグに置く（世界の作成は元のリーグの球団数で日程を組むので、2球団のリーグのままにする）
        other_league = make_league("ほかのリーグ", workspace=cls.ws)
        cls.other_team = orm_models.Team.objects.create(league=other_league, name="ほかのチーム")

        def user(name, **flags):
            return User.objects.create_user(username=name, password="x", **flags)

        manager = user("manager")
        cls.home.managers.add(manager)
        other_manager = user("other-manager")
        cls.other_team.managers.add(other_manager)
        cls.users = {
            "持ち主": cls.ws.owner,
            "編集者": make_editor(user("editor"), cls.ws),
            "担当": manager,
            "ほかのチームの担当": other_manager,
            "ログインした部外者": user("outsider"),
            "staff の部外者": user("staff", is_staff=True),
            "superuser の部外者": User.objects.create_superuser("root", password="x"),
            "未ログイン": None,
        }

    def setUp(self):
        super().setUp()
        self.skip_roster_check()
        self.player = build_roster_service(real_scope(self.ws)).register_player(self.home.id, "山田", 10, "内野手")
        self.game = play_game(self.home, self.away, recorded=False, scope=real_scope(self.ws))
        # 役割ごとに続けて保存するので、試合日を毎回変える（同じ内容の保存は行を変えず、通ったかを見分けられない）
        self.save_days = itertools.count(1)

    def url(self, name, *args):
        return ws_url(name, *args, workspace=self.ws)

    # --- 観測（どれも DB の変化と合わせて1つの記号にする） ---

    def _link(self, url, marker) -> str:
        response = self.client.get(url)
        if response.status_code == 302:
            return "login" if "/accounts/login/" in response["Location"] else f"302:{response['Location']}"
        if response.status_code != 200:
            return str(response.status_code)
        return "Y" if marker in response.content.decode() else "N"

    def _get(self, url) -> str:
        response = self.client.get(url)
        if response.status_code == 302 and "/accounts/login/" in response["Location"]:
            return "login"
        return str(response.status_code)

    def _write(self, response, changed: bool, *, ok_status=302) -> str:
        if response.status_code == 302 and "/accounts/login/" in response["Location"]:
            outcome = "login"
        elif response.status_code == ok_status and changed:
            return "ok"
        elif response.status_code == 200 and not changed:
            outcome = "refused"
        else:
            outcome = str(response.status_code)
        self.assertFalse(changed, f"{outcome} なのに保存された")
        return outcome

    def _observe(self, role: str) -> list[str]:
        home, player, game = self.home.id, self.player.id, self.game.id
        edit_url = self.url("player_edit", home, player)
        observed = [
            self._link(self.url("player_list", home), "新入団選手の登録"),
            self._link(self.url("player_detail", home, player), edit_url),
            self._link(self.url("game_list"), self.url("game_create")),
            self._link(self.url("game_detail", game), self.url("game_edit", game)),
            self._link(self.url("pennant_index"), 'name="leagues"'),
            self._get(self.url("game_create")),
            self._get(self.url("game_edit", game)),
            self._get(edit_url),
        ]

        # 役割ごとに続けて登録するので、背番号は呼ぶたびに変える（重複の検査で落ちて空振りしない）
        stints = orm_models.PlayerStint.objects.count()
        response = self.client.post(
            self.url("player_list", home),
            {"name": f"新人{role}", "number": str(20 + stints), "position": "内野手"},
        )
        observed.append(self._write(response, orm_models.PlayerStint.objects.count() != stints))

        name = f"改名{role}"
        response = self.client.post(edit_url, {"name": name, "number": "10", "position": "内野手"})
        observed.append(self._write(response, orm_models.Player.objects.get(id=player).name == name))

        games = orm_models.Game.objects.count()
        response = self.client.post(
            self.url("game_create"),
            {"year": "2026", "played_on": "2026-05-01", "home_team": home, "away_team": self.away.id},
        )
        observed.append(self._write(response, orm_models.Game.objects.count() != games))

        before = orm_models.Game.objects.filter(id=game).values().get()
        response = self.client.post(
            self.url("api_game_scorebook", game),
            data=json.dumps(
                {
                    **GAME_DAY_UNRECORDED,
                    "year": 2026,
                    "played_on": f"2026-06-{next(self.save_days):02d}",
                    "home_team": home,
                    "away_team": self.away.id,
                    "lineup": [],
                    "plate_appearances": [],
                }
            ),
            content_type="application/json",
        )
        changed = orm_models.Game.objects.filter(id=game).values().get() != before
        observed.append(self._write(response, changed, ok_status=200))

        worlds = orm_models.PennantWorld.objects.count()
        response = self.client.post(
            self.url("pennant_index"),
            {
                "name": f"{role}の世界",
                "leagues": [str(self.league_id)],
                "managed_team": str(home),
                "postseason_format": "NPB",
                "start_year": "2040",
            },
        )
        observed.append(self._write(response, orm_models.PennantWorld.objects.count() != worlds))
        return observed

    def check_the_table(self):
        for role, expected in self.expected.items():
            with self.subTest(role=role):
                user = self.users[role]
                if user is None:
                    self.client.logout()
                else:
                    self.client.force_login(user)
                observed = self._observe(role)
                self.assertEqual(
                    dict(zip(CHECKS, observed, strict=True)),
                    dict(zip(CHECKS, expected.split(), strict=True)),
                )


class PublicWorkspacePermissionTest(PermissionTableCase):
    """公開のワークスペース（既定のワークスペース）。"""

    def test_roles_by_pages(self):
        self.check_the_table()


class PrivateWorkspacePermissionTest(PermissionTableCase):
    """非公開のワークスペース。メンバーの行は公開と同じ、部外者は 404、未ログインはログインへ。"""

    visibility = Visibility.PRIVATE
    expected = PRIVATE

    def test_roles_by_pages(self):
        self.check_the_table()


class ManagerOfAnotherWorkspaceTest(PermissionTableCase):
    """ほかのワークスペースの担当・編集者は、このワークスペースでは部外者（担当チームは解決したワークスペースのだけを数える）。"""

    def test_members_of_another_workspace_have_no_rights_here(self):
        elsewhere = make_workspace(name="よそ")
        league = make_league("よそのリーグ", workspace=elsewhere)
        team = orm_models.Team.objects.create(league=league, name="よそのチーム")
        manager = User.objects.create_user(username="elsewhere-manager", password="x")
        team.managers.add(manager)
        editor = make_editor(User.objects.create_user(username="elsewhere-editor", password="x"), elsewhere)
        for user in (manager, editor, elsewhere.owner):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertEqual(self._observe(user.username), _NO_RIGHTS.split())
