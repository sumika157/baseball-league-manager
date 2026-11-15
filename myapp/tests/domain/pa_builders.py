"""打席の記録のテストで使う組み立て（打席・走者の進塁・試合）。

`test_plate_appearances`・`test_pa_derivation`・`test_run_responsibility`・`test_third_out` が使う。
"""

from datetime import date

from myapp.domain.entities import Game, PlateAppearance, RunnerAdvance
from myapp.domain.value_objects import AdvanceReason, Base, GameStage, PlateAppearanceResult, Season

P = PlateAppearanceResult
R = AdvanceReason

HOME, AWAY = 1, 2
# ホームの投手（ビジターの攻撃で投げる）と、交代で出てくる2人目
STARTER, RELIEVER = 100, 101


def _to(runner_id, frm, to, reason=R.BATTED_BALL, error_index=None) -> RunnerAdvance:
    return RunnerAdvance(runner_id=runner_id, from_base=frm, to_base=to, reason=reason, error_index=error_index)


def _pa(
    sequence,
    order,
    result,
    *,
    inning=1,
    bottom=False,
    batter=None,
    pitcher=STARTER,
    advances=None,
    errors=(),
    substitutions=(),
) -> PlateAppearance:
    """打席を1つ作る。進塁を省いたら打者の既定の到達塁だけを埋める。

    打者の id は既定で打順と同じにし、走者を追う筋が読めるようにしている。
    """
    batter_id = order if batter is None else batter
    if advances is None:
        advances = [_to(batter_id, Base.BATTER, result.default_batter_base, result.default_batter_reason)]
    return PlateAppearance(
        sequence=sequence,
        inning=inning,
        is_bottom=bottom,
        batter_id=batter_id,
        pitcher_id=pitcher,
        batting_order=order,
        result=result,
        advances=list(advances),
        errors=list(errors),
        substitutions=list(substitutions),
    )


def _game(plate_appearances, *, home_score=0, away_score=0) -> Game:
    return Game(
        season=Season(2026),
        played_on=date(2026, 4, 1),
        home_team_id=HOME,
        away_team_id=AWAY,
        stage=GameStage.REGULAR,
        home_score=home_score,
        away_score=away_score,
        plate_appearances=list(plate_appearances),
    )


def _half_inning():
    """1回表ぶんの、成立する記録。

    単打 → 二塁打（一塁走者が三塁へ）→ 犠飛（三塁走者が還る）→ 三振 → ゴロアウト。
    1点、3アウト、二塁に残塁1。
    """
    return [
        _pa(1, 1, P.SINGLE),
        _pa(
            2,
            2,
            P.DOUBLE,
            advances=[_to(1, Base.FIRST, Base.THIRD), _to(2, Base.BATTER, Base.SECOND)],
        ),
        _pa(
            3,
            3,
            P.SACRIFICE_FLY,
            advances=[_to(3, Base.BATTER, Base.OUT, R.PUT_OUT), _to(1, Base.THIRD, Base.HOME, R.TAG_UP)],
        ),
        _pa(4, 4, P.STRIKEOUT_SWINGING),
        _pa(5, 5, P.GROUND_OUT),
    ]
