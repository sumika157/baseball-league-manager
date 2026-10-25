"""能力の値の `LABELS` に無いフィールド（成長型・伸びしろ）は、年を進めても錨をかけても変わらない。DB 不要。

能力を作り直す処理（`BatterRatings(...)` を項目だけで組み立て直す）は、フィールドを足すと黙って既定値に戻す
（例外にならず、その値だけが毎年消える静かな不具合になる。#308）。項目だけを差し替える処理（`replace`）にしてあることを、
フィールドを列挙して確かめる。**隠しフィールドを足したら `HIDDEN_SAMPLES` に既定値でない値を足す**
（足し忘れはテストが落ちる）。
"""

from __future__ import annotations

import random
import unittest
from dataclasses import fields, replace
from typing import Any

from myapp.domain.pennant.aging import age_ratings
from myapp.domain.pennant.offseason import anchor_stamina
from myapp.domain.simulation.ratings import BatterRatings, GrowthType, PitcherRatings, Potential
from myapp.domain.simulation.spread import MIN_POPULATION, spread_ratings

Ratings = BatterRatings | PitcherRatings

# 隠しフィールドごとの、既定値でない値（選手ごとに変えるので2つ）
HIDDEN_SAMPLES: dict[str, tuple[Any, Any]] = {
    "growth": (GrowthType.EARLY, GrowthType.LATE),
    "potential": (Potential(17), Potential(88)),
}


def hidden_names(kind: type[BatterRatings] | type[PitcherRatings]) -> set[str]:
    return {field.name for field in fields(kind)} - set(kind.LABELS)


def population() -> list[Ratings]:
    """野手と投手が交互に並び、隠しフィールドが選手ごとに違う母集団（錨がかかる人数）。"""
    rng = random.Random(3)
    result: list[Ratings] = []
    for index in range(2 * (MIN_POPULATION + 5)):
        hidden = {name: values[index % 2] for name, values in HIDDEN_SAMPLES.items()}
        if index % 2:
            result.append(
                PitcherRatings(*(rng.randint(20, 80) for _ in PitcherRatings.LABELS), **hidden)  # type: ignore[arg-type]
            )
        else:
            result.append(
                BatterRatings(*(rng.randint(20, 80) for _ in BatterRatings.LABELS), **hidden)  # type: ignore[arg-type]
            )
    return result


def hidden_values(ratings: Ratings) -> dict[str, Any]:
    return {name: getattr(ratings, name) for name in hidden_names(type(ratings))}


class HiddenFieldsTest(unittest.TestCase):
    def test_every_hidden_field_has_a_sample(self) -> None:
        """隠しフィールドを足したら、ここに既定値でない値を足す（下のテストが新しいフィールドも見るように）。"""
        for kind in (BatterRatings, PitcherRatings):
            with self.subTest(kind=kind.__name__):
                self.assertEqual(hidden_names(kind), set(HIDDEN_SAMPLES))
                defaults = kind()
                for name, values in HIDDEN_SAMPLES.items():
                    self.assertNotIn(getattr(defaults, name), values, "既定値と同じ値では、戻ったことに気づけない")

    def test_aging_keeps_the_hidden_fields(self) -> None:
        for ratings in population()[:4]:
            for age in (18, 27, 38):
                aged = age_ratings(ratings, age=age, rng=random.Random(age))
                self.assertEqual(hidden_values(aged), hidden_values(ratings))

    def test_the_anchor_keeps_the_hidden_fields(self) -> None:
        before = population()
        after = spread_ratings(before)
        self.assertNotEqual(after, before, "錨がかかって項目が動く人数にしてある")
        self.assertEqual([hidden_values(item) for item in after], [hidden_values(item) for item in before])

    def test_the_stamina_anchor_keeps_the_hidden_fields(self) -> None:
        before = population()
        after = anchor_stamina(before, (70.0, 3.0))
        self.assertNotEqual(after, before)
        self.assertEqual([hidden_values(item) for item in after], [hidden_values(item) for item in before])

    def test_only_the_labelled_items_move(self) -> None:
        """作り直しと `replace` の違いは隠しフィールドだけに出る。項目を戻せば元と同じ値になる。"""
        before = population()
        for original, moved in zip(before, spread_ratings(before), strict=True):
            items = {name: getattr(original, name) for name in original.LABELS}
            self.assertEqual(replace(moved, **items), original)


if __name__ == "__main__":
    unittest.main()
