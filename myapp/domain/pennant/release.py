"""AI 球団の戦力外。締める処理の中で、引退の後・ドラフトの前に、球団ごとに外す選手を決める。
**純粋な関数**で、乱数も使わない（同じ材料なら必ず同じ選手を外す）。DB にも Django にも触れない。

    引退 → **戦力外** → 外国人の獲得 → ドラフト → 成長と衰え → 水準の錨

規則は2つ（2026-10-11・10-12 ユーザー決定。Issue #168）:

- **支配下の空き**（`release_counts`）: ふだんの新人（`draft.USUAL_DRAFTEES`）を迎えても天井（帯の上端か支配下の上限の
  低い方）を超えない人数まで外す。式は `OPENING_REGISTERED_BAND.excess` が出典（ドラフトの `gap` と同じ天井）。
  外すのは、その年の主力（`PlayingTime.is_regular`）と入団1年目を除いた支配下から、残す価値（`keep_value`）の低い順
- **外国人の入れ替え**（`is_replaceable_foreigner`）: 外国人は、能力で測れる価値にタイトルの加点だけを足した値
  （`foreign_value`。ほかの実績の加点は足さない。タイトルは #248）が**新しく迎える外国人に見込める価値**
  （`foreign_prospect_value`。獲得した外国人の分布から導く。`foreign_market.py`）を下回れば、主力でも外す
  （入団1年目は `FIRST_YEAR_FOREIGN_MARGIN` 点の余裕を持たせる）。
  全員が下回れば全員を入れ替える。外した外国人は外国人の獲得が同じオフに目標（`draft.foreign_target`）まで補う

どちらも育成は外さない（#170・#174）。位置の最低人数（`draft.MIN_BY_POSITION`）を割らず、候補が足りなければ
外せる分だけ（締める処理を失敗させない）。外国人を外した分も支配下の空きに数える。

残す価値（`keep_value`）は、**所属球団のスカウトの見立てで見た見込みの能力の総合値**（`ability_value`。
`outlook.roster_projection`。#308 P3b。2026-10-25 ユーザー決定 Q3 A。それまでの「若さの上乗せ」＝27歳より若い1歳に
一律1点を置き換えた）に**実績の加点**（`track_record_credit`。生え抜き・その球団での
在籍年数・世界の通算の実績・主将・タイトル。2026-10-12 ユーザー決定。タイトルは #248）を足したもの。
実績は外されにくくするだけで、試合には効かない。外国人の入れ替えの比較には、タイトルの加点だけを使う。

GM は受け持つ球団の戦力外を手動で指名できる（`ReleasePlan`。検査は `ensure_releasable`）。手動の球団は指名どおりに外し
（`releases_as_chosen`）、上の規則（自動）を使わない。AI 球団は常に自動。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from functools import cache

from ..exceptions import InvalidReleasePlan
from ..simulation.manager import LINEUP_SIZE
from ..simulation.ratings import POTENTIAL_AVERAGE, Potential
from ..value_objects import ForeignPlayerRules, Position
from .club_plan import ClubMember, lineup_capacity, playable_shortfall
from .draft import MIN_BY_POSITION, USUAL_DRAFTEES
from .foreign_market import expected_signing_ages, expected_signing_ratings
from .offseason_player import OffseasonClub, OffseasonPlayer, TrackRecord, player_age, retirement_value
from .outlook import projected_ratings, roster_projection
from .roster import OPENING_REGISTERED_BAND

# 入団1年目の外国人の余裕（点）。1年目だけ、新しく迎える外国人の見込みからこの値を引いて比べ、大きく下回るときだけ外す
# （2026-10-12 ユーザー決定。0 だと、見込みの平均の近くで入った1年目の外国人が成績と関係なく半分近く外れる。
# 値は実測で決めた。Wiki「ペナントモードの仕組み」の「戦力外」）
FIRST_YEAR_FOREIGN_MARGIN = 5.0

# 実績の加点（`track_record_credit`。2026-10-12 ユーザー決定）。値は実測で決めた（Wiki「ペナントモードの仕組み」の
# 「戦力外」）。
# 通算の実績は「主力の1シーズンぶん ≒ 1点」に揃える（野手は安打と本塁打、投手は勝利・セーブ・ホールド）
HITS_PER_POINT = 150
HOME_RUNS_PER_POINT = 50
WINS_PER_POINT = 10
SAVES_PER_POINT = 30
HOLDS_PER_POINT = 30
# 通算の実績1点あたりの加点
CAREER_CREDIT = 1.0
# その球団に在籍した1年あたりの加点
SEASON_CREDIT = 0.25
# 生え抜き・主将の加点
HOMEGROWN_CREDIT = 1.0
CAPTAIN_CREDIT = 2.0
# タイトル1つあたりの加点（#248）。通算の実績と同じ「主力の1シーズン ≒ 1点」の物差し。外国人の入れ替えにも効き、実測で
# タイトルのある外国人の戦力外が 24 → 10人（2点では 2人でほぼ外れなくなる）・外国人の戦力外全体は約4%減
# （Wiki「ペナントモードの仕組み」の「実測値」）
TITLE_CREDIT = 1.0
# 加点の上限（実績だけで、能力の衰えた選手をいつまでも残さない）
MAX_TRACK_RECORD_CREDIT = 6.0


@dataclass(frozen=True)
class ReleaseCounts:
    """球団が支配下の空きを作るために外したい人数（国籍を問わない）。"""

    registered: int


def release_counts(*, registered: int, registered_limit: int | None) -> ReleaseCounts:
    """支配下の空きを作るために外したい人数。**人数の式の出典はここだけ**。

    `registered` は引退の後の支配下の人数（育成は数えない）。`excess(registered, expected=USUAL_DRAFTEES)` 人。
    """
    return ReleaseCounts(
        registered=OPENING_REGISTERED_BAND.excess(registered, limit=registered_limit, expected=USUAL_DRAFTEES)
    )


def career_points(record: TrackRecord) -> float:
    """世界の通算の実績を「主力の1シーズンぶん ≒ 1点」に揃えた値。野手は安打と本塁打、投手は勝利・セーブ・ホールド。"""
    return (
        record.hits / HITS_PER_POINT
        + record.home_runs / HOME_RUNS_PER_POINT
        + record.wins / WINS_PER_POINT
        + record.saves / SAVES_PER_POINT
        + record.holds / HOLDS_PER_POINT
    )


def track_record_credit(record: TrackRecord) -> float:
    """実績の加点（点）。能力で測れない価値（2026-10-12 ユーザー決定）で、残す価値に足して外されにくくするだけ。

    通算の実績（`career_points`）・その球団での在籍年数・生え抜き・主将・タイトルの数の和で、
    `MAX_TRACK_RECORD_CREDIT` で頭打ち。
    """
    credit = (
        CAREER_CREDIT * career_points(record)
        + SEASON_CREDIT * record.seasons
        + (HOMEGROWN_CREDIT if record.homegrown else 0.0)
        + (CAPTAIN_CREDIT if record.is_captain else 0.0)
        + title_credit(record)
    )
    return min(MAX_TRACK_RECORD_CREDIT, credit)


def title_credit(record: TrackRecord) -> float:
    """タイトルの加点（点）。世界の中で取ったタイトルの数 × `TITLE_CREDIT`（頭打ちはしない。使う側が上限を当てる）。

    実績の加点（`track_record_credit`）の一部で、外国人の入れ替えの比較（`foreign_value`）にもこれだけを足す
    （2026-10-15 ユーザー決定。#248）。
    """
    return TITLE_CREDIT * record.titles


def ability_value(player: OffseasonPlayer, *, team_id: int, year: int, start_year: int, world_seed: int) -> float:
    """能力で測れる価値。球団 `team_id` のスカウトの見立てで見た**見込みの能力**（`outlook.roster_projection`）の
    総合値（`retirement_value`。捕手は守備力と打撃の大きい方）。`outlook.PEAK_AGE` 歳以上は今の能力の総合値。

    見込みの材料は画面の見込みの区分（`application/pennant_outlook.py`）と同じ: 締める年 `year` の能力・4月1日の年齢
    （`player_age`）・その球団での在籍年数（`TrackRecord.seasons`＝`club_seasons`）。本当の伸びしろは見ない
    （2026-10-25 ユーザー決定。AI も見立てを使う）。引退の確率（`offseason.decide_retirements`）もこれを使い、
    外国人の入れ替えは、これを新しく迎える外国人の見込み（`foreign_prospect_value`）と比べる。
    """
    if player.ratings is None:
        # 能力の無い選手は年齢によらず中立の値（見込みは能力から導くので、若手にも上乗せしない）
        return retirement_value(player.position, None)
    projected = roster_projection(
        player.ratings.ratings,
        age=player_age(player.profile, year, start_year=start_year),
        position=player.position,
        world_seed=world_seed,
        team_id=team_id,
        player_id=player.player_id,
        seasons=player.track_record.seasons,
    )
    return retirement_value(player.position, projected)


def keep_value(player: OffseasonPlayer, *, team_id: int, year: int, start_year: int, world_seed: int) -> float:
    """残す価値。能力で測れる価値（`ability_value`）に、実績の加点（`track_record_credit`）を足す。低いほど外される。"""
    ability = ability_value(player, team_id=team_id, year=year, start_year=start_year, world_seed=world_seed)
    return ability + track_record_credit(player.track_record)


def foreign_value(player: OffseasonPlayer, *, team_id: int, year: int, start_year: int, world_seed: int) -> float:
    """外国人の入れ替えで、新しく迎える外国人の見込みと比べる値。能力で測れる価値に、**タイトルの加点だけ**を足す
    （`MAX_TRACK_RECORD_CREDIT` で頭打ち）。

    通算成績・在籍年数・生え抜き・主将は足さない（足すと世界の中で活躍した外国人がほとんど入れ替わらなくなり、外国人の
    戦力外が約半分に減った。#168 段階B）。タイトルは足す（2026-10-15 ユーザー決定。#248。タイトルを取った外国人を
    能力の見込みだけで入れ替えない）。
    """
    credit = min(MAX_TRACK_RECORD_CREDIT, title_credit(player.track_record))
    ability = ability_value(player, team_id=team_id, year=year, start_year=start_year, world_seed=world_seed)
    return ability + credit


@cache
def foreign_prospect_value(position: Position) -> float:
    """新しく迎える外国人に見込める価値（`ability_value` と同じ物差し。新しく迎える外国人に実績は無い）。

    能力は獲得した外国人の平均（`foreign_market.expected_signing_ratings`。錨の前の尺度で、今の選手の能力と同じ尺度）を、
    候補の年齢の分布（`foreign_market.expected_signing_ages`。前所属ごとの範囲）の年齢ごとに見込みの能力
    （`outlook.projected_ratings`）にした総合値の平均。伸びしろは普通（`POTENTIAL_AVERAGE`。獲得した外国人の伸びしろの平均）。
    外国人は捕手を獲得しないので、捕手は内野手の見込みで比べる。
    材料は定数だけ（獲得の分布）なので位置ごとに覚える。獲得の分布を世界・球団ごとに変えるとき（#208）は引数に足すこと。
    """
    compared = Position.INFIELDER if position is Position.CATCHER else position
    ratings = expected_signing_ratings(compared)
    potential = Potential(POTENTIAL_AVERAGE)
    return sum(
        share
        * retirement_value(
            compared, projected_ratings(ratings, age=age, position=compared, potential_estimate=potential)
        )
        for age, share in expected_signing_ages().items()
    )


def is_release_candidate(player: OffseasonPlayer, *, year: int, start_year: int) -> bool:
    """支配下の空きのために AI が外しうる選手か。支配下で、その年の主力でなく、入団1年目（`is_first_year`）でない。"""
    return (
        player.is_registered
        and not player.playing_time.is_regular
        and not player.is_first_year(year, start_year=start_year)
    )


def is_replaceable_foreigner(
    player: OffseasonPlayer, *, team_id: int, year: int, start_year: int, world_seed: int
) -> bool:
    """新しい外国人と入れ替える外国人か。支配下の外国人で、比べる値（`foreign_value`。能力で測れる価値にタイトルの
    加点だけを足したもの）が新しく迎える外国人の見込みを下回る（主力も）。

    入団1年目の外国人は、見込みから `FIRST_YEAR_FOREIGN_MARGIN` を引いた値と比べる（大きく下回るときだけ外す）。
    タイトル以外の実績の加点は足さない（理由は `foreign_value`。実測は Wiki「ペナントモードの仕組み」の「戦力外」）。
    """
    if not (player.is_registered and player.profile.is_foreign_player):
        return False
    threshold = foreign_prospect_value(player.position)
    if player.is_first_year(year, start_year=start_year):
        threshold -= FIRST_YEAR_FOREIGN_MARGIN
    value = foreign_value(player, team_id=team_id, year=year, start_year=start_year, world_seed=world_seed)
    return value < threshold


def decide_releases(
    club: OffseasonClub, remaining: Sequence[OffseasonPlayer], *, year: int, start_year: int, world_seed: int
) -> tuple[int, ...]:
    """球団 `club` の戦力外の選手の id（外す順）。`remaining` は引退しなかった選手。

    先に入れ替える外国人、続けて支配下の候補から `ReleaseCounts.registered` 人に届くまで。
    どちらも残す価値の昇順（同値は選手 id の昇順。見込みは球団 `club` のスカウトの見立て）で、
    その位置の支配下が `MIN_BY_POSITION` 以下なら飛ばす。
    """
    counts = release_counts(
        registered=sum(1 for player in remaining if player.is_registered),
        registered_limit=club.registered_player_limit,
    )
    by_position = Counter(player.position for player in remaining if player.is_registered)

    def order(players: Sequence[OffseasonPlayer]) -> list[OffseasonPlayer]:
        return sorted(
            players,
            key=lambda player: (
                keep_value(player, team_id=club.team_id, year=year, start_year=start_year, world_seed=world_seed),
                player.player_id,
            ),
        )

    chosen: list[int] = []

    def take(pool: Sequence[OffseasonPlayer], until: int) -> None:
        for player in pool:
            if len(chosen) >= until:
                return
            if player.player_id in chosen or by_position[player.position] <= MIN_BY_POSITION.get(player.position, 0):
                continue
            by_position[player.position] -= 1
            chosen.append(player.player_id)

    foreigners = order(
        [
            player
            for player in remaining
            if is_replaceable_foreigner(
                player, team_id=club.team_id, year=year, start_year=start_year, world_seed=world_seed
            )
        ]
    )
    take(foreigners, len(foreigners))
    if counts.registered > len(chosen):
        take(
            order([p for p in remaining if is_release_candidate(p, year=year, start_year=start_year)]),
            counts.registered,
        )
    return tuple(chosen)


# --- GM の指名（受け持つ球団だけ） ---


@dataclass(frozen=True)
class ReleasePlan:
    """GM の戦力外の指名。球団ごとに1つで、締める年 `year` のオフに使う。

    `player_ids` が None なら自動（AI と同じ規則）、空なら手動で0人。`ClubPlan` と同じく GM の上書き（入力）だけを
    持ち、締めた時点で消す（戦力外の事実は在籍の退団の経路に1つだけ残す）。コンストラクタは保存した値をそのまま写すので
    検査しない（検査は決めるときの `ensure_releasable`）。
    """

    team_id: int
    year: int
    player_ids: frozenset[int] | None = None

    @property
    def is_manual(self) -> bool:
        return self.player_ids is not None


def ensure_releasable(
    player_ids: Sequence[int], registered: Sequence[ClubMember], *, foreign_rules: ForeignPlayerRules
) -> None:
    """戦力外の指名として成立するか。成立しなければ `InvalidReleasePlan`。

    - `registered`: 球団の、いま在籍している支配下の選手（育成は外せない。#170・#174）
    - `foreign_rules`: 世界のリーグで最も厳しい外国人枠（`ForeignPlayerRules.strictest`）

    指名できるのは支配下の在籍選手だけで、重複しない。外した後に、位置の最低人数（`draft.MIN_BY_POSITION`）を割らず、
    残る支配下で試合を組めること（`playable_shortfall`・外国人枠の中でスタメン9人）。
    主力・入団1年目も GM は外せる（AI が外さないのは自動の規則で、GM の判断は縛らない）。帯は見ない。
    """
    if len(set(player_ids)) != len(player_ids):
        raise InvalidReleasePlan("戦力外に同じ選手が重複しています。")
    by_id = {member.player_id: member for member in registered}
    unknown = [player_id for player_id in player_ids if player_id not in by_id]
    if unknown:
        raise InvalidReleasePlan(
            f"戦力外にできるのは、この球団に在籍している支配下の選手だけです（id={unknown[0]}）。"
        )
    leaving = set(player_ids)
    remaining = [member for member in registered if member.player_id not in leaving]
    before = Counter(member.position for member in registered)
    after = Counter(member.position for member in remaining)
    for position, minimum in MIN_BY_POSITION.items():
        if after[position] < before[position] and after[position] < minimum:
            raise InvalidReleasePlan(
                f"{position.value}は支配下に{minimum}人以上残す必要があります（外すと{after[position]}人）。"
            )
    shortfall = playable_shortfall((member.position for member in remaining), subject="残る支配下")
    if shortfall is not None:
        raise InvalidReleasePlan(shortfall)
    if lineup_capacity(remaining, foreign_rules) < LINEUP_SIZE:
        raise InvalidReleasePlan(
            f"外国人選手の枠（{foreign_rules.label}）の中では、残る支配下でスタメン{LINEUP_SIZE}人を組めません。"
            "外国人でない野手を残してください。"
        )


def releases_as_chosen(chosen: Collection[int], remaining: Sequence[OffseasonPlayer]) -> tuple[int, ...]:
    """手動の指名から、締めるときに外す選手（id の昇順）。`remaining` は引退しなかった選手。

    指名した後に引退した選手（引退として記録する）・支配下でなくなった選手は黙って除く。帯は見ない。
    """
    registered = {player.player_id for player in remaining if player.is_registered}
    return tuple(sorted(registered.intersection(chosen)))
