"""既にプロにいる選手（ベテラン）ひとりぶんの名前・プロフィール・加入の組み立て。
**Django を import しない純粋な関数**。

実データへの投入（`seed_virtual_players`）と、ワークスペースを作るときの見本のリーグ（`domain/sample/`）が
同じ関数を通る（W5 #81 決定3。規則の出典を1つにする）。新人（ドラフト会議の候補）は `pennant/draft.py` の
`draw_rookie` で、こちらは入団から何年か経った選手を作る。

規則:

- 年齢は契約区分の帯（支配下は `generator.REGISTERED_AGE`、育成は `generator.DEVELOPMENTAL_AGE`）の正規分布を、
  下限と上限に収めて丸める。
- 経歴は `generator.amateur_career`。育成の日本人は育成で指名できる経路
  （`DraftRoute.allows_development_pick`）からだけ引く。
- 加入年は生年 ＋ プロ入りの年齢（今の年齢を超えない）。日本人はその前年の秋のドラフト（支配下は本指名・育成は育成
  ドラフト）で指名され、加入年はその翌年（`DraftPick.entry_year`）、入団の経路は指名から導く
  （`DraftPick.acquisition_route`）。順位は呼ぶ側が渡す `draft_rounds`（球団 × ドラフト年 × 本指名/育成ごとの
  最後の順位）の次の番号。外国人はドラフトを持たず、経路は新外国人。

乱数は `rng.random()` だけを使う（`generator` と同じ。版をまたいで同じ列になる）。引く順は
名前 → 出身地 → 年齢 → 生年月日 → 経歴 → 投打 → 体格。背ネームは球団の中の同姓で決まるので、ここでは付けない
（`generator.back_name`）。
"""

from __future__ import annotations

from collections.abc import MutableMapping, MutableSet
from dataclasses import dataclass
from datetime import date

from ..pennant.draft import DraftRoute
from ..simulation.randomness import GameRandom, normal
from ..value_objects import AcquisitionRoute, ContractStatus, DraftPick, Position, Profile
from . import generator

# 経歴の経路（育成ドラフトの選手は、育成で指名できる経路だけ。規則の出典は `DraftRoute.allows_development_pick`）
ALL_PATHS = tuple(generator.AmateurPath)
DEVELOPMENT_PATHS = DraftRoute.development_amateur_paths()


@dataclass(frozen=True)
class GeneratedPlayer:
    """組み立てた選手ひとりぶん（保存しない値）。背番号と背ネームは球団の中で決まるので持たない。"""

    name: str
    position: Position
    contract: ContractStatus
    # 加入年（最初の在籍の年）
    from_year: int
    acquired_via: AcquisitionRoute
    # 背ネームは空（球団の中の同姓で決める）。外国人は `is_foreign_player` と国籍（出身国）を持つ
    profile: Profile
    surname_romaji: str
    given_romaji: str

    @property
    def is_foreign(self) -> bool:
        return self.profile.is_foreign_player


def draw_age(rng: GameRandom, contract: ContractStatus) -> int:
    """`as_of` の時点の年齢。契約区分の帯の正規分布を下限・上限に収めて丸める（`random()` を2回使う）。"""
    mean, sd, youngest, oldest = (
        generator.DEVELOPMENTAL_AGE if contract is ContractStatus.DEVELOPMENTAL else generator.REGISTERED_AGE
    )
    return max(youngest, min(oldest, round(normal(rng, mean, sd))))


def draw_veteran(
    rng: GameRandom,
    position: Position,
    *,
    contract: ContractStatus,
    is_foreign: bool,
    as_of: date,
    used_names: MutableSet[str],
    draft_rounds: MutableMapping[tuple[int, bool], int],
) -> GeneratedPlayer:
    """既にプロにいる選手ひとりぶん。`as_of` の時点の年齢で生年月日を決める。

    `used_names` は重複を避ける名前（作った名前を足す）。`draft_rounds` は（ドラフト年, 育成か）→ その球団の最後の
    順位で、日本人を作るたびに次の番号を振って書き足す（球団ごとに1つ渡す。重複しない）。
    外国人は支配下だけ（育成の外国人は作らない。呼ぶ側の規則）。
    """
    if is_foreign and contract is not ContractStatus.REGISTERED:
        raise ValueError("外国人の選手は支配下で作ります。")
    if is_foreign:
        foreign = generator.foreign_name(rng, used_names)
        name, kana, surname_romaji, given_romaji = (
            foreign.name,
            foreign.kana,
            foreign.surname_romaji,
            foreign.given_romaji,
        )
        birthplace = foreign.country
    else:
        domestic = generator.japanese_name(rng, used_names)
        name, kana = domestic.name, domestic.kana
        surname_romaji, given_romaji = domestic.surname_romaji, domestic.given_romaji
        birthplace = generator.prefecture(rng)

    age = draw_age(rng, contract)
    birth_date = generator.birth_date_for(rng, age=age, as_of=as_of)
    paths = DEVELOPMENT_PATHS if contract is ContractStatus.DEVELOPMENTAL else ALL_PATHS
    career = generator.amateur_career(rng, is_foreign=is_foreign, birthplace=birthplace, paths=paths)
    # プロ入りの年（加入年）。経歴のプロ入りの年齢で、今の年齢を超えない
    from_year = birth_date.year + min(career.debut_age, age)
    draft = None
    route = AcquisitionRoute.NEW_FOREIGN
    if not is_foreign:
        # 日本人はドラフトを経て入る。プロ入りの前年の秋に指名され、加入年はその翌年（`DraftPick.entry_year`）
        is_development = contract is ContractStatus.DEVELOPMENTAL
        draft_year = from_year - 1
        draft_rounds[(draft_year, is_development)] = draft_rounds.get((draft_year, is_development), 0) + 1
        draft = DraftPick(
            year=draft_year, round=draft_rounds[(draft_year, is_development)], is_development=is_development
        )
        from_year = draft.entry_year
        route = draft.acquisition_route
    route.ensure_fits_contract(contract)
    throws, bats = generator.handedness(rng, position)
    height_cm, weight_kg = generator.physique(rng, position)

    profile = Profile(
        birth_date=birth_date,
        throws=throws,
        bats=bats,
        height_cm=height_cm,
        weight_kg=weight_kg,
        birthplace=birthplace,
        draft=draft,
        high_school=career.high_school,
        university=career.university,
        corporate_team=career.corporate_team,
        nationality=birthplace if is_foreign else "",
        name_kana=kana,
        is_foreign_player=is_foreign,
    )
    return GeneratedPlayer(
        name=name,
        position=position,
        contract=contract,
        from_year=from_year,
        acquired_via=route,
        profile=profile,
        surname_romaji=surname_romaji,
        given_romaji=given_romaji,
    )
