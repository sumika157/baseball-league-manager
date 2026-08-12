"""列挙がずれていないことの検査。

同じ事実を2か所に書かざるを得ない箇所を、機械的に突き合わせる。
ずれても例外にはならず「その項目だけ保存されない」「入力欄が出ない」「既定値だけが
古い」という静かな不具合になる。項目を増やすときに触る場所はこのテストの参照先がすべて。

**打席の語彙（結果・進塁の理由・失策の種類）は React に列挙していない。**
payload の vocabulary としてサーバーから届くので、突き合わせるものが無い
（払い出せば出典は1つのまま）。TypeScript に残っているのは塁の番号だけで、
それはここで突き合わせる。
"""

import pathlib
import re
from dataclasses import fields

from django.conf import settings
from django.test import SimpleTestCase

from myapp.domain.value_objects import Base, BattingLine, PitchingLine
from myapp.infrastructure.repositories import (
    _BATTING_FIELDS,
    _DERIVED_PITCHING_COUNTS,
    _PITCHING_COUNTS,
)

TYPES_TS = pathlib.Path(settings.BASE_DIR) / "frontend" / "src" / "game_edit" / "types.ts"


def _ts_fields(name: str) -> set[str]:
    """types.ts の `export const <name> = [...] as const;` から項目名を取り出す。"""
    text = TYPES_TS.read_text(encoding="utf-8")
    match = re.search(rf"export const {name} = \[(.*?)\] as const;", text, re.DOTALL)
    assert match is not None, f"{TYPES_TS.name} に {name} が見つかりません"
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def _ts_number(name: str) -> int:
    """types.ts の `export const <name> = <数値>;` を読む。"""
    text = TYPES_TS.read_text(encoding="utf-8")
    match = re.search(rf"export const {name} = (-?\d+);", text)
    assert match is not None, f"{TYPES_TS.name} に {name} が見つかりません"
    return int(match.group(1))


class BattingStatFieldsTest(SimpleTestCase):
    """打撃のカウント項目は、値オブジェクトと永続化で同じであること。

    **入力フォームの列挙は無くなった**（成績を手入力する画面が消え、打席から
    導くようになったため）。残る重複は値オブジェクト ↔ 永続化の2か所だけ。
    """

    def test_persistence_matches_the_line(self):
        domain = {f.name for f in fields(BattingLine)}
        self.assertEqual(domain, set(_BATTING_FIELDS), "永続化の列挙が BattingLine と違います")


class PitchingStatFieldsTest(SimpleTestCase):
    """投球は、打席から数える項目と継投から導く項目に分かれる。"""

    def test_persistence_covers_the_whole_line(self):
        domain = {f.name for f in fields(PitchingLine)}
        persisted = {"innings", *_PITCHING_COUNTS, *_DERIVED_PITCHING_COUNTS}
        self.assertEqual(domain, persisted, "永続化の列挙が PitchingLine と違います")


class BaseNumbersTest(SimpleTestCase):
    """塁の番号が Python と TypeScript で一致していること。

    **番号の大小がそのまま「進んだか」を表す**ので、ずれると画面の進塁の判定だけが
    静かに壊れる（表示名は payload から届くため、見た目では気づけない）。
    """

    EXPECTED = {
        "BASE_BATTER": Base.BATTER,
        "BASE_FIRST": Base.FIRST,
        "BASE_SECOND": Base.SECOND,
        "BASE_THIRD": Base.THIRD,
        "BASE_HOME": Base.HOME,
        "BASE_OUT": Base.OUT,
    }

    def test_typescript_matches_the_domain(self):
        for name, base in self.EXPECTED.items():
            with self.subTest(name=name):
                self.assertEqual(_ts_number(name), base.value, f"{name} が Base.{base.name} と違います")

    def test_every_base_is_covered(self):
        """ドメインに塁を足したら TypeScript にも足すこと。"""
        self.assertEqual(set(Base), set(self.EXPECTED.values()))
