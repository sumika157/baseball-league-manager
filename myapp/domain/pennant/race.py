"""優勝争い（優勝の確定とマジック）。順位と残り試合から導き、保存しない。

順位の規則（勝率 = 勝 ÷ (勝 + 敗)、引分は分母に入れない。同じ勝率は同順位）は
`domain/services/records.py` の `standings` と同じ。ここでは「このあと全球団がどう転んでも
自軍が単独で首位になる」かを、`TeamRecord.winning_percentage`（勝率の出典。勝敗0は0）の比較で判定する。

- 優勝の確定: 自軍が残りを全敗し、ほかの球団が全勝しても、自軍の勝率がどの球団よりも高い。
  勝率が並ぶ余地があれば確定にしない（安全側。同率で終わっても規定で必ず勝つと言える場合までは広げない）。
- マジック: 自軍の勝ちとある球団の負けの合計がいくつになれば、その球団を確実に上回るか。
  全球団ぶんの最大が、優勝までのマジック（0 なら確定）。直接対決の勝ちは自軍の勝ちと相手の負けの
  両方に数えられる（数が足りなくても進む）。途中の引分は数えない近似で、0（確定）の判定は厳密。
- 点灯: マジックが自軍の残り試合以内になったとき（自力で優勝を決められる範囲）。
- 同率の決着（`season_champion`）: シーズン終了時に勝率が同率で首位が並んだら、NPB の規定どおり
  (1) 当該球団間の対戦勝率（引分は数えない）→ (2) 前年の順位 → (3) 前年の順位も同じ・無い（開幕年）なら
  球団の並び順、の順で必ず1球団に決める。順位表の「同率は同順位」の表示は変えず、優勝の判定だけに使う。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from ..value_objects import TeamRecord


@dataclass(frozen=True)
class RaceTeam:
    """優勝争いに要る球団の材料。勝敗は消化した試合、`remaining` は未消化の対戦の数。"""

    team_id: int
    wins: int
    losses: int
    remaining: int


@dataclass(frozen=True)
class PennantRace:
    """ある球団の優勝争い。確定していれば `clinched`、点灯していれば `magic`（1 以上）。"""

    team_id: int
    clinched: bool
    magic: int | None


def _beats_at(team: RaceTeam, rival: RaceTeam, team_wins: int, rival_losses: int) -> bool:
    """自軍が残りのうち `team_wins` 勝（残りは負け）、相手が `rival_losses` 敗（残りは勝ち）なら、自軍の勝率が上か。"""
    mine = TeamRecord(wins=team.wins + team_wins, losses=team.losses + team.remaining - team_wins)
    theirs = TeamRecord(wins=rival.wins + rival.remaining - rival_losses, losses=rival.losses + rival_losses)
    return mine.winning_percentage > theirs.winning_percentage


def _magic_against(team: RaceTeam, rival: RaceTeam) -> int | None:
    """`rival` を確実に上回るのに要る「自軍の勝ち + 相手の負け」の数。上回れる余地が無ければ None。

    自軍が x 勝して残りは負け、相手が y 敗して残りは勝ち、として、合計 x + y が最小でいくつあれば
    どの内訳でも上回るかを数える。勝率は x と y のどちらについても単調なので、内訳は両端を見れば足りる。
    """
    for total in range(team.remaining + rival.remaining + 1):
        low = max(0, total - rival.remaining)
        high = min(team.remaining, total)
        if all(_beats_at(team, rival, x, total - x) for x in {low, high}):
            return total
    return None


def pennant_race(team_id: int, teams: Sequence[RaceTeam]) -> PennantRace | None:
    """`team_id` の優勝争い。`teams` は同じリーグの全球団。球団が見つからない・相手がいないときは None。

    ほかのどれかを上回れる余地がない（残りを全勝してもかなわない球団がある）ときは、確定でも点灯でもない。
    """
    team = next((t for t in teams if t.team_id == team_id), None)
    rivals = [t for t in teams if t.team_id != team_id]
    if team is None or not rivals:
        return None
    needed = [_magic_against(team, rival) for rival in rivals]
    if any(value is None for value in needed):
        return PennantRace(team_id=team_id, clinched=False, magic=None)
    magic = max(value for value in needed if value is not None)
    if magic == 0:
        return PennantRace(team_id=team_id, clinched=True, magic=None)
    return PennantRace(team_id=team_id, clinched=False, magic=magic if magic <= team.remaining else None)


def season_champion(
    leaders: Sequence[int],
    results: Iterable[tuple[int, int]],
    previous_rank: Mapping[int, int],
    order: Sequence[int],
) -> int:
    """シーズン終了時に勝率が同率で並んだ首位の球団（`leaders`）から、優勝の1球団を決める（NPB の規定）。

    1. 当該球団間の対戦勝率（`results` は (勝った球団, 負けた球団) の対戦ごとの結果。引分は含めない。
       `leaders` どうしの対戦だけを数える。対戦が0なら .000 扱い）が最も高い球団。並べば、並んだ球団だけで次へ
    2. 前年の最終順位（`previous_rank`。小さいほうが上。無い球団は最後。前年に同率首位があれば、
       呼ぶ側が規定で決めた優勝を1位にして渡す）が最も上の球団
    3. それでも並ぶ（前年の順位が同じ・開幕年で無い）なら、`order`（球団の並び順）で先の球団

    必ず1球団に決まる。`leaders` が1つならその球団。空は決められないので ValueError。
    """
    if not leaders:
        raise ValueError("首位の球団がありません。")
    candidates = list(dict.fromkeys(leaders))
    if len(candidates) > 1:
        wins = dict.fromkeys(candidates, 0)
        losses = dict.fromkeys(candidates, 0)
        for winner, loser in results:
            if winner in wins and loser in wins:
                wins[winner] += 1
                losses[loser] += 1
        best = max(TeamRecord(wins=wins[t], losses=losses[t]).winning_percentage for t in candidates)
        candidates = [t for t in candidates if TeamRecord(wins=wins[t], losses=losses[t]).winning_percentage == best]
    if len(candidates) > 1:
        unranked = max(previous_rank.values(), default=0) + 1
        top = min(previous_rank.get(t, unranked) for t in candidates)
        candidates = [t for t in candidates if previous_rank.get(t, unranked) == top]
    if len(candidates) > 1:
        position = {team_id: index for index, team_id in enumerate(order)}
        candidates.sort(key=lambda t: position.get(t, len(position)))
    return candidates[0]
