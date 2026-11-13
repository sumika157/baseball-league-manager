"""見本のリーグの固定の名前（リーグ・チーム・球場）。**実在の球団名・球場名・地名は使わない**（すべて架空）。

球場の属性の選択肢（グラウンドの種類・屋根）は `StadiumProfile.SURFACES`・`StadiumProfile.ROOFS` が唯一の出典で、
ここでは添字ではなくその値を指す（値を書き写さない）。
"""

from __future__ import annotations

from typing import NamedTuple

from ..value_objects import StadiumProfile

_NATURAL, _ARTIFICIAL, _ = StadiumProfile.SURFACES
_OPEN_AIR, _DOME, _RETRACTABLE = StadiumProfile.ROOFS

SAMPLE_LEAGUE_NAME = "見本リーグ"


class SampleClubName(NamedTuple):
    """1球団の名前と、その本拠地の球場の名前と属性。"""

    team: str
    stadium: str
    city: str
    capacity: int
    surface: str
    roof: str
    opened_year: int


# 並びがチームの表示順。6球団（`league.SAMPLE_TEAM_COUNT`）
SAMPLE_CLUBS: tuple[SampleClubName, ...] = (
    SampleClubName("北浜シーガルズ", "北浜臨海球場", "北浜市", 30000, _NATURAL, _OPEN_AIR, 1978),
    SampleClubName("杉ノ森フォクシーズ", "杉ノ森総合公園野球場", "杉ノ森町", 24000, _NATURAL, _OPEN_AIR, 1992),
    SampleClubName("白峰スノーベアーズ", "白峰ドーム", "白峰市", 38000, _ARTIFICIAL, _DOME, 2001),
    SampleClubName("桜川ファルコンズ", "桜川スタジアム", "桜川市", 32000, _ARTIFICIAL, _OPEN_AIR, 1985),
    SampleClubName("朝凪ペリカンズ", "朝凪マリンフィールド", "朝凪市", 27000, _NATURAL, _OPEN_AIR, 1996),
    SampleClubName("青葉台ブルーウィングス", "青葉台ドームパーク", "青葉台市", 35000, _ARTIFICIAL, _RETRACTABLE, 2009),
)
