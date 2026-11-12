"""domain のテストで使う乱数源の偽物（テストダブル）。

シミュレーションと仮想データの生成は `random()` だけを呼ぶ乱数源を受け取る。テストではその乱数源を差し替えて、
出目を決めたり（`ScriptedRandom`）、`random()` 以外を呼んでいないことと呼んだ回数を確かめたり（`OnlyRandom`）する。
同じものを各テストファイルに書き写すと、片方だけ直したときに食い違うので、ここにまとめる。
ファイル名が `test_` で始まらないので、テストとしては集められない。
"""

from __future__ import annotations

import random


class ScriptedRandom:
    """決めた列を繰り返す乱数源。"""

    def __init__(self, *values: float) -> None:
        self.values = values
        self.index = 0

    def random(self) -> float:
        value = self.values[self.index % len(self.values)]
        self.index += 1
        return value


class OnlyRandom:
    """`random()` しか持たない乱数源。呼ばれた回数を `calls` に数える。

    他のメソッド（`choice` など）を呼ぶと AttributeError で落ちる。生成が `random()` だけで決まる
    （版をまたいで同じ結果になる）ことを確かめるのに使う。
    """

    def __init__(self, seed: int) -> None:
        self._rng = random.Random(seed)
        self.calls = 0

    def random(self) -> float:
        self.calls += 1
        return self._rng.random()
