"""仮想の選手の名前とプロフィールの生成。**Django を import しない純粋な関数**。

`seed_virtual_players`（実データへの投入）とペナントのオフ（自動ドラフトの新人）が同じ関数を通る。
乱数は呼び出し側が渡す（`GameRandom`）。**`rng.random()` だけを使う**（版をまたいで同じ列を保証するのは
`random()` だけ。`choice` / `randint` / `gauss` は使わない。`simulation/randomness.py`）ので、
同じ乱数源なら同じ名前になる。

氏名は「苗字＋名前」をプールから組み合わせて作るため、よみがな（カタカナ）と背ネーム
（ユニフォーム背面のヘボン式アルファベット表記）もプール側に持たせてある。
"""

from __future__ import annotations

from collections.abc import Mapping, MutableSet, Sequence
from datetime import date
from enum import Enum
from typing import NamedTuple

from ..simulation.randomness import GameRandom, weighted_index
from ..value_objects import Handedness, Position
from . import pools

# MLBの40人ロースター構成をおおまかに参考にした比率。合計は 0.9875 で、端数は最大剰余法が配る。
POSITION_RATIOS: dict[Position, float] = {
    Position.PITCHER: 0.475,
    Position.CATCHER: 0.0625,
    Position.INFIELDER: 0.2375,
    Position.OUTFIELDER: 0.1875,
    Position.DESIGNATED_HITTER: 0.025,
}

# ポジションごとの体格レンジ（cm, kg）。MLB選手の体格傾向を参考にした目安。
PHYSIQUE_RANGES: dict[Position, tuple[tuple[int, int], tuple[int, int]]] = {
    Position.PITCHER: ((178, 196), (78, 98)),
    Position.CATCHER: ((172, 185), (75, 92)),
    Position.INFIELDER: ((170, 186), (68, 88)),
    Position.OUTFIELDER: ((175, 190), (72, 92)),
    Position.DESIGNATED_HITTER: ((178, 193), (82, 100)),
}

# 投入時に外国人にする割合
FOREIGN_PLAYER_RATIO = 0.12
# 1球団の人数の範囲（実運用に近い28〜40人）
MIN_ROSTER, MAX_ROSTER = 28, 40

# 珍しい苗字・名前を抽選する確率。プール自体は104種・88種と大きいが、
# 比率を高くすると「六月一日」のような超レア姓が「佐藤」など上位頻出姓と
# 大差ない頻度で現れてしまう（プールが大きいほど1件あたりの分母が薄まるため）。
# 0.10なら「姓名とも定番」が約81%、「どちらかが珍しい」が約18%、
# 「姓名とも珍しい」が約1%になる目安で、あくまで少数の彩りにとどめる。
RARE_NAME_RATIO = 0.10

# 私立高校は特定の地域と結び付けず、全国から選手が集まる架空校として扱う。
# NPBの実際の出身校は私立が多数派（強豪校の大半が私立）なので、比率も私立優位にしてある。
PRIVATE_HIGH_SCHOOL_RATIO = 0.65

# 外国人のプロ入りの年齢の範囲（投入の既定。自動ドラフトは `amateur_career` に別の範囲を渡す）
FOREIGN_DEBUT_AGE_RANGE = (19, 30)

# 名前の衝突を避けて引き直す回数。これでも衝突したら連番で確定させる
_NAME_ATTEMPTS = 50


class AmateurPath(Enum):
    """プロ入り前の経路（日本人）。値は画面に出す名前。"""

    HIGH_SCHOOL = "高校"
    UNIVERSITY = "大学"
    CORPORATE = "社会人"

    @property
    def weight(self) -> int:
        """日本人の新人がこの経路で入る比率（合計 100）。"""
        return _AMATEUR_PATH_WEIGHTS[self]


_AMATEUR_PATH_WEIGHTS = {AmateurPath.HIGH_SCHOOL: 30, AmateurPath.UNIVERSITY: 50, AmateurPath.CORPORATE: 20}


class JapaneseName(NamedTuple):
    """日本人の氏名。"""

    name: str
    kana: str
    surname_romaji: str
    given_romaji: str


class ForeignName(NamedTuple):
    """外国人の氏名。名前は「名・姓」のカタカナで、よみがなも同じ。"""

    name: str
    kana: str
    country: str
    surname_romaji: str
    given_romaji: str


class AmateurCareer(NamedTuple):
    """プロ入り前の経歴と、プロ入りの年齢。"""

    high_school: str
    university: str
    corporate_team: str
    debut_age: int
    # 経路。外国人は日本の経路を持たないので None
    path: AmateurPath | None = None


def pick[T](rng: GameRandom, items: Sequence[T]) -> T:
    """一様に1つ選ぶ。`random()` を1回だけ使う。"""
    return items[min(len(items) - 1, int(rng.random() * len(items)))]


def randint(rng: GameRandom, low: int, high: int) -> int:
    """low 以上 high 以下の整数。`random()` を1回だけ使う。"""
    return low + min(high - low, int(rng.random() * (high - low + 1)))


def largest_remainder[K](total: int, ratios: Mapping[K, float]) -> dict[K, int]:
    """比率にしたがって total を整数配分する（最大剰余法）。"""
    raw = {key: total * ratio for key, ratio in ratios.items()}
    floored = {key: int(value) for key, value in raw.items()}
    remainder = total - sum(floored.values())
    order = sorted(raw, key=lambda key: raw[key] - floored[key], reverse=True)
    for key in order[:remainder]:
        floored[key] += 1
    return floored


def _pick_surname(rng: GameRandom) -> tuple[str, str, str]:
    pool = pools.RARE_JP_SURNAMES if rng.random() < RARE_NAME_RATIO else pools.JP_SURNAMES
    return pick(rng, pool)


def _pick_given_name(rng: GameRandom) -> tuple[str, str, str]:
    pool = pools.RARE_JP_GIVEN_NAMES if rng.random() < RARE_NAME_RATIO else pools.JP_GIVEN_NAMES
    return pick(rng, pool)


def japanese_name(rng: GameRandom, used_names: MutableSet[str]) -> JapaneseName:
    """日本人の氏名を作り、`used_names` に足す（既にある名前は避ける）。"""
    for _ in range(_NAME_ATTEMPTS):
        surname_kanji, surname_kana, surname_romaji = _pick_surname(rng)
        given_kanji, given_kana, given_romaji = _pick_given_name(rng)
        name = surname_kanji + given_kanji
        if name not in used_names:
            used_names.add(name)
            return JapaneseName(name, surname_kana + given_kana, surname_romaji, given_romaji)
    # 衝突を続ける確率は極めて低いが、念のため連番で確定させる
    name = f"{surname_kanji}{given_kanji}{len(used_names)}"
    used_names.add(name)
    return JapaneseName(name, surname_kana + given_kana, surname_romaji, given_romaji)


def foreign_name(rng: GameRandom, used_names: MutableSet[str]) -> ForeignName:
    """外国人の氏名を作り、`used_names` に足す（既にある名前は避ける）。"""
    group = pick(rng, pools.FOREIGN_GROUPS)
    for _ in range(_NAME_ATTEMPTS):
        given_kana, given_romaji = pick(rng, group["given"])
        surname_kana, surname_romaji = pick(rng, group["surname"])
        name = f"{given_kana}・{surname_kana}"
        if name not in used_names:
            used_names.add(name)
            return ForeignName(name, name, group["country"], surname_romaji, given_romaji)
    name = f"{name}{len(used_names)}"
    used_names.add(name)
    return ForeignName(name, name, group["country"], surname_romaji, given_romaji)


def prefecture(rng: GameRandom) -> str:
    """日本人の出身地（都道府県）。"""
    return pick(rng, pools.JAPANESE_PREFECTURES)


def birth_date_for(rng: GameRandom, *, age: int, as_of: date) -> date:
    """`as_of` の時点で満 `age` 歳になる生年月日（月日は乱数）。"""
    month = randint(rng, 1, 12)
    day = randint(rng, 1, 28)
    had_birthday = (month, day) <= (as_of.month, as_of.day)
    birth_year = as_of.year - age if had_birthday else as_of.year - age - 1
    return date(birth_year, month, day)


def _public_prefecture_prefix(prefecture_name: str) -> str:
    """公立高校名の頭に付く「〜県立」。北海道だけ「立」を付けない実際の慣例に合わせる。"""
    return prefecture_name if prefecture_name == "北海道" else f"{prefecture_name}立"


def public_high_school(rng: GameRandom, prefecture_name: str) -> str:
    """出身都道府県の公立高校名。"""
    locale = pick(rng, pools.PREFECTURE_LOCALES.get(prefecture_name, []) + pools.GENERIC_LOCALES)
    return f"{_public_prefecture_prefix(prefecture_name)}{locale}{pick(rng, pools.PUBLIC_SCHOOL_KINDS)}"


def amateur_career(
    rng: GameRandom,
    *,
    is_foreign: bool,
    birthplace: str,
    path: AmateurPath | None = None,
    foreign_age_range: tuple[int, int] = FOREIGN_DEBUT_AGE_RANGE,
) -> AmateurCareer:
    """プロ入り前の経歴。

    公立高校は出身都道府県（birthplace）の高校にする。私立高校は全国から選手が集まる前提で、
    出身地とは結び付けない架空校のプールから選ぶ。`path` を省くと経路を `AmateurPath.weight` で決める。
    外国人は日本の学校を持たず、プロ入りの年齢は `foreign_age_range` から引く。
    """
    if is_foreign:
        return AmateurCareer("", "", "", randint(rng, *foreign_age_range))

    if rng.random() < PRIVATE_HIGH_SCHOOL_RATIO:
        high_school = pick(rng, pools.PRIVATE_HIGH_SCHOOLS)
    else:
        high_school = public_high_school(rng, birthplace)

    if path is None:
        paths = list(AmateurPath)
        path = paths[weighted_index(rng, [item.weight for item in paths])]
    if path is AmateurPath.HIGH_SCHOOL:
        return AmateurCareer(high_school, "", "", 18, path)
    if path is AmateurPath.UNIVERSITY:
        return AmateurCareer(high_school, pick(rng, pools.UNIVERSITIES), "", 22, path)
    return AmateurCareer(high_school, "", pick(rng, pools.CORPORATE_TEAMS), randint(rng, 23, 26), path)


def physique(rng: GameRandom, position: Position) -> tuple[int, int]:
    """(身長 cm, 体重 kg)。ポジションごとのレンジから引く。"""
    height_range, weight_range = PHYSIQUE_RANGES[position]
    return randint(rng, *height_range), randint(rng, *weight_range)


def handedness(rng: GameRandom, position: Position) -> tuple[Handedness, Handedness]:
    """(投, 打)。捕手は右投げが多い。"""
    right_ratio = 0.92 if position is Position.CATCHER else 0.75
    throws = Handedness.RIGHT if rng.random() < right_ratio else Handedness.LEFT
    hands = (Handedness.RIGHT, Handedness.LEFT, Handedness.BOTH)
    bats = hands[weighted_index(rng, [55, 30, 15])]
    return throws, bats


def back_name(surname_romaji: str, given_romaji: str, *, shares_surname: bool) -> str:
    """背ネーム。同じ球団に同じ苗字の選手がいるときは、名前の頭文字を付ける（例: 「K.SATO」）。"""
    if shares_surname and given_romaji:
        return f"{given_romaji[0]}.{surname_romaji}"
    return surname_romaji
