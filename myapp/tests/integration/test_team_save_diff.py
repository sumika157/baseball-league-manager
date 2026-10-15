"""`Team` の保存は、今の行と比べて変わった行だけを書く（#176）。

全員を書き直していたころは、シーズンを締める処理が引退した選手まで含めて約1.3万行を書き、その間ずっと
書き込みロックを持っていた。ここでは次を確かめる。

- 変わっていない集約を保存しても、選手・在籍・FA 宣言・主将の行に1文も書かない（全員を書き直す形に戻ると落ちる）
- 書く文の数は球団の人数によらない（同じ変わり方の行は1文にまとめる。変わり方が多いときは `bulk_update` の1文）
- 比べる列に取りこぼしが無い（書く値のどの列が DB と食い違っても、保存で集約の値に戻る）
- 保存した後の DB の状態は、全員を書き直す形（変更前の `save()`）と同じ（シーズンを締める処理で突き合わせる）
"""

import re
from datetime import date

from django.db import connection, models, transaction
from django.test.utils import CaptureQueriesContext

from myapp.domain.entities import Team
from myapp.domain.pennant.world import WorldScope
from myapp.domain.value_objects import (
    AcquisitionRoute,
    ContractStatus,
    FreeAgencyKind,
    Handedness,
    JerseyNumber,
    Position,
    Profile,
    RosterLimits,
)
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import (
    DjangoTeamRepository,
    _captaincy_values,
    _declaration_values,
    _player_values,
    _profile_defaults,
    _stint_values,
)
from myapp.presentation.views import build_pennant_offseason_service

from ..helpers import register_players
from .base import BaseCase
from .test_pennant_offseason import ClosedSeasonCase

# `register_players` は年を省いて加える（現実の今日の年で在籍を開く）ので、FA 宣言や主将の年もそれに合わせる
YEAR = date.today().year
UNLIMITED = RosterLimits.UNLIMITED

# 集約の保存が書く表（球団の行は1行の update_or_create のまま）
ROSTER_TABLES = frozenset(
    model._meta.db_table
    for model in (
        orm_models.Player,
        orm_models.PlayerStint,
        orm_models.PlayerFreeAgentDeclaration,
        orm_models.Captaincy,
    )
)
_WRITE = re.compile(r'^(?:INSERT INTO|UPDATE|DELETE FROM) "(\w+)"')


def roster_writes(context: CaptureQueriesContext) -> list[str]:
    """選手・在籍・FA 宣言・主将の表への INSERT / UPDATE / DELETE の文。"""
    return [
        query["sql"]
        for query in context.captured_queries
        if (matched := _WRITE.match(query["sql"])) and matched.group(1) in ROSTER_TABLES
    ]


class _RewritingEveryoneRepository(DjangoTeamRepository):
    """変更前の `save()`（集約の全員を行ごとに update_or_create で書き直す）。結果を突き合わせる物差し。"""

    @transaction.atomic
    def save(self, team: Team) -> Team:
        team_row, _ = orm_models.Team.objects.update_or_create(  # type: ignore[misc]
            id=team.id,
            defaults={
                "league_id": team.league_id,
                "name": team.name,
                "home_stadium_id": team.home_stadium_id,
                "display_order": team.display_order,
            },
        )
        team.id = team_row.id
        for player in team.players:
            row, _ = orm_models.Player.objects.update_or_create(  # type: ignore[misc]
                id=player.id,
                defaults={"name": player.name, "position": player.position.value, **_profile_defaults(player.profile)},
            )
            player.id = row.id
            for stint in player.career:
                stint_row, _ = orm_models.PlayerStint.objects.update_or_create(  # type: ignore[misc]
                    id=stint.id, defaults=_stint_values(stint, row.id)
                )
                stint.id = stint_row.id
            if player.removed_declaration_ids:
                orm_models.PlayerFreeAgentDeclaration.objects.filter(
                    player=row, id__in=player.removed_declaration_ids
                ).delete()
                player.removed_declaration_ids.clear()
            for declaration in player.fa_declarations:
                declaration_row, _ = orm_models.PlayerFreeAgentDeclaration.objects.update_or_create(  # type: ignore[misc]
                    id=declaration.id, defaults=_declaration_values(declaration, row.id)
                )
                declaration.id = declaration_row.id
            for captaincy in player.captaincies:
                captaincy_row, _ = orm_models.Captaincy.objects.update_or_create(  # type: ignore[misc]
                    id=captaincy.id, defaults=_captaincy_values(captaincy, row.id)
                )
                captaincy.id = captaincy_row.id
        return team


FULL_PROFILE = Profile(
    birth_date=date(1995, 4, 2),
    throws=Handedness.RIGHT,
    bats=Handedness.LEFT,
    height_cm=180,
    weight_kg=82,
    birthplace="宮城",
    debut_year=2018,
    high_school="青葉高",
    university="杜大",
    corporate_team="仙台製鉄",
    nationality="日本",
    name_kana="あおば",
    back_name="AOBA",
    is_foreign_player=False,
)


class TeamSaveWritesOnlyChangesTest(BaseCase):
    """実データの球団。主将・FA 宣言・閉じた在籍（昇格前の背番号と経路つき）まで、全部の表に行がある形にする。"""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        register_players(cls.team, [(f"選手{n}", n, "内野手") for n in range(1, 31)])
        register_players(cls.rival, [(f"相手{n}", n, "内野手") for n in range(1, 6)])
        teams = DjangoTeamRepository(WorldScope.real())
        team = teams.find_by_id(cls.team.id)
        first = team.active_players[0]
        first.profile = FULL_PROFILE
        team.appoint_captain(first, year=YEAR)
        team.declare_free_agency(cls.saved(first.id), YEAR, FreeAgencyKind.DOMESTIC)
        # 育成で入って昇格し、経路の付いた在籍を閉じた選手（在籍の全列に値がある）
        promoted = team.add_player(
            "昇格選手", JerseyNumber("120"), Position.PITCHER, from_year=YEAR - 2,
            contract=ContractStatus.DEVELOPMENTAL, acquired_via=AcquisitionRoute.DEVELOPMENTAL_DRAFT, limits=UNLIMITED,
        )  # fmt: skip
        teams.save(team)
        team.promote_player(cls.saved(promoted.id), JerseyNumber("99"), year=YEAR - 1, limits=UNLIMITED)
        team.release_player(promoted, year=YEAR)
        teams.save(team)
        cls.captain_id = cls.saved(first.id)
        cls.promoted_id = cls.saved(promoted.id)

    @staticmethod
    def saved(value: int | None) -> int:
        assert value is not None
        return value

    def setUp(self):
        super().setUp()
        self.teams = DjangoTeamRepository(WorldScope.real())

    def save_capturing(self, team: Team) -> list[str]:
        with CaptureQueriesContext(connection) as context:
            self.teams.save(team)
        return roster_writes(context)

    def test_saving_an_unchanged_team_writes_no_roster_rows(self):
        team = self.teams.find_by_id(self.team.id)
        captain = team.find_player(self.captain_id)
        self.assertTrue(captain.captaincies and captain.fa_declarations, "前提: 主将と FA 宣言の行がある")

        self.assertEqual(self.save_capturing(team), [])

    def test_the_writes_do_not_grow_with_the_roster(self):
        def retire_one_and_add_one(team_id: int) -> list[str]:
            team = self.teams.find_by_id(team_id)
            team.retire_player(team.active_players[-1], YEAR)
            team.add_player("新人", JerseyNumber("77"), Position.OUTFIELDER, from_year=YEAR + 1, limits=UNLIMITED)
            return self.save_capturing(team)

        large = retire_one_and_add_one(self.team.id)
        small = retire_one_and_add_one(self.rival.id)

        self.assertEqual(len(large), len(small))
        # 退団の在籍の UPDATE・新人の選手と在籍の INSERT（退団した選手の行は変わらない）
        self.assertEqual(len(large), 3, large)

    def test_rows_changed_in_the_same_way_are_written_by_one_statement(self):
        team = self.teams.find_by_id(self.team.id)
        for player in team.active_players[1:6]:  # 0番目は主将（主将の在任も閉じる）
            team.retire_player(player, YEAR)

        writes = self.save_capturing(team)

        self.assertEqual(len(writes), 1, writes)

    def test_many_different_changes_are_all_written(self):
        """変わり方が多い（行ごとに違う）ときは `bulk_update` の1文にまとめ、どの行も集約の値に戻る。"""
        team = self.teams.find_by_id(self.team.id)
        players = team.active_players[:25]
        for player in players:
            orm_models.Player.objects.filter(id=player.id).update(name=f"書き換え{player.id}")

        writes = self.save_capturing(team)

        self.assertEqual(len(writes), 1, writes)
        names = dict(orm_models.Player.objects.filter(id__in=[p.id for p in players]).values_list("id", "name"))
        self.assertEqual(names, {player.id: player.name for player in players})

    def test_many_different_changes_of_foreign_keys_and_nulls_are_all_written(self):
        """`bulk_update` に落ちる経路でも、外部キー（`player_id`・`team_id`）と空（None）に戻す列が書かれる。"""
        team = self.teams.find_by_id(self.team.id)
        stints = [(player, player.career[0]) for player in team.active_players[:25]]
        self.assertTrue(all(stint.promoted_year is None and stint.to_year is None for _, stint in stints), "前提")
        # 行ごとに違う変わり方にする（選手は1つずらす。球団を変えるので選手・球団・加入年の一意には触れない）
        for index, (_, stint) in enumerate(stints):
            next_player, _ = stints[(index + 1) % len(stints)]
            orm_models.PlayerStint.objects.filter(id=stint.id).update(
                player_id=next_player.id,
                team_id=self.rival.id,
                number=str(500 + index),
                to_year=YEAR + 1 + index,
                promoted_year=YEAR,
                acquired_via=AcquisitionRoute.TRADE.value,
            )

        writes = self.save_capturing(team)

        self.assertEqual(len(writes), 1, writes)
        for player, stint in stints:
            row = orm_models.PlayerStint.objects.filter(id=stint.id).values(*_stint_values(stint, 0)).get()
            self.assertEqual(row, _stint_values(stint, self.saved(player.id)))

    def test_new_rows_get_their_ids_back(self):
        team = self.teams.find_by_id(self.team.id)
        player = team.add_player("新人", JerseyNumber("77"), Position.OUTFIELDER, from_year=YEAR, limits=UNLIMITED)
        player.declare_free_agency(YEAR, FreeAgencyKind.OVERSEAS)

        self.teams.save(team)

        assert player.id is not None and player.career[0].id is not None
        self.assertEqual(orm_models.Player.objects.get(id=player.id).name, "新人")
        self.assertEqual(orm_models.PlayerStint.objects.get(id=player.career[0].id).player_id, player.id)
        declaration = player.fa_declarations[0]
        assert declaration.id is not None
        self.assertEqual(orm_models.PlayerFreeAgentDeclaration.objects.get(id=declaration.id).kind, "海外")

    def test_a_row_missing_for_a_held_id_is_made_again_with_that_id(self):
        team = self.teams.find_by_id(self.team.id)
        captaincy = team.find_player(self.captain_id).captaincies[0]
        orm_models.Captaincy.objects.filter(id=captaincy.id).delete()

        self.teams.save(team)

        self.assertTrue(orm_models.Captaincy.objects.filter(id=captaincy.id, player_id=self.captain_id).exists())

    def test_rows_the_team_does_not_hold_are_kept(self):
        team = self.teams.find_by_id(self.team.id)
        left_out = team.players.pop()
        assert left_out.id is not None
        before = list(orm_models.PlayerStint.objects.filter(player_id=left_out.id).values())

        self.teams.save(team)

        self.assertTrue(orm_models.Player.objects.filter(id=left_out.id).exists())
        self.assertEqual(list(orm_models.PlayerStint.objects.filter(player_id=left_out.id).values()), before)

    def test_the_written_columns_cover_every_column_of_the_tables(self):
        """書く値の dict のキーが表の全列（id を除く）。列を足して書き忘れると、その列だけ保存されない。"""
        team = self.teams.find_by_id(self.team.id)
        player = team.find_player(self.promoted_id)
        stint = player.career[0]
        captain = team.find_player(self.captain_id)
        cases = [
            (orm_models.Player, _player_values(player)),
            (orm_models.PlayerStint, _stint_values(stint, self.promoted_id)),
            (orm_models.PlayerFreeAgentDeclaration, _declaration_values(captain.fa_declarations[0], self.captain_id)),
            (orm_models.Captaincy, _captaincy_values(captain.captaincies[0], self.captain_id)),
        ]
        for model, values in cases:
            with self.subTest(model=model.__name__):
                columns = {field.attname for field in model._meta.concrete_fields if not field.primary_key}
                self.assertEqual(set(values), columns)

    def test_every_written_column_is_compared(self):
        """DB の列を1つずつ集約と違う値に書き換え、保存で集約の値に戻ること（比べる列の取りこぼしを捕まえる）。"""
        team = self.teams.find_by_id(self.team.id)
        released = team.find_player(self.promoted_id)
        captain = team.find_player(self.captain_id)
        rows = [
            (orm_models.Player, captain.id, _player_values(captain)),
            (orm_models.PlayerStint, released.career[0].id, _stint_values(released.career[0], self.promoted_id)),
            (
                orm_models.PlayerFreeAgentDeclaration,
                captain.fa_declarations[0].id,
                _declaration_values(captain.fa_declarations[0], self.captain_id),
            ),
            (
                orm_models.Captaincy,
                captain.captaincies[0].id,
                _captaincy_values(captain.captaincies[0], self.captain_id),
            ),
        ]
        for model, row_id, values in rows:
            for column, value in values.items():
                with self.subTest(model=model.__name__, column=column):
                    tampered = self._other_value(model, column, value)
                    model._default_manager.filter(id=row_id).update(**{column: tampered})

                    self.teams.save(team)

                    self.assertEqual(
                        model._default_manager.filter(id=row_id).values_list(column, flat=True).get(), value
                    )

    def _other_value(self, model: type[models.Model], column: str, value):
        """その列で、いまの値と違う（制約に触れない）値。"""
        field = next(f for f in model._meta.concrete_fields if f.attname == column)
        if isinstance(field, models.ForeignKey):
            if field.related_model is orm_models.Team:
                return self.rival.id
            return orm_models.Player.objects.exclude(id=value).values_list("id", flat=True).first()
        if isinstance(field, models.BooleanField):
            return not value
        if isinstance(field, models.DateField):
            return date(1990, 1, 1)
        if isinstance(field, models.IntegerField):
            return 1900 if value is None else value + 1000
        assert isinstance(field, models.CharField), column
        return "x" if value != "x" else "y"


class CloseSeasonSameAsRewritingEveryoneTest(ClosedSeasonCase):
    """シーズンを締めた後の DB は、全員を書き直す保存（変更前）で締めたときと同じ。同じ世界を巻き戻して比べる。"""

    def _close_and_dump(self, *, rewriting_everyone: bool):
        savepoint = transaction.savepoint()
        try:
            service = build_pennant_offseason_service(self.world_a)
            if rewriting_everyone:
                service._teams = _RewritingEveryoneRepository(WorldScope.pennant(self.world_a))
            with CaptureQueriesContext(connection) as context:
                result = service.close_season()
            return result, self._dump(), len(roster_writes(context))
        finally:
            transaction.savepoint_rollback(savepoint)

    def _dump(self):
        player_ids = orm_models.PlayerStint.objects.filter(team__league__world_id=self.world_a).values("player_id")
        return {
            model.__name__: list(model._default_manager.filter(**{key: player_ids}).order_by("id").values())
            for model, key in (
                (orm_models.Player, "id__in"),
                (orm_models.PlayerStint, "player_id__in"),
                (orm_models.PlayerFreeAgentDeclaration, "player_id__in"),
                (orm_models.Captaincy, "player_id__in"),
                (orm_models.PennantPlayerRatings, "player_id__in"),
            )
        }

    def test_the_rows_equal_those_of_rewriting_everyone(self):
        result, rows, writes = self._close_and_dump(rewriting_everyone=False)
        expected_result, expected_rows, rewrites = self._close_and_dump(rewriting_everyone=True)

        self.assertGreater(result.draftee_count, 0)
        self.assertEqual(result, expected_result)
        self.assertEqual(rows, expected_rows)
        # 書く文は、全員を書き直すときより桁で少ない
        self.assertLess(writes * 10, rewrites, (writes, rewrites))
