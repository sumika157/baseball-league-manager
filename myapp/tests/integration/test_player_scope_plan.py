"""選手を範囲で絞る SQL が、選手の表を全部走査しないこと（`EXPLAIN QUERY PLAN`。#74 仕上げ T2b）。

選手は範囲の列を持たず、在籍 → 球団 → リーグをたどって範囲が決まる（`scoping.players_in`）。選手の行ごとに
在籍を見る相関した `Exists` の形にすると、SQLite は選手の表を全部走査し、範囲が小さくても DB 全体の選手の数
（＝ワークスペースの数）に比例して遅くなる（200個の見本で選手検索 +59%・世界の球団ホーム +63%・見本の削除 約4倍）。
範囲のリーグ → 球団 → 在籍 → 選手の向き（`id IN (範囲の在籍の選手 id)`）なら、選手は主キーで引く。

計画が安定して確かめられる理由: テストの DB には `ANALYZE` の統計（`sqlite_stat1`）が無いので、SQLite は
行数ではなくスキーマ（索引）だけから計画を選ぶ。行を置かなくても、本番の大きさの写しと同じ計画になる
（T2b で、200個の見本を作った開発 DB の写しでも同じ計画になることを確かめた）。

見る SQL: `players_in` そのもの（実データ・ペナント）、それで絞る読み取り（能力・抽選・FA 宣言・年俸の契約・
id の照合）、選手検索、ワークスペースの削除と世界の削除の全 SQL。管理画面の概況の「実データの選手の数」
（`admin_real_queryset`）は全ワークスペースの選手を数えるので、選手の表を読むのは意味のうち（対象にしない）。
"""

import re

from django.db import connection
from django.db.models import Exists, OuterRef, QuerySet
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from myapp.domain.pennant.world import WorldScope
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoWorldRepository
from myapp.infrastructure.scoping import (
    declarations_in,
    draft_lotteries_in,
    players_in,
    ratings_in,
    salary_contracts_in,
)
from myapp.presentation.views import build_player_search_query

from ..helpers import (
    build_workspace_service,
    make_league,
    make_pennant_world,
    make_workspace,
    real_scope,
    register_players,
    world_scope,
)

PLAYER_TABLE = orm_models.Player._meta.db_table
# トランザクションの制御は計画を持たない
_CONTROL = ("SAVEPOINT", "RELEASE", "ROLLBACK", "BEGIN", "COMMIT")


def player_scans(sql: str, params: tuple = ()) -> list[str]:
    """`sql` の計画のうち、選手の表（別名を含む）を全部走査する行（`SCAN <選手の表>`。覆う索引の走査も含む）。"""
    aliases = {PLAYER_TABLE, *re.findall(rf'"{PLAYER_TABLE}" (?:AS )?"?(\w+)"?', sql)}
    with connection.cursor() as cursor:
        cursor.execute(f"EXPLAIN QUERY PLAN {sql}", params)
        plan = [row[3] for row in cursor.fetchall()]
    return [line for line in plan if (m := re.match(r"SCAN (\w+)", line)) and m.group(1) in aliases]


def queryset_scans(queryset: QuerySet) -> list[str]:
    sql, params = queryset.query.sql_with_params()
    return player_scans(sql, params)


class PlayerScopePlanCase(TestCase):
    """実データだけのワークスペース（消せる）と、世界を持つワークスペース。行は計画に効かないが、削除が通るだけ置く。"""

    workspace: orm_models.Workspace
    world_workspace: orm_models.Workspace
    world: orm_models.PennantWorld

    @classmethod
    def setUpTestData(cls):
        cls.workspace = make_workspace(name="消すワークスペース")
        team = orm_models.Team.objects.create(league=make_league("実データ", workspace=cls.workspace), name="球団")
        register_players(team, [("田中", 1, "投手")])
        cls.world_workspace = make_workspace(name="世界のあるワークスペース")
        cls.world = make_pennant_world("世界", workspace=cls.world_workspace, seed=1, start_year=2030)
        world_league = make_league("世界のリーグ", world=cls.world)
        world_team = orm_models.Team.objects.create(league=world_league, name="世界の球団")
        orm_models.PlayerStint.objects.create(
            player=orm_models.Player.objects.create(name="田村", position="投手"), team=world_team, from_year=2030
        )

    def scopes(self) -> dict[str, tuple[WorldScope, str]]:
        """範囲と、そこにいる選手の名前（検索が空振りしていないことを見る）。"""
        return {"実データ": (real_scope(self.workspace), "田中"), "ペナント": (world_scope(self.world.id), "田村")}

    def assert_no_player_scan(self, captured: list[dict]) -> None:
        statements = [query["sql"] for query in captured if not query["sql"].startswith(_CONTROL)]
        self.assertTrue(statements, "SQL を1本も拾えていない（空振り）")
        for sql in statements:
            with self.subTest(sql=sql[:160]):
                self.assertEqual(player_scans(sql), [])


class DetectorTest(PlayerScopePlanCase):
    def test_the_correlated_exists_form_is_reported(self):
        """空振りの防止: 直す前の形（選手の行ごとに在籍を見る `Exists`）は、選手の表の走査として拾う。"""
        in_scope = orm_models.PlayerStint.objects.filter(player_id=OuterRef("pk"), team__league__workspace_id=1)
        self.assertNotEqual(queryset_scans(orm_models.Player.objects.filter(Exists(in_scope))), [])


class PlayersInScopeTest(PlayerScopePlanCase):
    def test_players_in_reads_players_by_primary_key(self):
        for label, (scope, _) in self.scopes().items():
            with self.subTest(label):
                self.assertEqual(queryset_scans(players_in(scope)), [])
                self.assertEqual(queryset_scans(players_in(scope).filter(id__in=[1, 2])), [])
                self.assertEqual(queryset_scans(players_in(scope).filter(name__icontains="田").order_by("name")), [])

    def test_reads_filtered_by_the_players_of_the_scope(self):
        """世界の能力の読み取り（世界ダッシュボード・選手一覧・球団ホーム）・締めた年の抽選・年俸の契約（#40 K1a）。"""
        scope = world_scope(self.world.id)
        self.assertEqual(queryset_scans(ratings_in(scope).filter(year=2030)), [])
        self.assertEqual(queryset_scans(ratings_in(scope).filter(player_id__in=[1, 2], year=2030)), [])
        self.assertEqual(queryset_scans(draft_lotteries_in(scope)), [])
        self.assertEqual(queryset_scans(declarations_in(scope).filter(player_id__in=[1, 2])), [])
        self.assertEqual(
            queryset_scans(salary_contracts_in(scope).filter(first_year__lte=2030, last_year__gte=2030)), []
        )
        self.assertEqual(queryset_scans(salary_contracts_in(scope).filter(player_id__in=[1, 2])), [])

    def test_free_agent_declarations_are_read_through_the_players_of_the_scope(self):
        """FA 宣言の表も走査しない（宣言の行ごとに在籍を見る相関した `Exists` にすると、全世界の宣言を走査する）。"""
        sql, params = declarations_in(world_scope(self.world.id)).query.sql_with_params()
        with connection.cursor() as cursor:
            cursor.execute(f"EXPLAIN QUERY PLAN {sql}", params)
            plan = [row[3] for row in cursor.fetchall()]
        table = orm_models.PlayerFreeAgentDeclaration._meta.db_table
        self.assertEqual([line for line in plan if line.startswith(f"SCAN {table}")], [], plan)

    def test_player_search(self):
        """選手検索（実データの範囲だけの画面）。名前の部分一致と範囲を同じ1本で絞る。"""
        with CaptureQueriesContext(connection) as captured:
            rows = build_player_search_query(real_scope(self.workspace)).search("田")
        self.assertEqual([row.name for row in rows], ["田中"])
        self.assert_no_player_scan(captured.captured_queries)


class DeletionPlanTest(PlayerScopePlanCase):
    def test_deleting_a_workspace(self):
        with CaptureQueriesContext(connection) as captured:
            build_workspace_service().delete(self.workspace.id)
        self.assertFalse(orm_models.Workspace.objects.filter(id=self.workspace.id).exists())
        self.assert_no_player_scan(captured.captured_queries)

    def test_deleting_a_world(self):
        with CaptureQueriesContext(connection) as captured:
            DjangoWorldRepository(self.world_workspace.id).delete(self.world.id)
        self.assertFalse(orm_models.PennantWorld.objects.filter(id=self.world.id).exists())
        self.assert_no_player_scan(captured.captured_queries)
