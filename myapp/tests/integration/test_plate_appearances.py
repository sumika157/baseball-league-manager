"""打席の記録の永続化。ORM ⇄ ドメインの往復で経過が失われないことを確認する。

業務ルール（打順の巡回・塁の再生・打点の判定）は DB を使わない
`tests/domain/test_plate_appearances.py` にある。ここで見るのは保存と読み込みだけ。
"""

from datetime import date

from myapp.domain.entities import (
    FieldingError,
    Game,
    PlateAppearance,
    RunnerAdvance,
    RunnerSubstitution,
)
from myapp.domain.value_objects import (
    AdvanceReason,
    Base,
    ErrorKind,
    FieldingPosition,
    PlateAppearanceResult,
    Season,
)
from myapp.infrastructure import orm_models
from myapp.infrastructure.repositories import DjangoGameRepository

from .base import BaseCase

P = PlateAppearanceResult
R = AdvanceReason


def _to(runner_id, frm, to, reason=R.BATTED_BALL, error_index=None) -> RunnerAdvance:
    return RunnerAdvance(runner_id=runner_id, from_base=frm, to_base=to, reason=reason, error_index=error_index)


class PlateAppearancePersistenceTest(BaseCase):
    """1回表ぶんの記録を保存して読み直す。

    打順1〜6で、単打 → 単打（代走）→ 失策出塁（2人還らず1点）→ 三振 → ゴロアウト →
    フライアウト。進塁の理由・代走・失策・打球の処理経路が一通り登場する。
    """

    def setUp(self):
        super().setUp()
        self.repo = DjangoGameRepository()
        self.pitcher = self._register(self.team, "先発", 11, "投手")
        self.shortstop = self._register(self.team, "遊撃", 6, "内野手")
        self.batters = [self._register(self.rival, f"打者{i}", 20 + i, "内野手") for i in range(1, 7)]
        self.pinch_runner = self._register(self.rival, "代走", 40, "外野手")

    def _register(self, team, name, number, position) -> int:
        return self.service.register_player(team.id, name, number, position).id

    def _plate_appearances(self) -> list[PlateAppearance]:
        first, second, third, fourth, fifth, sixth = self.batters
        return [
            self._pa(1, 1, first, P.SINGLE, [_to(first, Base.BATTER, Base.FIRST)]),
            # 一塁の走者に代走を出してから、その代走が二塁へ進む
            self._pa(
                2,
                2,
                second,
                P.SINGLE,
                [
                    _to(self.pinch_runner, Base.FIRST, Base.SECOND),
                    _to(second, Base.BATTER, Base.FIRST),
                ],
                substitutions=[
                    RunnerSubstitution(
                        base=Base.FIRST,
                        leaving_runner_id=first,
                        entering_runner_id=self.pinch_runner,
                    )
                ],
            ),
            # 失策で1点。失策に起因する得点なので打点は付かない
            self._pa(
                3,
                3,
                third,
                P.REACHED_ON_ERROR,
                [
                    _to(self.pinch_runner, Base.SECOND, Base.HOME, R.ERROR, error_index=0),
                    _to(second, Base.FIRST, Base.THIRD, R.ERROR, error_index=0),
                    _to(third, Base.BATTER, Base.FIRST, R.ERROR, error_index=0),
                ],
                errors=[
                    FieldingError(
                        player_id=self.shortstop,
                        position=FieldingPosition.SHORTSTOP,
                        kind=ErrorKind.THROWING,
                    )
                ],
            ),
            self._pa(4, 4, fourth, P.STRIKEOUT_SWINGING, [_to(fourth, Base.BATTER, Base.OUT, R.PUT_OUT)]),
            self._pa(
                5,
                5,
                fifth,
                P.GROUND_OUT,
                [_to(fifth, Base.BATTER, Base.OUT, R.PUT_OUT)],
                fielded_by=(FieldingPosition.SHORTSTOP, FieldingPosition.FIRST_BASE),
            ),
            self._pa(6, 6, sixth, P.FLY_OUT, [_to(sixth, Base.BATTER, Base.OUT, R.PUT_OUT)]),
        ]

    def _pa(self, sequence, order, batter, result, advances, *, substitutions=(), errors=(), fielded_by=()):
        return PlateAppearance(
            sequence=sequence,
            inning=1,
            is_bottom=False,
            batter_id=batter,
            pitcher_id=self.pitcher,
            batting_order=order,
            result=result,
            fielded_by=fielded_by,
            advances=list(advances),
            substitutions=list(substitutions),
            errors=list(errors),
        )

    def _save_game(self) -> Game:
        game = Game(
            season=Season(2026),
            played_on=date(2026, 4, 1),
            home_team_id=self.team.id,
            away_team_id=self.rival.id,
            home_score=0,
            away_score=1,
            plate_appearances=self._plate_appearances(),
        )
        game.ensure_plate_appearances_consistent()
        return self.repo.save(game)

    def test_the_record_survives_the_round_trip(self):
        saved = self._save_game()

        reloaded = self.repo.find_by_id(saved.id)

        self.assertTrue(reloaded.plate_appearances_loaded)
        self.assertEqual([entry.sequence for entry in reloaded.plate_appearances_in_order()], [1, 2, 3, 4, 5, 6])
        # 読み直した記録だけでスコアブックとして成立していること
        reloaded.ensure_plate_appearances_consistent()
        self.assertEqual(reloaded.derived_line_score().away, (1,))

    def test_advances_keep_their_reason_and_order(self):
        """理由を失うと、失策で還った走者に打点が付いてしまう。"""
        saved = self._save_game()

        third_pa = self.repo.find_by_id(saved.id).plate_appearances_in_order()[2]

        self.assertEqual(
            third_pa.advances,
            [
                _to(self.pinch_runner, Base.SECOND, Base.HOME, R.ERROR, error_index=0),
                _to(self.batters[1], Base.FIRST, Base.THIRD, R.ERROR, error_index=0),
                _to(self.batters[2], Base.BATTER, Base.FIRST, R.ERROR, error_index=0),
            ],
        )
        self.assertEqual(third_pa.runs_scored, 1)
        self.assertEqual(third_pa.runs_batted_in, 0)

    def test_errors_substitutions_and_fielding_route_survive(self):
        saved = self._save_game()

        entries = self.repo.find_by_id(saved.id).plate_appearances_in_order()

        self.assertEqual(
            entries[1].substitutions,
            [
                RunnerSubstitution(
                    base=Base.FIRST, leaving_runner_id=self.batters[0], entering_runner_id=self.pinch_runner
                )
            ],
        )
        self.assertEqual(
            entries[2].errors,
            [FieldingError(player_id=self.shortstop, position=FieldingPosition.SHORTSTOP, kind=ErrorKind.THROWING)],
        )
        self.assertEqual(entries[4].fielded_by, (FieldingPosition.SHORTSTOP, FieldingPosition.FIRST_BASE))
        self.assertEqual(entries[0].fielded_by, ())

    def test_removed_plate_appearances_are_deleted(self):
        """記録を減らして保存したら、余った打席と、その進塁・失策も消えること。"""
        saved = self._save_game()

        trimmed = self.repo.find_by_id(saved.id)
        trimmed.plate_appearances = trimmed.plate_appearances_in_order()[:2]
        self.repo.save(trimmed)

        self.assertEqual(orm_models.GamePlateAppearance.objects.filter(game_id=saved.id).count(), 2)
        self.assertEqual(orm_models.GameFieldingError.objects.count(), 0)
        self.assertEqual(orm_models.GameRunnerAdvance.objects.count(), 3)

    def test_a_game_read_without_plate_appearances_does_not_erase_them(self):
        """一覧のために打席抜きで読んだ試合を保存しても、記録が消えないこと。

        「読み込んでいない」を「記録が無い」と同じに扱うと、順位表や集計のために
        読んだ試合を保存しただけで経過が全部消える。エラーにならないので気づけない。
        """
        saved = self._save_game()

        from_list = next(game for game in self.repo.find_by_team(self.team.id) if game.id == saved.id)
        self.assertFalse(from_list.plate_appearances_loaded)
        self.assertEqual(from_list.plate_appearances, [])
        from_list.home_score = 2
        self.repo.save(from_list)

        self.assertEqual(len(self.repo.find_by_id(saved.id).plate_appearances), 6)

    def test_bulk_reads_leave_plate_appearances_alone(self):
        """まとめて読む経路は打席を組み立てない（1試合で約280行あるため）。"""
        self._save_game()

        games = self.repo.find_all()

        self.assertTrue(games)
        for game in games:
            self.assertFalse(game.plate_appearances_loaded)
            self.assertEqual(game.plate_appearances, [])
