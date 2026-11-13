"""ドラフト会議の記録の永続化（`DjangoDraftRepository`）と、選手の名前だけを読む参照（#297 D3 T1）。

往復（人数の幅・行動）・世界 × 年の一意・`expected_seq` の食い違い・範囲（別の世界・実データ）・世界の削除を見る。
世界は ORM で最小に作る（リーグ1・球団2。選手は要らない）。名前の参照は選手のいる世界（`WorldCase`）で見る。
"""

from unittest.mock import patch

from django.test import TestCase

from myapp.domain.exceptions import DraftConflict, DraftStarted, InvalidDraftAction, InvalidWorld, TeamNotFound
from myapp.domain.pennant.draft_meeting import DraftAction, DraftActionKind, DraftSlot
from myapp.domain.pennant.draft_record import DraftRecord
from myapp.domain.pennant.world import WorldScope
from myapp.domain.value_objects import Position
from myapp.infrastructure import orm_models
from myapp.infrastructure.queries import DjangoPlayerNameQuery
from myapp.infrastructure.repositories import DjangoDraftRepository, DjangoTeamRepository, DjangoWorldRepository

from .world_case import WorldCase

YEAR = 2027


def make_world(name: str) -> tuple[int, list[int]]:
    """世界ひとつ（リーグ1・球団2）。世界の id と球団の id を返す。"""
    world = orm_models.PennantWorld.objects.create(name=name, seed=7, start_year=2026)
    league = orm_models.League.objects.create(name=f"{name}リーグ", world=world)
    teams = [orm_models.Team.objects.create(league=league, name=f"{name}球団{i}") for i in (1, 2)]
    return world.id, [team.id for team in teams]


def slot(team_id: int, *, development_max: int = 2) -> DraftSlot:
    return DraftSlot(
        team_id=team_id,
        minimum=1,
        maximum=3,
        planned=(Position.PITCHER, Position.CATCHER, Position.PITCHER),
        base_counts={Position.PITCHER: 30, Position.CATCHER: 2, Position.INFIELDER: 12, Position.OUTFIELDER: 10},
        development_planned=(Position.OUTFIELDER,),
        development_max=development_max,
    )


def record(team_ids: list[int], actions=()) -> DraftRecord:
    return DraftRecord(
        year=YEAR,
        signature="0123456789abcdef0123456789abcdef",
        slots=(slot(team_ids[0], development_max=6), slot(team_ids[1])),
        actions=tuple(actions),
    )


class DraftRepositoryTest(TestCase):
    world_id: int
    team_ids: list[int]
    other_id: int
    other_team_ids: list[int]

    @classmethod
    def setUpTestData(cls):
        cls.world_id, cls.team_ids = make_world("甲")
        cls.other_id, cls.other_team_ids = make_world("乙")

    def repository(self, world_id: int | None = None) -> DjangoDraftRepository:
        return DjangoDraftRepository(WorldScope.pennant(self.world_id if world_id is None else world_id))

    def actions(self) -> list[DraftAction]:
        first, second = self.team_ids
        return [
            DraftAction(DraftActionKind.BID, first, 4),
            DraftAction(DraftActionKind.BID, second, 4),
            DraftAction(DraftActionKind.PICK, second, 9, extra_position=Position.CATCHER),
            DraftAction(DraftActionKind.PASS, first),
        ]

    def test_a_started_meeting_round_trips(self):
        saved = record(self.team_ids, self.actions())
        self.repository().start(saved)

        self.assertEqual(self.repository().find(YEAR), saved)
        self.assertEqual(self.repository().last_seq(YEAR), 4)

    def test_no_meeting_is_none_and_a_meeting_without_actions_is_zero(self):
        self.assertIsNone(self.repository().find(YEAR))
        self.assertIsNone(self.repository().last_seq(YEAR))
        self.repository().start(record(self.team_ids))
        self.assertEqual(self.repository().last_seq(YEAR), 0)
        self.assertIsNone(self.repository().last_seq(YEAR + 1), "年が違えば別の会議")

    def test_a_meeting_cannot_start_twice_in_a_year(self):
        self.repository().start(record(self.team_ids))
        with self.assertRaises(DraftStarted):
            self.repository().start(record(self.team_ids, self.actions()))
        self.assertEqual(self.repository().last_seq(YEAR), 0, "2度目は何も書かない")

    def test_append_needs_the_last_seq(self):
        actions = self.actions()
        self.repository().start(record(self.team_ids, actions[:2]))

        self.repository().append(YEAR, actions[2:3], expected_seq=2)
        with self.assertRaises(DraftConflict):
            self.repository().append(YEAR, actions[3:], expected_seq=2)  # 二重送信・別のタブ

        self.assertEqual(self.repository().find(YEAR), record(self.team_ids, actions[:3]))

    def test_append_without_a_meeting_is_a_conflict(self):
        with self.assertRaises(DraftConflict):
            self.repository().append(YEAR, self.actions(), expected_seq=0)

    def test_the_seq_constraint_is_the_last_line_of_defence(self):
        """読み直しの後に別のリクエストが同じ番号を先に書いたら、一意制約で断る（書きかけも残さない）。"""
        self.repository().start(record(self.team_ids))
        draft = orm_models.PennantDraft.objects.get(world_id=self.world_id, year=YEAR)
        original = DjangoDraftRepository._write_actions

        def race(row, actions, *, first_seq):
            orm_models.PennantDraftAction.objects.create(
                draft=draft, seq=first_seq, team_id=self.team_ids[1], kind=DraftActionKind.PASS.value
            )
            original(row, actions, first_seq=first_seq)

        with (
            patch.object(DjangoDraftRepository, "_write_actions", staticmethod(race)),
            self.assertRaises(DraftConflict),
        ):
            self.repository().append(YEAR, self.actions()[:2], expected_seq=0)
        self.assertFalse(
            orm_models.PennantDraftAction.objects.filter(kind=DraftActionKind.BID.value).exists(),
            "こちらの行は書かない",
        )

    def test_actions_of_clubs_outside_the_meeting_are_refused(self):
        self.repository().start(record(self.team_ids))
        with self.assertRaises(InvalidDraftAction):
            self.repository().append(YEAR, [DraftAction(DraftActionKind.PASS, self.other_team_ids[0])], expected_seq=0)

    def test_meetings_stay_in_their_world(self):
        self.repository().start(record(self.team_ids, self.actions()))

        self.assertIsNone(self.repository(self.other_id).find(YEAR))
        self.assertIsNone(self.repository(self.other_id).last_seq(YEAR))
        with self.assertRaises(DraftConflict):
            self.repository(self.other_id).append(YEAR, [], expected_seq=4)
        self.repository(self.other_id).remove(YEAR)
        self.assertIsNotNone(self.repository().find(YEAR), "別の世界から消せない")
        with self.assertRaises(TeamNotFound):
            self.repository(self.other_id).start(record(self.team_ids))

    def test_the_real_data_has_no_meeting(self):
        real = DjangoDraftRepository(WorldScope.real())
        self.repository().start(record(self.team_ids))
        self.assertIsNone(real.find(YEAR))
        self.assertIsNone(real.last_seq(YEAR))
        with self.assertRaises(InvalidWorld):
            real.start(record(self.team_ids))

    def test_remove_clears_the_meeting_and_its_rows(self):
        self.repository().start(record(self.team_ids, self.actions()))
        self.repository().remove(YEAR)
        self.repository().remove(YEAR)  # 無ければ何もしない

        self.assertIsNone(self.repository().find(YEAR))
        self.assertFalse(orm_models.PennantDraftSlot.objects.exists())
        self.assertFalse(orm_models.PennantDraftAction.objects.exists())

    def test_deleting_the_world_deletes_its_meeting_only(self):
        self.repository().start(record(self.team_ids, self.actions()))
        other = DraftRecord(year=YEAR, signature="x", slots=(slot(self.other_team_ids[0]),), actions=())
        self.repository(self.other_id).start(other)

        DjangoWorldRepository().delete(self.world_id)

        self.assertEqual(list(orm_models.PennantDraft.objects.values_list("world_id", flat=True)), [self.other_id])
        self.assertEqual(orm_models.PennantDraftSlot.objects.count(), 1)
        self.assertEqual(self.repository(self.other_id).find(YEAR), other)


class PlayerNameQueryTest(WorldCase):
    def test_the_names_are_those_of_every_player_in_the_world(self):
        """締める処理が集約から作る名前（`team.players`）と同じ集まりを、集約を組み立てずに読む。"""
        teams = DjangoTeamRepository(self.scope).find_all_with_roster()
        expected = frozenset(player.name for team in teams for player in team.players)

        names = DjangoPlayerNameQuery(self.scope).names()

        self.assertTrue(names)
        self.assertEqual(names, expected)
        real = DjangoPlayerNameQuery(WorldScope.real()).names()
        self.assertFalse(names & real, "世界の選手の名前は目印つきで、実データと重ならない")
