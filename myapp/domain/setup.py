"""リーグ・チーム・球場の登録（骨組みの組み立て）の規則。W3（#79）。

ここにあるのは「消してよいか」「所属リーグを変えてよいか」の規則と、表示順の並べ替え（`apply_order`。W3 T8）だけで、
判定の材料（チームの数・在籍の数・試合の数・本拠地にしているチームの数）は呼ぶ側が数えて渡す。数は保存せず、その都度数える（同じ事実の出典を
2つ作らない）。**消せるかの出典はここだけ**で、画面の表示（参照クエリの DTO）とリポジトリの削除の両方がこれを通す。
消せるなら None、消せなければ日本語の理由を返す（前例: `pennant.world.postseason_format_change_blocker`）。
Django は import しない。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from .exceptions import InvalidOrder, LeagueChangeLocked


def _ensure_count(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"数は0以上の整数で渡してください（{value!r}）。")
    return value


def league_deletion_blocker(team_count: int) -> str | None:
    """リーグを消せない理由。チームが1つも無いときだけ消せる。

    チームを連鎖で消すと、在籍・試合まで一緒に消える。チームを先に消す（か、ほかのリーグへ移す）よう案内する。
    """
    if _ensure_count(team_count):
        return (
            f"このリーグにはチームが{team_count}つあるため削除できません。"
            "先にチームを削除するか、ほかのリーグへ移してください。"
        )
    return None


def team_deletion_blocker(stint_count: int, captaincy_count: int, game_count: int) -> str | None:
    """チームを消せない理由。在籍・主将の在任・試合が1つも無いときだけ消せる。

    どれも消すと選手の経歴や試合の記録（成績の出典）が一緒に消える。誤って作ったチームを取り消すための削除にとどめる。
    """
    reasons = [
        f"{label}が{count}{unit}"
        for label, count, unit in (
            ("選手の在籍の記録", _ensure_count(stint_count), "件"),
            ("主将の在任の記録", _ensure_count(captaincy_count), "件"),
            ("試合", _ensure_count(game_count), "試合"),
        )
        if count
    ]
    if reasons:
        return f"このチームには{'・'.join(reasons)}あるため削除できません。"
    return None


def stadium_deletion_blocker(real_home_team_count: int, world_home_team_count: int) -> str | None:
    """球場を消せない理由。本拠地にしているチームが1つも無いときだけ消せる。

    ペナントの世界の球団もワークスペースの球場を共有する（2026-10-25 決定）ので、世界の球団の本拠地も数える。
    消すと世界の球団の本拠地が外れ、シミュレーションの観客数が出なくなるため。
    """
    real = _ensure_count(real_home_team_count)
    world = _ensure_count(world_home_team_count)
    if not real and not world:
        return None
    parts = []
    if real:
        parts.append(f"{real}チーム")
    if world:
        parts.append(f"ペナントの世界の{world}球団")
    return f"この球場を本拠地にしているチーム（{'・'.join(parts)}）があるため削除できません。"


def league_change_blocker(game_count: int) -> str | None:
    """チームの所属リーグを変えられない理由。試合が1つも無いときだけ変えられる（2026-10-25 決定）。

    所属リーグは期間を持たず、順位・タイトルは今の所属で数える。試合のあるチームを移すと、過去のシーズンの順位表まで
    移した先のリーグで数え直されてしまう。
    """
    if _ensure_count(game_count):
        return f"このチームには試合が{game_count}試合あるため、所属リーグを変えられません。"
    return None


def ensure_league_change_allowed(game_count: int) -> None:
    """所属リーグを変えてよいかを検査する。変えられなければ `LeagueChangeLocked`（理由は `league_change_blocker`）。"""
    if (reason := league_change_blocker(game_count)) is not None:
        raise LeagueChangeLocked(reason)


# --- 表示順の並べ替え（W3 T8。2026-10-25 決定: 登録画面の一覧のドラッグで決める） ---

ORDER_MISMATCH = (
    "並べ替えの内容が今の一覧と合いません（ほかの人が行を足したり消したりした可能性があります）。"
    "画面を開き直してから並べ替えてください。"
)


class Orderable(Protocol):
    """表示順を持つ行（`League`・`Team`）。"""

    @property
    def id(self) -> int | None: ...

    def reorder(self, display_order: int) -> None: ...


def apply_order[O: Orderable](items: Sequence[O], ordered_ids: Sequence[int]) -> list[O]:
    """`ordered_ids` の並びに、表示順を 0, 1, 2 … と振り直す（並べた後の行を返す）。

    **識別子で組み立てる**（位置では組み立てない）。`ordered_ids` が `items` の id の集まりと**ちょうど一致しない**
    とき（欠け・余り・重複・範囲の外の id）は `InvalidOrder` で、どの行も変えない。欠けた行を末尾に回すなどして
    黙って補わない（開いていた画面が古いまま送られた並べ替えで、見ていない行の順を決めない）。
    """
    by_id = {item.id: item for item in items}
    if len(ordered_ids) != len(set(ordered_ids)) or set(ordered_ids) != set(by_id):
        raise InvalidOrder(ORDER_MISMATCH)
    ordered = [by_id[item_id] for item_id in ordered_ids]
    for position, item in enumerate(ordered):
        item.reorder(position)
    return ordered
