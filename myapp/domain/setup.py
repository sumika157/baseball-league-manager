"""リーグ・チーム・球場の登録（骨組みの組み立て）の規則。W3（#79）。

ここにあるのは「消してよいか」「所属リーグを変えてよいか」の規則だけで、判定の材料（チームの数・在籍の数・
試合の数・本拠地にしているチームの数）は呼ぶ側が数えて渡す。数は保存せず、その都度数える（同じ事実の出典を
2つ作らない）。**消せるかの出典はここだけ**で、画面の表示（参照クエリの DTO）とリポジトリの削除の両方がこれを通す。
消せるなら None、消せなければ日本語の理由を返す（前例: `pennant.world.postseason_format_change_blocker`）。
Django は import しない。
"""

from __future__ import annotations

from .exceptions import LeagueChangeLocked


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
