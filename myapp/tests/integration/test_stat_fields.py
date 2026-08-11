"""成績項目の列挙がずれていないことの検査。

打撃・投球のカウント項目は、値オブジェクト（ドメイン）だけでなく
永続化・フォーム・React の3か所にも列挙されている。Python から TypeScript を
import できないため、この重複は消せない。

ずれても例外にはならず「その項目だけ保存されない」「入力欄が出ない」という
静かな不具合になるため、機械的に突き合わせる。項目を増やすときに触る場所は
このテストの参照先がすべて。
"""

import pathlib
import re
from dataclasses import fields

from django.conf import settings
from django.test import SimpleTestCase

from myapp.domain.value_objects import BattingLine, PitchingLine
from myapp.infrastructure.repositories import (
    _BATTING_FIELDS,
    _DERIVED_PITCHING_COUNTS,
    _PITCHING_COUNTS,
)
from myapp.presentation.forms import BattingEntryForm, PitchingEntryForm

TYPES_TS = pathlib.Path(settings.BASE_DIR) / "frontend" / "src" / "game_edit" / "types.ts"


def _ts_fields(name: str) -> set[str]:
    """types.ts の `export const <name> = [...] as const;` から項目名を取り出す。"""
    text = TYPES_TS.read_text(encoding="utf-8")
    match = re.search(rf"export const {name} = \[(.*?)\] as const;", text, re.DOTALL)
    assert match is not None, f"{TYPES_TS.name} に {name} が見つかりません"
    return set(re.findall(r'"([^"]+)"', match.group(1)))


class BattingStatFieldsTest(SimpleTestCase):
    """打撃のカウント項目は、どの層でも同じ9項目であること。"""

    def test_all_sources_agree(self):
        domain = {f.name for f in fields(BattingLine)}
        self.assertEqual(domain, set(_BATTING_FIELDS), "永続化の列挙が BattingLine と違います")
        self.assertEqual(domain, set(BattingEntryForm.STAT_FIELDS), "入力フォームの列挙が BattingLine と違います")
        self.assertEqual(domain, _ts_fields("BATTING_STAT_FIELDS"), "React の列挙が BattingLine と違います")


class PitchingStatFieldsTest(SimpleTestCase):
    """投球は、手で入力する項目と導出する項目に分かれる。

    勝敗・セーブ・ホールドは継投から導くため入力欄を持たない
    （フォームと React の列挙が永続化より少ないのは、そのぶん）。
    """

    def test_persistence_covers_the_whole_line(self):
        domain = {f.name for f in fields(PitchingLine)}
        persisted = {"innings", *_PITCHING_COUNTS, *_DERIVED_PITCHING_COUNTS}
        self.assertEqual(domain, persisted, "永続化の列挙が PitchingLine と違います")

    def test_form_and_react_agree(self):
        form = set(PitchingEntryForm.COUNT_FIELDS)
        self.assertEqual(form, _ts_fields("PITCHING_COUNT_FIELDS"), "React の列挙が入力フォームと違います")

    def test_entered_fields_are_persisted(self):
        """入力できる項目が、保存される項目に含まれていること。"""
        self.assertLessEqual(set(PitchingEntryForm.COUNT_FIELDS), set(_PITCHING_COUNTS))
