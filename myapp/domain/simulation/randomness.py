"""乱数の扱い。シミュレーションは乱数源をここで定めた Protocol 越しにだけ使う。

**`random()` だけを呼ぶ。** Python の公式ドキュメントが、版をまたいでも同じ列を保証して
いるのは `random.Random.random()` だけ（`gauss` や `choice` は版で列が変わりうる）。
正規分布や重み付きの選択は、この `random()` の上に自前で書く。

試合ごとのシードは blake2b から作る。組み込みの `hash()` は `PYTHONHASHSEED` で実行ごとに
変わるので使えない。
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Sequence
from typing import Protocol


class GameRandom(Protocol):
    """乱数源。0以上1未満の実数を返す `random()` だけを要求する。

    テストでは固定の列を返す実装を差し込める。
    """

    def random(self) -> float: ...


def game_seed(world_seed: int, year: int, key: str) -> int:
    """世界のシード・年・試合の識別から、その試合だけのシードを作る。

    同じ引数なら、どの環境・どの実行でも同じ値になる。「1日ずつ進める」と「1週間まとめて
    進める」で同じ結果になるのは、試合の乱数が進めた順序に依存しないため。
    """
    digest = hashlib.blake2b(f"{world_seed}:{year}:{key}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def game_uniform(world_seed: int, year: int, key: str) -> float:
    """世界のシード・年・識別から、その用途だけの 0 以上 1 未満の実数を1つ作る。

    `random()` を1回だけ使う判定（引退するか、など）を、乱数源を作らずに引く（選手ごとに乱数源を作る
    と、数万人 × 数年で時間の大半を食う）。`game_seed` と同じく、どの環境・どの実行でも同じ値になる。
    """
    return (game_seed(world_seed, year, key) >> 11) / 2.0**53  # 上位 53 ビット。1.0 にはならない


def make_random(seed: int) -> random.Random:
    """シードから乱数源を作る。整数のシードは版をまたいで同じ列になる。"""
    return random.Random(seed)


def normal(rng: GameRandom, mean: float = 0.0, sd: float = 1.0) -> float:
    """正規分布の乱数（Box–Muller 法）。`random()` を2回だけ使う。"""
    u1 = 1.0 - rng.random()  # (0, 1] にして log(0) を避ける
    u2 = rng.random()
    return mean + sd * math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


def weighted_index(rng: GameRandom, weights: Sequence[float]) -> int:
    """重みに比例した添字を返す。`random()` を1回だけ使う。重みの合計が0なら先頭。"""
    total = sum(weights)
    if total <= 0:
        return 0
    threshold = rng.random() * total
    running = 0.0
    for index, weight in enumerate(weights):
        running += weight
        if threshold < running:
            return index
    return len(weights) - 1
