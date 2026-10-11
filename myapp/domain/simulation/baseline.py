"""リーグの基準値。**NPB の水準を決める定数の唯一の出典。**

能力 50 どうしの対戦は、どの段の確率もこの値と正確に一致する（`odds` の log5 の恒等性）。
水準の調整はここで、ばらつきの調整は `odds.RatingSensitivity` で行う、と役割を分ける。

率は打席ごとの確率の判定（死球 → 四球 → 犠打・犠飛 → 三振 → 本塁打 → インプレー）に
合わせた形で持つ。判定の途中で打席の一部が先に落ちる（四死球・犠打・犠飛）ので、
三振や本塁打を「打席あたり」で持つと、段ごとの確率に直す式が要る
（`strikeout_given_at_bat` など）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LeagueBaseline:
    # --- 打席あたりの事象 ---
    hit_by_pitch: float = 0.010
    walk: float = 0.074  # 故意四球を含む
    strikeout: float = 0.190
    home_run: float = 0.024
    # 犠打・犠飛が先に落ちる打席の見込み割合。三振・本塁打を段の確率に直すためだけに使う
    sacrifice: float = 0.021

    # --- インプレー ---
    # 本塁打を除く、フェアゾーンに飛んだ打球が安打になる割合（打数 − 三振 − 本塁打 に対する安打）
    babip: float = 0.298
    # 安打（本塁打を除く）の内訳。三塁打は二塁打を除いた残りに対する割合
    double_share: float = 0.225
    triple_share: float = 0.024
    # 安打にならなかった打球のうち失策になる割合（走者一塁で封殺できるとき野選になる割合）
    error_share: float = 0.032
    fielders_choice_share: float = 0.075
    # 失策でも野選でもないアウトの内訳。残りが邪飛
    ground_out_share: float = 0.52
    fly_out_share: float = 0.34
    line_out_share: float = 0.10

    # --- 結果の細目 ---
    intentional_walk_share: float = 0.035
    looking_strikeout_share: float = 0.28

    # --- 状況による判定（走者の状況が合うときの確率） ---
    sacrifice_bunt_rate: float = 0.075
    sacrifice_fly_rate: float = 0.10

    @property
    def walk_given_not_hit_by_pitch(self) -> float:
        """死球でなかった打席が四球になる確率（判定の2段目）。"""
        return self.walk / (1.0 - self.hit_by_pitch)

    @property
    def at_bat_share(self) -> float:
        """打数に数える打席の割合の見込み（四死球と犠打・犠飛を除く）。"""
        return 1.0 - self.hit_by_pitch - self.walk - self.sacrifice

    @property
    def strikeout_given_at_bat(self) -> float:
        """打数の打席が三振になる確率。"""
        return self.strikeout / self.at_bat_share

    @property
    def home_run_given_not_strikeout(self) -> float:
        """三振でなかった打数の打席が本塁打になる確率。"""
        return self.home_run / (self.at_bat_share - self.strikeout)

    @property
    def triple_given_not_double(self) -> float:
        """二塁打でなかった安打（本塁打を除く）が三塁打になる確率。"""
        return self.triple_share / (1.0 - self.double_share)


# NPB の近年の水準。
NPB = LeagueBaseline()
