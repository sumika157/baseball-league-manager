"""エンジンの延長の上限（`simulate_game` の `max_innings`）の単体テスト。Django も DB も使わない。

守りたいこと:
- 既定値（公式戦の12回）のままなら、引数を足す前と**ビット単位で同じ**打席列になる（既存の世界の再現性）
- `None`（日本シリーズの第8戦以降）なら、12回で引分だった試合が延長で決着する
- 点の入らない乱数源でも無限に回らない。9回未満の上限は受け付けない
"""

import hashlib
from datetime import date, timedelta
from unittest import TestCase

from myapp.domain.exceptions import InvalidGame
from myapp.domain.simulation.engine import MAX_INNINGS, SimulatedGame, simulate_game
from myapp.domain.simulation.fatigue import PitchingHistory
from myapp.domain.simulation.manager import choose_active_roster
from myapp.domain.simulation.randomness import game_seed, make_random
from myapp.domain.simulation.samples import average_club, round_robin_days, spread_league
from myapp.domain.value_objects import GameStage

OPENING_DAY = date(2026, 4, 1)
# `max_innings` を足す前のエンジン（S1 のコミット 346ca65）で、下の `_season_fingerprint()` を取った値。
# 公式戦の再現性（同じシードの世界が同じ試合を作る）を守るため、既定値の経路で打席列が1つも変わらないことを見る。
# エンジンの振る舞いを意図して変えたときだけ、この値を取り直す
FINGERPRINT_BEFORE_MAX_INNINGS = "8cd61263f94bd1cca9b7107dac46556ccfd0b381719f45d440f542d5e68a96b2"


class ScriptedRandom:
    """決めた列を繰り返す乱数源。"""

    def __init__(self, *values: float) -> None:
        self.values = values
        self.index = 0

    def random(self) -> float:
        value = self.values[self.index % len(self.values)]
        self.index += 1
        return value


def _season_fingerprint(**kwargs) -> tuple[str, int]:
    """能力を散らした6球団で28日回した試合の打席列のハッシュと、引分の数（引分が無いと延長の検査が空振りする）。"""
    rng = make_random(20260801)
    rosters = {club.team_id: choose_active_roster(club, 4) for club in spread_league(rng, 6)}
    history = PitchingHistory()
    digest = hashlib.sha256()
    ties = 0
    for day_index, day in enumerate(round_robin_days(list(rosters), 28)):
        played_on = OPENING_DAY + timedelta(days=day_index)
        for home_id, away_id in day:
            played = simulate_game(
                make_random(game_seed(1, 2026, f"{day_index}-{home_id}")),
                rosters[home_id],
                rosters[away_id],
                stage=GameStage.REGULAR,
                played_on=played_on,
                history=history,
                **kwargs,
            )
            ties += played.game.is_tie
            plate_appearances = [
                (p.sequence, p.inning, p.is_bottom, p.batter_id, p.pitcher_id, p.result.name, p.runs_scored)
                for p in played.plate_appearances
            ]
            line = played.game.line_score
            digest.update(repr((plate_appearances, line.away, line.home)).encode())
    return digest.hexdigest(), ties


def _play(seed: int, **kwargs) -> SimulatedGame:
    home = choose_active_roster(average_club(1))
    away = choose_active_roster(average_club(2))
    return simulate_game(make_random(seed), home, away, stage=GameStage.REGULAR, played_on=OPENING_DAY, **kwargs)


def _first_tied_seed() -> int:
    for seed in range(500):
        if _play(seed).game.is_tie:
            return seed
    raise AssertionError("500 シードで引分が1試合も無い（この検査が空振りしている）")


class DefaultMaxInningsTest(TestCase):
    def test_the_default_gives_the_same_plate_appearances_as_before_the_argument(self):
        fingerprint, ties = _season_fingerprint()

        self.assertEqual(FINGERPRINT_BEFORE_MAX_INNINGS, fingerprint)
        self.assertGreater(ties, 0)

    def test_passing_twelve_is_the_same_as_the_default(self):
        seed = _first_tied_seed()

        self.assertEqual(
            _play(seed).plate_appearances,
            _play(seed, max_innings=MAX_INNINGS).plate_appearances,
        )


class UnlimitedInningsTest(TestCase):
    def test_a_game_tied_after_twelve_goes_on_until_it_is_decided(self):
        seed = _first_tied_seed()
        tied = _play(seed)
        decided = _play(seed, max_innings=None)

        self.assertEqual(MAX_INNINGS, tied.game.line_score.innings)
        self.assertFalse(decided.game.is_tie)
        self.assertGreater(decided.game.line_score.innings, MAX_INNINGS)
        # 12回までは同じ試合（乱数の使い方は上限で変わらない）
        self.assertEqual(
            tied.plate_appearances,
            decided.plate_appearances[: len(tied.plate_appearances)],
        )
        decided.game.ensure_plate_appearances_consistent()
        decided.game.ensure_line_score_matches()

    def test_a_shorter_limit_ends_the_game_earlier(self):
        # 12回を終えて同点の試合は、10回を終えても同点だった（決着していれば10回で終わっていた）
        game = _play(_first_tied_seed(), max_innings=10).game

        self.assertTrue(game.is_tie)
        self.assertEqual(10, game.line_score.innings)

    def test_a_scoreless_random_source_cannot_loop_forever(self):
        home = choose_active_roster(average_club(1))
        away = choose_active_roster(average_club(2))

        with self.assertRaises(InvalidGame):
            simulate_game(
                ScriptedRandom(0.99), home, away, stage=GameStage.REGULAR, played_on=OPENING_DAY, max_innings=None
            )

    def test_a_limit_below_nine_innings_is_rejected(self):
        with self.assertRaises(ValueError):
            _play(1, max_innings=8)

    def test_a_limit_above_the_safety_cap_is_rejected(self):
        # 安全装置（50回）より大きい上限は、途中で InvalidGame になる前に入口で弾く。上限なしは None
        _play(1, max_innings=50)
        with self.assertRaises(ValueError):
            _play(1, max_innings=51)
