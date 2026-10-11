"""シミュレーションの試合が作る「開始・終了の時刻」と「観客数」。

**試合の結果（打席）を進める乱数の列には触れない。** ここの関数は呼ぶ側が用意した別の乱数源
（ペナントなら `game_seed` に用途の接尾辞を付けたもの）だけを使うので、同じシードの試合の
得点・打席はこのモジュールの有無で変わらない。`engine.simulate_game()` も変えない。

実データの試合に後から当てはめる規則ではなく、シミュレーションが「その試合の記録」として値を作る
（記録員が書く値の代わり）。このモジュールが時刻と観客数のモデルの**唯一の出典**。

- 開始時刻: 平日 18:00（ナイター）、土日 14:00（デーゲーム）。乱数は使わない。祝日は見ない。
- 正味の試合時間: 固定の分 ＋ 打席あたりの分 × 打席数 ＋ 投手交代あたりの分 × 救援登板数 ＋ 正規の揺らぎ。
  9回の試合で平均およそ3時間04分、標準偏差およそ18分（NPB の近年の水準）。延長は打席が増えるぶん長くなる。
  中断は作らない（天候のモデルが無い）。
- 観客数: 収容人数 × 入場率。入場率は土日が高く、0.4〜1.0 に収める（収容人数を超えない）。
  収容人数が分からない（本拠地が無い・未設定）なら `None`（根拠の無い数を作らない）。
"""

from __future__ import annotations

from datetime import date, time

from ..entities import Game
from ..value_objects import GameTimes
from .randomness import GameRandom, normal

WEEKDAY_START = time(18, 0)
WEEKEND_START = time(14, 0)

# 正味の試合時間（分）= 固定 + 打席 × 係数 + 救援登板 × 係数 + 揺らぎ
BASE_MINUTES = 38.0
MINUTES_PER_PLATE_APPEARANCE = 1.7
MINUTES_PER_RELIEVER = 3.5
DURATION_SD_MINUTES = 10.0
MIN_DURATION_MINUTES = 90  # 揺らぎで極端に短く（負に）ならないための下限

# 入場率（収容人数に対する割合）
WEEKDAY_FILL = (0.80, 0.08)  # (平均, 標準偏差)
WEEKEND_FILL = (0.90, 0.07)
MIN_FILL = 0.4
MAX_FILL = 1.0  # 収容人数を超えない


def _is_weekend(played_on: date) -> bool:
    return played_on.weekday() >= 5


def start_time_of(played_on: date) -> time:
    """開始時刻。平日はナイター、土日はデーゲーム。"""
    return WEEKEND_START if _is_weekend(played_on) else WEEKDAY_START


def simulated_times(game: Game, rng: GameRandom) -> GameTimes:
    """試合の打席と継投から、開始・終了の時刻を作る。0時を越えたら翌日の旗を立てる。中断は作らない。"""
    # 登板した投手は投球明細から数える（打者を1人も打ち取らずに降りた投手も登板に数える）
    pitchers = {line.player_id for line in game.pitching}
    # 先発は両チームに1人ずつ。それ以外が救援登板
    relievers = max(len(pitchers) - 2, 0)
    expected = (
        BASE_MINUTES + MINUTES_PER_PLATE_APPEARANCE * len(game.plate_appearances) + MINUTES_PER_RELIEVER * relievers
    )
    net = max(round(normal(rng, expected, DURATION_SD_MINUTES)), MIN_DURATION_MINUTES)

    start = start_time_of(game.played_on)
    end_total = start.hour * 60 + start.minute + net
    ends_next_day = end_total >= 24 * 60
    end_total %= 24 * 60
    return GameTimes(start=start, end=time(end_total // 60, end_total % 60), ends_next_day=ends_next_day)


def simulated_attendance(capacity: int | None, played_on: date, rng: GameRandom) -> int | None:
    """観客数。収容人数が分からなければ `None`。収容人数を超えない。"""
    mean, sd = WEEKEND_FILL if _is_weekend(played_on) else WEEKDAY_FILL
    # 収容人数が無くても乱数は引く（1試合あたりの乱数の消費を一定にし、後から収容人数が入っても列がずれないように）
    fill = min(max(normal(rng, mean, sd), MIN_FILL), MAX_FILL)
    if capacity is None:
        return None
    return min(round(capacity * fill), capacity)
