"""勝敗・セーブ・ホールドの導出（日本プロ野球の規則）。

どの記録も「継投した時点のスコア」で決まる。最終得点だけでは
「3点差以内のリードで登板して抑えた」を判定できないため、
イニングスコア（LineScore）と各投手の登板した回から導く。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..entities import Game

# セーブは3点差以内のリードで登板して1回以上、または3回以上を投げた場合。
SAVE_LEAD_LIMIT = 3
SAVE_MINIMUM_OUTS = 3
SAVE_LONG_RELIEF_OUTS = 9
# 先発が勝利投手になるには5回以上を投げる必要がある。
STARTER_WIN_MINIMUM_OUTS = 15


@dataclass(frozen=True)
class PitchingDecisions:
    """1試合ぶんの勝敗・セーブ・ホールド。選手idの集合で表す。

    勝利・敗戦・セーブは1試合に1人までなので単独、ホールドは複数付きうる。
    """

    winner_id: int | None = None
    loser_id: int | None = None
    save_id: int | None = None
    hold_ids: frozenset[int] = frozenset()

    def wins_for(self, player_id: int) -> int:
        return 1 if player_id == self.winner_id else 0

    def losses_for(self, player_id: int) -> int:
        return 1 if player_id == self.loser_id else 0

    def saves_for(self, player_id: int) -> int:
        return 1 if player_id == self.save_id else 0

    def holds_for(self, player_id: int) -> int:
        return 1 if player_id in self.hold_ids else 0


def _staff_of(game: Game, team_ids: set[int], team_of: dict[int, int]):
    """指定チームの投手を、登板順に並べて返す。"""
    entries = [entry for entry in game.pitching if team_of.get(entry.player_id) in team_ids]
    return sorted(entries, key=lambda e: (e.appearance_order, e.entered_inning))


def _pitcher_covering(staff, inning: int):
    """その回に投げていた投手。登板した回がその回以前で最も後の投手。"""
    covering = None
    for entry in staff:
        if entry.entered_inning <= inning:
            covering = entry
    return covering or (staff[0] if staff else None)


def _lead_before(game: Game, inning: int, *, is_home: bool) -> int:
    """その回に登板する直前の、自チームから見た得点差。

    ホームの投手は表に投げるので、直前は前の回の裏を終えた時点。
    ビジターの投手は裏に投げるので、直前は同じ回の表を終えた時点。
    """
    if is_home:
        away, home = game.line_score.score_after(inning - 1, bottom=True)
    else:
        away, home = game.line_score.score_after(inning, bottom=False)
    return (home - away) if is_home else (away - home)


def _lead_after(game: Game, inning: int, *, is_home: bool) -> int:
    """その回を投げ終えた時点の、自チームから見た得点差。"""
    if is_home:
        away, home = game.line_score.score_after(inning, bottom=False)
    else:
        away, home = game.line_score.score_after(inning, bottom=True)
    return (home - away) if is_home else (away - home)


def _decisive_inning(game: Game, *, winner_is_home: bool) -> tuple[int, bool]:
    """勝ちチームが最後にリードを奪った半回。(回, 裏か) を返す。

    そこで入った点が決勝点。その時点の投手が勝利投手・敗戦投手になる。
    """
    decisive = (1, False)
    led = False
    for inning in range(1, game.line_score.innings + 1):
        for bottom in (False, True):
            away, home = game.line_score.score_after(inning, bottom=bottom)
            margin = (home - away) if winner_is_home else (away - home)
            if margin > 0 and not led:
                decisive, led = (inning, bottom), True
            elif margin <= 0:
                led = False
    return decisive


def _most_effective_reliever(staff):
    """最も内容の良い救援。長く投げて自責点が少ない順。

    先発が5回未満で勝利投手の条件を満たさない場合の受け皿。規則では記録員の
    判断だが、ここでは「自責点が少なく、長く投げた投手」を選ぶ。
    """
    relievers = [entry for entry in staff if not entry.is_starter]
    if not relievers:
        return None
    return min(
        relievers,
        key=lambda e: (e.line.earned_runs, -e.line.innings.outs, e.appearance_order),
    )


def pitching_decisions(game: Game, team_of: dict[int, int]) -> PitchingDecisions:
    """1試合の勝敗・セーブ・ホールドを日本プロ野球の規則で決める。

    team_of は 選手id → チームid。試合の明細には両チームの投手が混ざって
    入っているため、どちらのチームの投手かを外から渡してもらう
    （Game 集約はロスターを知らない）。

    イニングスコアが無い試合では判定できないので、何も付けない。
    """
    if game.line_score.is_empty or not game.pitching:
        return PitchingDecisions()

    home_staff = _staff_of(game, {game.home_team_id}, team_of)
    away_staff = _staff_of(game, {game.away_team_id}, team_of)

    holds = set()
    for staff, is_home in ((home_staff, True), (away_staff, False)):
        holds |= _holds(game, staff, is_home=is_home)

    if game.is_tie:
        # 引分では勝敗もセーブも付かない。ホールドは付く
        return PitchingDecisions(hold_ids=frozenset(holds))

    winner_is_home = game.winner_team_id == game.home_team_id
    winning_staff = home_staff if winner_is_home else away_staff
    losing_staff = away_staff if winner_is_home else home_staff

    inning, _ = _decisive_inning(game, winner_is_home=winner_is_home)

    winner = _pitcher_covering(winning_staff, inning)
    if winner is not None and winner.is_starter and (winner.line.innings.outs < STARTER_WIN_MINIMUM_OUTS):
        winner = _most_effective_reliever(winning_staff) or winner

    loser = _pitcher_covering(losing_staff, inning)
    save = _save(game, winning_staff, winner, is_home=winner_is_home)

    return PitchingDecisions(
        winner_id=winner.player_id if winner else None,
        loser_id=loser.player_id if loser else None,
        save_id=save.player_id if save else None,
        # 勝利投手にホールドは付かない
        hold_ids=frozenset(pid for pid in holds if winner is None or pid != winner.player_id),
    )


def _save(game: Game, staff, winner, *, is_home: bool):
    """セーブ。試合を締めた投手が条件を満たす場合に付く。

    完投は勝利のみでセーブは付かない。勝利投手にも付かない。
    """
    if len(staff) < 2:
        return None

    finisher = staff[-1]
    if winner is not None and finisher.player_id == winner.player_id:
        return None

    lead = _lead_before(game, finisher.entered_inning, is_home=is_home)
    if lead <= 0:
        return None

    outs = finisher.line.innings.outs
    qualifies = (lead <= SAVE_LEAD_LIMIT and outs >= SAVE_MINIMUM_OUTS) or outs >= SAVE_LONG_RELIEF_OUTS
    return finisher if qualifies else None


def _holds(game: Game, staff, *, is_home: bool) -> set[int]:
    """ホールド。セーブの条件を満たす状況で登板し、1つ以上アウトを取って
    リードを保ったまま次の投手に引き継いだ救援投手に付く。
    """
    if len(staff) < 3:
        # 先発と最後の投手だけでは、引き継いだ救援が存在しない
        return set()

    holds = set()
    for index, entry in enumerate(staff[1:-1], start=1):
        if entry.line.innings.outs < 1:
            continue
        lead = _lead_before(game, entry.entered_inning, is_home=is_home)
        if not (0 < lead <= SAVE_LEAD_LIMIT):
            continue
        # 引き継いだ時点でリードが残っているか
        last_inning = staff[index + 1].entered_inning - 1
        if _lead_after(game, max(entry.entered_inning, last_inning), is_home=is_home) > 0:
            holds.add(entry.player_id)
    return holds
