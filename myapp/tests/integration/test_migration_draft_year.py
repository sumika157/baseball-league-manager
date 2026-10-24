"""0053: 入団年（debut_year）をドラフト年に移すデータ移行（#108）。"""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

BEFORE = [("myapp", "0052_player_draft_fields")]
AFTER = [("myapp", "0053_backfill_player_draft_year")]


class MigrationDraftYearTest(TransactionTestCase):
    def tearDown(self):
        # 後続のテストのために最新の状態へ戻す
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def _migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return MigrationExecutor(connection).loader.project_state(targets).apps

    def test_japanese_players_move_and_foreign_players_stay_empty(self):
        old = self._migrate(BEFORE)
        Player = old.get_model("myapp", "Player")
        # tearDown で最新まで進めるとき、リーグがあるので 0056（既定のワークスペース）が持ち主を要求する
        old.get_model("auth", "User").objects.create(username="admin", is_superuser=True)
        league = old.get_model("myapp", "League").objects.create(name="旧リーグ")
        team = old.get_model("myapp", "Team").objects.create(league=league, name="旧チーム")
        japanese = Player.objects.create(name="日本人", position="投手", debut_year=2020)
        foreign = Player.objects.create(name="外国人", position="投手", debut_year=2023, is_foreign_player=True)
        unknown = Player.objects.create(name="入団年なし", position="投手")
        too_old = Player.objects.create(name="範囲外", position="投手", debut_year=1900)
        old.get_model("myapp", "PlayerStint").objects.create(player=foreign, team=team, number="42", from_year=2023)
        # 最初の在籍が育成ドラフトの選手は育成の指名に。2つ目の在籍だけが育成ドラフトなら本指名のまま
        Stint = old.get_model("myapp", "PlayerStint")
        trainee = Player.objects.create(name="育成", position="投手", debut_year=2021)
        Stint.objects.create(
            player=trainee, team=team, number="120", from_year=2021, signed_as="育成", acquired_via="育成ドラフト"
        )
        later = Player.objects.create(name="後から育成", position="投手", debut_year=2018)
        Stint.objects.create(
            player=later, team=team, number="18", from_year=2018, to_year=2020, acquired_via="ドラフト"
        )
        Stint.objects.create(
            player=later, team=team, number="130", from_year=2022, signed_as="育成", acquired_via="育成ドラフト"
        )

        new = self._migrate(AFTER)

        moved = {
            p.name: (p.draft_year, p.draft_round, p.is_development_draft)
            for p in new.get_model("myapp", "Player").objects.all()
        }
        self.assertEqual(moved[japanese.name], (2019, None, False))
        self.assertEqual(moved[trainee.name], (2020, None, True))
        self.assertEqual(moved[later.name], (2017, None, False))
        self.assertEqual(moved[foreign.name], (None, None, False))
        self.assertEqual(moved[unknown.name], (None, None, False))
        # ドラフト年 1899 はシーズンの範囲（1900〜2100）の外なので移さない
        self.assertEqual(moved[too_old.name], (None, None, False))

        # 逆方向: ドラフト年のある選手はその翌年、外国人選手は最初の在籍の加入年に戻る
        new.get_model("myapp", "Player").objects.update(debut_year=None)
        back = self._migrate(BEFORE)
        restored = {p.name: p.debut_year for p in back.get_model("myapp", "Player").objects.all()}
        self.assertEqual(
            restored,
            {
                japanese.name: 2020,
                foreign.name: 2023,
                unknown.name: None,
                too_old.name: None,
                trainee.name: 2021,
                later.name: 2018,
            },
        )
