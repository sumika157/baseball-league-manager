"""仮想の試合データを投入する。

チームと選手が登録済みであることを前提に、リーグ内の総当たりで1シーズンぶんの
試合を作る。順位表・対戦成績・月別成績・タイトル・ボックススコアといった画面を、
データのある状態で確認するためのもの。

**打席を1つずつ組み立て、成績はそこから導出する。** 以前は「完成したボックススコアを
乱数で作る」形で、打点の合計を得点に一致させる・投手の被安打を相手打線の安打から
逆算する、といった配分の工夫が要った。打席を先に作れば整合は自動的に取れる
（打点は還った走者を数えるだけ、被安打は投げた打席の安打を数えるだけ）。
配分のための多項分布・多変量超幾何分布はこの書き換えで不要になった。

成績は**確率分布から引く**（numpy）。

| 対象 | 分布 | なぜ |
| --- | --- | --- |
| 打者の能力 | 多変量正規分布 | 打率・長打力・四球率には相関がある（強打者は歩かされる） |
| 投手の能力 | 対数正規分布 | 失点の倍率なので正の値だけを取り、右に裾を引く |
| 1打席の結果 | 能力から作った確率での逐次判定 | 「この打席で何が起きたか」そのもの |

**得点・イニングスコア・勝敗は引かない。** 打席を積み上げた結果として決まる。
延長は12回まで行い、決着しなければ引分（NPB の規定）。ホームがリードしていれば
9回裏は行わず、逆転すればその時点で終わる（サヨナラ）。

**得点はチームの戦力に応じて増減する。** 打者の能力と投手の力量が1打席ごとの
確率に効くので、戦力差は打席の積み上げを通して得点差になる。

**出場選手は毎試合ロスターから選び直さず、チームごとに決めたレギュラーと
先発ローテーションを軸にする。** 全員を均等に出場させると誰も規定打席・規定
投球回に届かず、タイトルや率のランキングが空になってしまう。

試合日は4月から9月に散らす。1日に固めると月別成績が1行しか出ず、
月ごとの推移を確認できない。

**簡略化しているもの**（画面の確認には要らず、入れると複雑さだけが増える）:
代走・投手の打席（指名打者制のみ）・タッチアップでの進塁（犠飛として扱う）・
捕逸と暴投。盗塁は「打者が打球を放たない打席」でだけ試みる（下の説明を参照）。
"""

from collections import defaultdict
from dataclasses import dataclass, field, fields, replace
from datetime import date, timedelta

import numpy as np
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from myapp.domain import services as domain_services
from myapp.domain.entities import FieldingError, PlateAppearance, RunnerAdvance
from myapp.domain.entities import Game as DomainGame
from myapp.domain.value_objects import (
    AdvanceReason,
    Base,
    BattingLine,
    ErrorKind,
    FieldingPosition,
    PitchingLine,
    PlateAppearanceResult,
    Position,
    Season,
)
from myapp.models import (
    Game,
    GameBattingLine,
    GameFieldingError,
    GameInningScore,
    GamePitchingLine,
    GamePlateAppearance,
    GameRunnerAdvance,
    League,
    PlayerStint,
)

# --- 現実的な数値に寄せるための調整値 ---
#
# 目標はNPBの近年の水準。1試合平均得点 3.9、リーグ打率 .255、リーグ防御率 3.5、
# WHIP 1.25、K/9 7.5、BB/9 2.7、9回あたり被本塁打 0.9 あたりに落ち着く。
# 投入後に表示される「リーグ全体の水準」で確認できる。

INNINGS_PER_GAME = 9
OUTS_PER_INNING = 3
# 延長の上限。NPB は12回を終えて同点なら引分。
MAX_INNINGS = 12

# 先発が受け持つ回。残りは救援で分ける。
STARTER_INNINGS = (5, 7)
# 救援が受け持つアウト数。
RELIEVER_OUTS = (2, 5)
# 交代が回の途中で起きる確率。受け持ちを3の倍数から外すことで表す
# （これが無いと投球回が常に X.0 になり、5.2 のような表記が出てこない）。
MID_INNING_CHANGE_RATIO = 0.40

# 打順に並べる人数。
LINEUP_SIZE = 9

# 代打。7回以降、打順が回ってきた枠を控えと入れ替える確率と、1試合の上限。
PINCH_HITTER_FROM_INNING = 7
PINCH_HITTER_RATIO = 0.06
MAX_PINCH_HITTERS = 2

# レギュラーが先発を外れて控えに回る確率。休養・不調による入れ替えを表す。
BENCH_RATIO = 0.12
# 先発ローテーションの人数。残りの投手は救援に回る。
ROTATION_SIZE = 5

# 控え・救援の序列の減衰率。控えの出番を全員に均等に散らすと、1人あたり30打数
# 程度しか回らず、打率10割に近い「小さな標本の極端な率」が一覧の上位を占める。
BENCH_DECAY = 0.80
BULLPEN_DECAY = 0.85

# 抑えが最終回を任される割合。序列1位を専任にしないとセーブが数人に分散し、
# 「抑えが年30セーブ」という実際の形にならない。
#
# 実際の抑えは「勝っている終盤」にしか出ないので年50試合前後にとどまる。
# 勝っている接戦（3点差以内）ではほぼ出て、それ以外の試合ではまれに出る。
CLOSER_USAGE_IN_SAVE_SITUATION = 0.88
CLOSER_USAGE_OTHERWISE = 0.10

# --- 打者の能力 ---
# 順に「1打数あたりの安打確率」「安打のうち本塁打の割合」「1打席あたりの四球率」。
# 長打力のばらつきは平均を動かさずに広げてある。狭くすると本塁打王が20本台に
# とどまり、リーグ本塁打王が30〜40本という実際の水準に届かない。
BATTER_MEANS = np.array([0.250, 0.080, 0.070])
BATTER_SDS = np.array([0.030, 0.058, 0.020])
BATTER_BOUNDS = np.array([[0.170, 0.335], [0.010, 0.270], [0.020, 0.140]])
# 能力の相関。長打力のある打者は歩かされやすい（0.35）。打率と長打力は
# 弱い正の相関にとどめる（打率だけ高い単打型と、一発屋の両方を残すため）。
BATTER_CORRELATION = np.array(
    [
        [1.00, 0.15, 0.10],
        [0.15, 1.00, 0.35],
        [0.10, 0.35, 1.00],
    ]
)

# 安打の内訳（本塁打を除いた残りの配分）。二塁打・三塁打・単打の比。
DOUBLE_SHARE, TRIPLE_SHARE = 0.225, 0.030

# --- 投手の能力 ---
# 失点の倍率。1.0 が平均で、小さいほど抑える投手。対数正規なので必ず正の値になる。
PITCHER_SKILL_SIGMA = 0.26
PITCHER_SKILL_BOUNDS = (0.45, 1.75)
# 投手の力量を1打席の確率に効かせる強さ。1.0 でそのまま比例する。
# そのまま掛けると能力 0.5 の投手の被打率が .12 という非現実的な値になるため、
# 指数で鈍らせる（奪三振も同じ理由で STRIKEOUT_BOOST を掛ける）。
PITCHER_CONTACT_ELASTICITY = 0.40
PITCHER_WALK_ELASTICITY = 0.45
STRIKEOUT_BOOST = 0.50

# --- 1打席の結果 ---
# 打数に数える打席のうち安打になる確率は打者の能力（contact）そのもの。
# ここに置くのは「安打にならなかった打数」の振り分けと、打数に数えない結果の頻度。

BATTER_HIT_BY_PITCH_PER_PA = 0.010
# 四球のうち故意四球。
INTENTIONAL_WALK_SHARE = 0.035

# 安打にならなかった打数の振り分け。残りが三振と打球によるアウト。
REACHED_ON_ERROR_SHARE = 0.024
FIELDERS_CHOICE_SHARE = 0.055
STRIKEOUT_SHARE = 0.275
# 三振のうち見逃し。
LOOKING_STRIKEOUT_SHARE = 0.28
# 打球によるアウトの内訳（ゴロ・フライ・ライナー。残りが邪飛）。
GROUND_OUT_SHARE, FLY_OUT_SHARE, LINE_OUT_SHARE = 0.52, 0.34, 0.10

# 送りバント（走者が一塁か二塁にいて、三塁が空いていて、2アウト未満のとき）。
SACRIFICE_BUNT_RATIO = 0.075
# 犠飛（三塁に走者がいて2アウト未満のとき）。
SACRIFICE_FLY_RATIO = 0.10
# 併殺（一塁に走者がいて2アウト未満のゴロ）。
DOUBLE_PLAY_RATIO = 0.40
# ゴロアウトで走者が1つ進む確率（併殺にならなかった場合）。
# **ここは得点に強く効く。** 三塁走者はゴロが転がるたびに還ってしまうので、
# 高くすると1試合平均得点がNPBの水準を大きく超える。
ADVANCE_ON_GROUND_OUT_RATIO = 0.18

# 盗塁。**打者が打球を放たない打席（四死球・三振）でだけ試みる。**
# 打球が飛ぶ打席に混ぜると、走塁のアウトが打球の処理と同じ打席に並んでしまう。
STEAL_ATTEMPT_RATIO = 0.55
STEAL_SUCCESS_RATIO = 0.72

# 単打・二塁打での走者の進み方。**1試合平均得点はここでほぼ決まる。**
# 打率・四球・本塁打を水準に合わせてもなお得点が多い場合は、走者を還す効率が
# 高すぎるということなので、打撃の指標ではなくここを下げる。
EXTRA_BASE_ON_SINGLE_FROM_FIRST = 0.20
SCORE_ON_SINGLE_FROM_SECOND = 0.48
SCORE_ON_DOUBLE_FROM_FIRST = 0.33

# 失策の種類の内訳（捕球・送球・落球）。
ERROR_KIND_WEIGHTS = {ErrorKind.FIELDING: 0.35, ErrorKind.THROWING: 0.45, ErrorKind.DROPPED_FLY: 0.20}

# セーブが記録される点差の上限は、勝敗の判定と同じ規則なのでドメインから借りる。
# ここで別に持つと、抑えを出す条件と実際にセーブが付く条件がずれる。
SAVE_LEAD_LIMIT = domain_services.SAVE_LEAD_LIMIT

# 試合日を散らす期間（開幕〜終了）。
SEASON_START = (4, 1)
SEASON_END = (9, 30)

# 対戦カードごとの試合数の既定値。6チームのリーグなら 5組 × 29 = 145試合となり、
# NPBの143試合に近いシーズンになる。これより短くすると規定打席・規定投球回に
# 誰も届かず、タイトルや率のランキングが空のままになる。
DEFAULT_GAMES_PER_PAIR = 29

# 何試合ぶんためてから DB に流すか。1試合で打席・進塁があわせて約280行あるため、
# 全試合ぶんをメモリに持つと数百MBになる。
FLUSH_EVERY_GAMES = 200

# 守備の並び。捕手・内野4・外野3・指名打者で9人。登録位置からこの順に埋める。
DEFENSIVE_SLOTS = (
    (Position.CATCHER, (FieldingPosition.CATCHER,)),
    (
        Position.INFIELDER,
        (
            FieldingPosition.FIRST_BASE,
            FieldingPosition.SECOND_BASE,
            FieldingPosition.THIRD_BASE,
            FieldingPosition.SHORTSTOP,
        ),
    ),
    (
        Position.OUTFIELDER,
        (
            FieldingPosition.LEFT_FIELD,
            FieldingPosition.CENTER_FIELD,
            FieldingPosition.RIGHT_FIELD,
        ),
    ),
    (Position.DESIGNATED_HITTER, (FieldingPosition.DESIGNATED_HITTER,)),
)

P = PlateAppearanceResult
R = AdvanceReason

# 投球成績のうち、列としては持たない項目。投球回は野球表記の1列（innings_pitched）に
# 直して持ち、先発登板と救援勝利は登板順から導く。**これ以外は値オブジェクトの
# フィールドをそのまま列に流す**（項目を並べ直すと、増やしたときにここだけ古くなる）。
PITCHING_NOT_STORED = ("innings", "starts", "relief_wins")


def _covariance(sds, correlation):
    """標準偏差と相関行列から共分散行列を作る。"""
    return np.outer(sds, sds) * correlation


def _depth_weights(count, decay):
    """序列に沿って減衰する確率。1番手から順に decay を掛けていく。"""
    if count == 0:
        return np.array([])
    weights = decay ** np.arange(count)
    return weights / weights.sum()


def _expected_ops(talent):
    """能力から見込まれる OPS。序列を決めるために使う。

    出塁率は「安打確率＋四球率」、長打率は「安打確率 × 1安打あたりの塁打数」で
    近似する。塁打数は本塁打の割合が高いほど大きくなる。
    """
    contact, power, walk = talent[:, 0], talent[:, 1], talent[:, 2]
    bases_per_hit = power * 4 + (1 - power) * (DOUBLE_SHARE * 2 + TRIPLE_SHARE * 3 + (1 - DOUBLE_SHARE - TRIPLE_SHARE))
    return (contact + walk) + contact * bases_per_hit


def _forced_bases(occupied):
    """打者が一塁を与えられたときに、押し出される走者の塁。

    一塁から詰まっている連続した塁だけが押し出される（一塁と三塁なら三塁は動かない）。
    """
    forced = []
    for base in Base.occupiable():
        if base not in occupied:
            break
        forced.append(base)
    return forced


@dataclass
class _Batter:
    """打順の1枠に入っている打者。交代すると slot_sequence が1つ増える。"""

    player: object
    batting_order: int
    slot_sequence: int
    fielding_position: FieldingPosition
    # 打率・長打力・四球率。1打席ごとの確率をここから作る
    talent: object


@dataclass
class _Outing:
    """1人の投手の登板。受け持ちのアウト数に達したら次の投手に代わる。"""

    player: object
    appearance_order: int
    entered_inning: int
    skill: float
    target_outs: int
    outs: int = 0


@dataclass
class _Side:
    """1チームの、その試合の状態。攻撃と守備の両方を持つ。"""

    team: object
    roster: dict
    is_home: bool
    lineup: list = field(default_factory=list)
    appearances: list = field(default_factory=list)
    outings: list = field(default_factory=list)
    order_index: int = 0
    used_player_ids: set = field(default_factory=set)
    foreign_used: int = 0
    pinch_hitters: int = 0
    score: int = 0

    @property
    def pitcher(self) -> _Outing:
        return self.outings[-1]


class Command(BaseCommand):
    help = "各リーグに仮想の試合データを投入する（順位表・タイトルなどの確認用）"

    def add_arguments(self, parser):
        parser.add_argument("--seed", type=int, default=None, help="乱数シード（再現用）")
        parser.add_argument("--year", type=int, default=date.today().year, help="投入するシーズン（西暦）")
        parser.add_argument(
            "--games-per-pair",
            type=int,
            default=DEFAULT_GAMES_PER_PAIR,
            help=f"対戦カードごとの試合数（既定 {DEFAULT_GAMES_PER_PAIR}）",
        )
        parser.add_argument("--dry-run", action="store_true", help="投入せず件数だけ表示する")
        parser.add_argument(
            "--replace",
            action="store_true",
            help="指定シーズンの既存の試合を削除してから投入する（既定は中止する）",
        )

    def handle(self, *args, **options):
        self.rng = np.random.default_rng(options["seed"])
        # 投入の経過と水準は verbosity に従う（テストから呼ぶと大量に流れるため）
        self.quiet = options["verbosity"] < 1

        year = options["year"]
        per_pair = options["games_per_pair"]
        if per_pair < 1:
            raise CommandError("--games-per-pair は1以上を指定してください。")

        existing = Game.objects.filter(year=year)
        if existing.exists() and not options["replace"]:
            raise CommandError(
                f"{year}年の試合が既に {existing.count()} 件あります。"
                "重複を防ぐため中止しました。作り直す場合は --replace を付けてください。"
            )

        rosters = self._rosters(year)
        schedule = self._schedule(year, per_pair, rosters)

        if not schedule:
            raise CommandError(
                "試合を作れるリーグがありません。"
                "同じリーグに2チーム以上あり、各チームに投手と野手が登録されている必要があります。"
            )

        if options["dry_run"]:
            self._say(f"{year}年 · {len(schedule)}試合を投入します（--dry-run のため未実行）")
            self._report_schedule(schedule)
            return

        self.totals = defaultdict(int)
        with transaction.atomic():
            if options["replace"] and existing.exists():
                removed = existing.count()
                existing.delete()  # 明細は on_delete=CASCADE で一緒に消える
                self._say(f"{year}年の既存の試合 {removed} 件を削除しました。")
            self._play_and_save(year, schedule, rosters)

        self._say(
            self.style.SUCCESS(
                f"{year}年 · {len(schedule)}試合を投入しました"
                f"（打席 {self.totals['plate_appearances']}件 / 進塁 {self.totals['advances']}件）"
            )
        )
        self._report_schedule(schedule)
        self._report_levels()

    def _say(self, message):
        if not self.quiet:
            self.stdout.write(message)

    # --- 下ごしらえ ---

    def _rosters(self, year):
        """チームごとの在籍中の選手を、投手と野手に分けて集める。

        その年に在籍している選手だけを対象にする（加入前・退団後の選手を
        出場させると、経歴と成績が食い違う）。
        """
        rosters = defaultdict(lambda: {"batters": [], "pitchers": [], "foreign": set()})
        stints = PlayerStint.objects.filter(from_year__lte=year).select_related("player", "team")
        for stint in stints:
            if stint.to_year is not None and stint.to_year < year:
                continue
            entry = rosters[stint.team_id]
            if stint.player.position == Position.PITCHER.value:
                entry["pitchers"].append(stint.player)
            else:
                entry["batters"].append(stint.player)
            if stint.player.is_foreign_player:
                entry["foreign"].add(stint.player.id)

        for entry in rosters.values():
            self._assign_depth_chart(entry)
        return rosters

    def _assign_depth_chart(self, roster):
        """レギュラー・控え・先発ローテーション・救援と、選手の能力を決める。

        毎試合ロスターから選び直すと出場が全員に薄く散り、規定打席・規定投球回に
        誰も届かない。実際のチームと同じように主力を固定する。

        能力もシーズン前に一度だけ引く。試合ごとに引き直すと全員が平均へ
        収束し、首位打者も最下位も同じ打率になってしまう。

        **序列は能力の高い順に決める。** 無作為に選ぶと、打率 .170 の打者が
        規定打席に到達し、防御率 4.90 の投手がローテーションに残ってしまう。
        """
        batters = roster["batters"]
        talents = self._batter_talents(batters)
        if batters:
            order = np.argsort(-_expected_ops(np.array([talents[p.id] for p in batters])))
            batters = [batters[index] for index in order]

        roster["batting_talent"] = talents
        roster["regulars"] = batters[:LINEUP_SIZE]
        roster["bench"] = batters[LINEUP_SIZE:]
        roster["bench_weights"] = _depth_weights(len(roster["bench"]), BENCH_DECAY)
        roster["positions"] = self._defensive_alignment(roster["regulars"])

        pitchers = roster["pitchers"]
        skills = self._pitcher_talents(pitchers)
        # 失点の倍率なので小さいほど good。昇順に並べれば上位が主力になる
        pitchers = sorted(pitchers, key=lambda p: skills[p.id])

        roster["pitching_talent"] = skills
        roster["rotation"] = pitchers[:ROTATION_SIZE]
        roster["bullpen"] = pitchers[ROTATION_SIZE:] or roster["rotation"]

    @staticmethod
    def _defensive_alignment(regulars):
        """スタメンの守備位置を決める。登録位置に合う枠から順に埋める。

        捕手・内野4・外野3・指名打者の9枠を、登録位置が合う選手で埋める。
        枠が埋まらない場合（外野手が足りないなど）は指名打者に回す。
        指名打者の枠も埋まっていれば守備位置なしとする。
        """
        available = {slot: list(slots) for slot, slots in DEFENSIVE_SLOTS}
        assigned = {}
        leftovers = []

        for player in regulars:
            registered = Position.from_label(player.position)
            slots = available.get(registered)
            if slots:
                assigned[player.id] = slots.pop(0)
            else:
                leftovers.append(player)

        spare = [slot for slots in available.values() for slot in slots]
        for player in leftovers:
            assigned[player.id] = spare.pop(0) if spare else FieldingPosition.DESIGNATED_HITTER
        return assigned

    def _batter_talents(self, batters):
        """打率・長打力・四球率を相関つきで引く。player_id → (3,) の配列。"""
        if not batters:
            return {}
        drawn = self.rng.multivariate_normal(
            BATTER_MEANS, _covariance(BATTER_SDS, BATTER_CORRELATION), size=len(batters)
        )
        drawn = np.clip(drawn, BATTER_BOUNDS[:, 0], BATTER_BOUNDS[:, 1])
        return {player.id: drawn[index] for index, player in enumerate(batters)}

    def _pitcher_talents(self, pitchers):
        """失点の倍率を対数正規分布から引く。player_id → float。"""
        if not pitchers:
            return {}
        drawn = self.rng.lognormal(mean=0.0, sigma=PITCHER_SKILL_SIGMA, size=len(pitchers))
        drawn = np.clip(drawn, *PITCHER_SKILL_BOUNDS)
        return {player.id: float(drawn[index]) for index, player in enumerate(pitchers)}

    def _schedule(self, year, per_pair, rosters):
        """組み合わせと試合日だけを決める。試合の中身はまだ作らない。"""
        schedule = []
        for league in League.objects.prefetch_related("teams"):
            teams = [
                team for team in league.teams.all() if rosters[team.id]["batters"] and rosters[team.id]["pitchers"]
            ]
            if len(teams) < 2:
                continue

            cards = [
                (home, away)
                for index, home in enumerate(teams)
                for away in teams[index + 1 :]
                for _ in range(per_pair)
            ]
            # ホームとビジターが偏らないよう半分は入れ替える
            cards = [(away, home) if position % 2 else (home, away) for position, (home, away) in enumerate(cards)]
            cards = self._shuffled(cards)

            for played_on, (home, away) in zip(self._dates(year, len(cards)), cards, strict=True):
                schedule.append({"league": league, "home": home, "away": away, "played_on": played_on})
        return schedule

    def _shuffled(self, items):
        order = self.rng.permutation(len(items))
        return [items[index] for index in order]

    @staticmethod
    def _dates(year, count):
        """試合日を開幕から終了まで均等に散らす。"""
        start = date(year, *SEASON_START)
        span = (date(year, *SEASON_END) - start).days
        return [start + timedelta(days=index * span // max(1, count)) for index in range(count)]

    # --- 試合を進めて保存する ---

    def _play_and_save(self, year, schedule, rosters):
        """日程を順に消化し、一定数ごとに DB へ流す。

        全試合ぶんをためてから書くと、打席・進塁だけで数十万件のオブジェクトを
        同時に抱えることになる。
        """
        # 先発の順番はチームごとに持ち回る（ローテーション）
        starts = defaultdict(int)
        buffered = []
        for card in schedule:
            buffered.append(self._play(card, rosters, starts))
            if len(buffered) >= FLUSH_EVERY_GAMES:
                self._save(year, buffered)
                buffered = []
        if buffered:
            self._save(year, buffered)

    def _play(self, card, rosters, starts):
        """1試合を打席単位で進めて、保存に必要なものをまとめて返す。"""
        home, away = card["home"], card["away"]
        limit = card["league"].foreign_player_game_limit

        sides = {}
        for team, is_home in ((home, True), (away, False)):
            side = _Side(team=team, roster=rosters[team.id], is_home=is_home)
            self._set_lineup(side, limit)
            self._take_mound(side, starts[team.id], limit)
            starts[team.id] += 1
            sides[is_home] = side

        plate_appearances = self._simulate(sides, limit)

        game = DomainGame(
            season=Season(card["played_on"].year),
            played_on=card["played_on"],
            home_team_id=home.id,
            away_team_id=away.id,
            home_score=sides[True].score,
            away_score=sides[False].score,
            plate_appearances=plate_appearances,
        )
        game.line_score = game.derived_line_score()
        # ORM へ直接書くコードは集約の検査を素通りするので、自分で同じ検査を行う
        game.ensure_plate_appearances_consistent()
        game.ensure_line_score_matches()

        batting = self._batting_lines(sides, plate_appearances)
        for entry in batting:
            game.record_batting(
                entry["player"].id,
                entry["line"],
                batting_order=entry["batting_order"],
                slot_sequence=entry["slot_sequence"],
                fielding_position=entry["fielding_position"],
            )
        pitching = self._pitching_lines(game, sides, plate_appearances)
        # 明細と打席が食い違っていないこと。bulk_create は集約の検査を素通りする
        domain_services.ensure_lines_match_plate_appearances(game)
        return {
            "league_name": card["league"].name,
            "card": card,
            "game": game,
            "batting": batting,
            "pitching": pitching,
        }

    def _simulate(self, sides, limit):
        """試合を打席単位で進める。通し番号が時系列の唯一の出典。"""
        plate_appearances = []
        inning = 1
        while True:
            for is_bottom in (False, True):
                if self._skip_bottom(sides, inning, is_bottom):
                    continue
                self._half_inning(plate_appearances, sides, inning, is_bottom, limit)
            if inning >= INNINGS_PER_GAME and sides[True].score != sides[False].score:
                break
            if inning >= MAX_INNINGS:
                break
            inning += 1
        return plate_appearances

    @staticmethod
    def _skip_bottom(sides, inning, is_bottom):
        """9回以降の裏は、ホームが既にリードしていれば行わない。"""
        return is_bottom and inning >= INNINGS_PER_GAME and sides[True].score > sides[False].score

    def _half_inning(self, plate_appearances, sides, inning, is_bottom, limit):
        """半回ぶんの打席を積む。3アウトか、サヨナラで終わる。"""
        batting, fielding = sides[is_bottom], sides[not is_bottom]
        occupied: dict = {}
        outs = 0

        while outs < OUTS_PER_INNING:
            self._maybe_change_pitcher(fielding, sides, inning, limit)
            batter = self._next_batter(batting, inning, limit)
            entry = self._plate_appearance(
                sequence=len(plate_appearances) + 1,
                inning=inning,
                is_bottom=is_bottom,
                batter=batter,
                fielding=fielding,
                occupied=occupied,
                outs=outs,
            )
            plate_appearances.append(entry)

            outs += entry.outs_recorded
            fielding.pitcher.outs += entry.outs_recorded
            batting.score += entry.runs_scored

            # サヨナラ。決勝点が入った時点で終わる
            if is_bottom and inning >= INNINGS_PER_GAME and batting.score > fielding.score:
                return

    # --- 出場選手 ---

    def _set_lineup(self, side, limit):
        """打順を決める。レギュラーを基本にし、一部を控えと入れ替える。

        控えは序列の上位から選ばれやすい。均等に散らすと1人あたりの打席が
        少なすぎて、極端な率の選手が一覧の上位に並ぶ。

        外国人枠を超える選手は控えと入れ替える。ORM へ直接書くコードは
        サイトからの入力で行われる検査を素通りするため、ここで同じ規則に従わせる。
        """
        roster = side.roster
        bench = roster["bench"]
        swap = self.rng.random(len(roster["regulars"])) < BENCH_RATIO

        for order, (regular, replaced) in enumerate(zip(roster["regulars"], swap, strict=True), start=1):
            player = regular
            if replaced and bench:
                picked = bench[self.rng.choice(len(bench), p=roster["bench_weights"])]
                if picked.id not in side.used_player_ids and self._quota_allows(side, picked, limit):
                    player = picked
            if player.id in side.used_player_ids:
                player = regular
            player = self._within_quota(side, player, bench, limit)
            entry = _Batter(
                player=player,
                batting_order=order,
                slot_sequence=0,
                # 控えが先発する場合はレギュラーの守備位置を引き継ぐ
                fielding_position=roster["positions"].get(player.id, roster["positions"].get(regular.id))
                or FieldingPosition.DESIGNATED_HITTER,
                talent=roster["batting_talent"][player.id],
            )
            self._join(side, entry)

    @classmethod
    def _within_quota(cls, side, wanted, alternatives, limit):
        """外国人枠に収まる選手を返す。収まらなければ控えから代わりを探す。

        代わりが見つからないロスター（外国人ばかりのチーム）では、そのまま返す。
        """
        if cls._quota_allows(side, wanted, limit):
            return wanted
        for other in alternatives:
            if other.id not in side.used_player_ids and cls._quota_allows(side, other, limit):
                return other
        return wanted

    def _next_batter(self, side, inning, limit):
        """次の打者。打順は1〜9を巡回する（スコアブックを横に読む性質そのもの）。"""
        slot = side.order_index
        side.order_index = (slot + 1) % LINEUP_SIZE
        self._maybe_pinch_hit(side, slot, inning, limit)
        return side.lineup[slot]

    def _maybe_pinch_hit(self, side, slot, inning, limit):
        """終盤に代打を送る。スタメンと途中出場の区別がボックススコアに出る。"""
        if inning < PINCH_HITTER_FROM_INNING or side.pinch_hitters >= MAX_PINCH_HITTERS:
            return
        if self.rng.random() >= PINCH_HITTER_RATIO:
            return
        bench = [p for p in side.roster["bench"] if p.id not in side.used_player_ids]
        if not bench:
            return
        picked = bench[int(self.rng.integers(0, len(bench)))]
        if not self._quota_allows(side, picked, limit):
            return

        current = side.lineup[slot]
        side.pinch_hitters += 1
        self._join(
            side,
            _Batter(
                player=picked,
                batting_order=current.batting_order,
                slot_sequence=current.slot_sequence + 1,
                fielding_position=FieldingPosition.PINCH_HITTER,
                talent=side.roster["batting_talent"][picked.id],
            ),
            slot=slot,
        )

    def _join(self, side, entry, *, slot=None):
        """選手を打順の枠に入れる。出場した選手はボックススコアに残す。"""
        index = entry.batting_order - 1 if slot is None else slot
        while len(side.lineup) <= index:
            side.lineup.append(None)
        side.lineup[index] = entry
        side.appearances.append(entry)
        side.used_player_ids.add(entry.player.id)
        if entry.player.id in side.roster["foreign"]:
            side.foreign_used += 1

    @staticmethod
    def _quota_allows(side, player, limit):
        """外国人選手の出場枠に収まるか。

        枠は「1試合に出場できる外国人選手の上限」で、リーグが持つ。サイトからの
        入力では検査されるので、投入データも同じ規則に従わせる。
        """
        if limit is None or player.id not in side.roster["foreign"]:
            return True
        return side.foreign_used < limit

    # --- 継投 ---

    def _take_mound(self, side, start_index, limit):
        """先発をマウンドに送る。ローテーションを順に回す。"""
        rotation = side.roster["rotation"]
        starter = rotation[start_index % len(rotation)]
        starter = self._within_quota(side, starter, rotation + side.roster["bullpen"], limit)
        innings = int(self.rng.integers(*STARTER_INNINGS, endpoint=True))
        self._add_outing(side, starter, inning=1, target_outs=innings * OUTS_PER_INNING)

    def _add_outing(self, side, player, inning, target_outs):
        side.outings.append(
            _Outing(
                player=player,
                appearance_order=len(side.outings) + 1,
                entered_inning=inning,
                skill=side.roster["pitching_talent"][player.id],
                # 交代を回の途中でも起こす。3の倍数のままだと投球回が常に X.0 になる
                target_outs=max(1, target_outs - self._mid_inning_shave()),
            )
        )
        side.used_player_ids.add(player.id)
        if player.id in side.roster["foreign"]:
            side.foreign_used += 1

    def _mid_inning_shave(self):
        if self.rng.random() >= MID_INNING_CHANGE_RATIO:
            return 0
        return int(self.rng.integers(1, OUTS_PER_INNING))

    def _maybe_change_pitcher(self, side, sides, inning, limit):
        """受け持ちのアウト数に達していたら次の投手に代える。"""
        if side.pitcher.outs < side.pitcher.target_outs:
            return
        lead = side.score - sides[not side.is_home].score
        nxt = self._pick_reliever(side, inning, lead, limit)
        if nxt is None:
            # 投げられる投手がいなければ続投する（投手のいない回を作らない）
            side.pitcher.target_outs += OUTS_PER_INNING
            return
        self._add_outing(side, nxt, inning, int(self.rng.integers(*RELIEVER_OUTS, endpoint=True)))

    def _pick_reliever(self, side, inning, lead, limit):
        """次に投げる救援。最終回の接戦は抑えが締める。

        抑えを固定しないとセーブが数人に分散し、1人で年30セーブという実際の
        形にならない。中継ぎは序列の上位から選ばれやすい。
        """
        bullpen = [
            p
            for p in side.roster["bullpen"]
            if p.id not in side.used_player_ids and self._quota_allows(side, p, limit)
        ]
        if not bullpen:
            return None

        closer = side.roster["bullpen"][0]
        if closer in bullpen and inning >= INNINGS_PER_GAME:
            usage = CLOSER_USAGE_IN_SAVE_SITUATION if 0 < lead <= SAVE_LEAD_LIMIT else CLOSER_USAGE_OTHERWISE
            if self.rng.random() < usage:
                return closer

        # 抑えは中継ぎの候補には入れない。候補に残すと序列1位ゆえ中継ぎとしても
        # 頻繁に選ばれ、投球回が実際の抑えの倍を超えてしまう
        pool = [p for p in bullpen if p is not closer] or bullpen
        return pool[int(self.rng.choice(len(pool), p=_depth_weights(len(pool), BULLPEN_DECAY)))]

    # --- 1打席 ---

    def _plate_appearance(self, *, sequence, inning, is_bottom, batter, fielding, occupied, outs):
        """1打席ぶんの記録を作り、塁の状態を進める。

        結果を先に引き、そのうえで走者の動きを組み立てる。**走者は先の塁から順に**
        動かし、`occupied` をその場で書き換える（一塁走者を先に動かすと、二塁走者が
        まだ居るために行き先が塞がって見える）。
        """
        pitcher = fielding.pitcher
        result = self._draw_result(batter.talent, pitcher.skill, occupied, outs)
        advances = []
        errors = []

        # 盗塁は打球が飛ばない打席でだけ。理由は STEAL_ATTEMPT_RATIO の説明を参照
        if result in (P.WALK, P.INTENTIONAL_WALK, P.HIT_BY_PITCH) or result.is_strikeout:
            self._maybe_steal(advances, occupied, outs)

        if result is P.REACHED_ON_ERROR:
            errors.append(self._draw_error(fielding))

        self._build_advances(advances, result, batter.player.id, occupied, outs)

        return PlateAppearance(
            sequence=sequence,
            inning=inning,
            is_bottom=is_bottom,
            batter_id=batter.player.id,
            pitcher_id=pitcher.player.id,
            batting_order=batter.batting_order,
            slot_sequence=batter.slot_sequence,
            result=result,
            fielded_by=self._fielded_by(result),
            advances=advances,
            errors=errors,
        )

    def _draw_result(self, talent, skill, occupied, outs):
        """1打席の結果を引く。

        判定の順は「死球 → 四球 → 送りバント → 犠飛 → 打数」。打数に数えない結果を
        先に落とすことで、残りが打数になり、**そこでの安打確率が打者の能力そのもの**に
        なる（打率の水準が能力の平均から動かない）。
        """
        contact, power, walk_rate = talent
        # 投手の力量は倍率（大きいほど打たれる）。指数で鈍らせて効かせる
        hit_chance = float(np.clip(contact * skill**PITCHER_CONTACT_ELASTICITY, 0.05, 0.60))
        walk_chance = float(np.clip(walk_rate * skill**PITCHER_WALK_ELASTICITY, 0.01, 0.30))

        if self.rng.random() < BATTER_HIT_BY_PITCH_PER_PA:
            return P.HIT_BY_PITCH
        if self.rng.random() < walk_chance:
            return P.INTENTIONAL_WALK if self.rng.random() < INTENTIONAL_WALK_SHARE else P.WALK

        on_first_or_second = Base.FIRST in occupied or Base.SECOND in occupied
        if (
            outs < OUTS_PER_INNING - 1
            and on_first_or_second
            and Base.THIRD not in occupied
            and self.rng.random() < SACRIFICE_BUNT_RATIO
        ):
            return P.SACRIFICE_BUNT
        if outs < OUTS_PER_INNING - 1 and Base.THIRD in occupied and self.rng.random() < SACRIFICE_FLY_RATIO:
            return P.SACRIFICE_FLY

        # ここからが打数
        if self.rng.random() < hit_chance:
            return self._draw_hit(power)
        return self._draw_out(skill, occupied)

    def _draw_hit(self, power):
        """安打の種類。本塁打・二塁打・三塁打の順に、残りから引き算して決める。"""
        if self.rng.random() < power:
            return P.HOME_RUN
        if self.rng.random() < DOUBLE_SHARE:
            return P.DOUBLE
        if self.rng.random() < TRIPLE_SHARE:
            return P.TRIPLE
        return P.SINGLE

    def _draw_out(self, skill, occupied):
        """安打にならなかった打数の結果。"""
        roll = self.rng.random()
        if roll < REACHED_ON_ERROR_SHARE:
            return P.REACHED_ON_ERROR
        # 野選は封殺できる走者がいるときだけ。三塁に走者がいると得点が絡んで
        # 打点の扱いが分かれるため、その場合は選ばない
        if (
            Base.FIRST in occupied
            and Base.THIRD not in occupied
            and roll < REACHED_ON_ERROR_SHARE + FIELDERS_CHOICE_SHARE
        ):
            return P.FIELDERS_CHOICE

        strikeout_chance = float(np.clip(STRIKEOUT_SHARE / skill**STRIKEOUT_BOOST, 0.0, 1.0))
        if self.rng.random() < strikeout_chance:
            return P.STRIKEOUT_LOOKING if self.rng.random() < LOOKING_STRIKEOUT_SHARE else P.STRIKEOUT_SWINGING

        batted = self.rng.random()
        if batted < GROUND_OUT_SHARE:
            return P.GROUND_OUT
        if batted < GROUND_OUT_SHARE + FLY_OUT_SHARE:
            return P.FLY_OUT
        if batted < GROUND_OUT_SHARE + FLY_OUT_SHARE + LINE_OUT_SHARE:
            return P.LINE_OUT
        return P.FOUL_FLY_OUT

    def _maybe_steal(self, advances, occupied, outs):
        """一塁走者の盗塁。二塁が空いていて、2アウト未満のときだけ試みる。

        2アウトで試みないのは、盗塁刺と打者のアウトが重なると1つの半回で
        アウトが4つになるため（打者はもう打席を終えている）。
        """
        runner = occupied.get(Base.FIRST)
        if runner is None or Base.SECOND in occupied:
            return
        if outs >= OUTS_PER_INNING - 1 or self.rng.random() >= STEAL_ATTEMPT_RATIO:
            return

        if self.rng.random() < STEAL_SUCCESS_RATIO:
            advances.append(RunnerAdvance(runner, Base.FIRST, Base.SECOND, R.STOLEN_BASE))
        else:
            advances.append(RunnerAdvance(runner, Base.FIRST, Base.OUT, R.CAUGHT_STEALING))
        # 盗塁の結果は打者の進塁を組み立てる前に反映する（押し出しの判定が変わる）
        occupied.pop(Base.FIRST, None)
        if advances[-1].to_base.occupies_base:
            occupied[Base.SECOND] = runner

    def _draw_error(self, fielding):
        """失策を記録する守備者を選ぶ。守備に就いている選手から引く。"""
        fielders = [
            entry for entry in fielding.lineup if entry is not None and not entry.fielding_position.is_substitute_only
        ]
        fielders = [e for e in fielders if e.fielding_position is not FieldingPosition.DESIGNATED_HITTER]
        kinds = list(ERROR_KIND_WEIGHTS)
        kind = kinds[int(self.rng.choice(len(kinds), p=list(ERROR_KIND_WEIGHTS.values())))]
        if not fielders:
            return FieldingError(fielding.pitcher.player.id, FieldingPosition.PITCHER, kind)
        picked = fielders[int(self.rng.integers(0, len(fielders)))]
        return FieldingError(picked.player.id, picked.fielding_position, kind)

    @staticmethod
    def _fielded_by(result):
        """打球の処理経路。刺殺・補殺の出典になる（守備成績は P6 で画面に出す）。

        打球方向まで乱数で散らす必要は今のところ無いので、結果ごとに代表的な
        経路を1つ当てる。刺殺・補殺を画面に出すときに細かくする。
        """
        if result in (P.GROUND_OUT, P.FIELDERS_CHOICE):
            return (FieldingPosition.SHORTSTOP, FieldingPosition.FIRST_BASE)
        if result in (P.FLY_OUT, P.SACRIFICE_FLY):
            return (FieldingPosition.CENTER_FIELD,)
        if result is P.LINE_OUT:
            return (FieldingPosition.THIRD_BASE,)
        if result is P.FOUL_FLY_OUT:
            return (FieldingPosition.CATCHER,)
        if result is P.SACRIFICE_BUNT:
            return (FieldingPosition.PITCHER, FieldingPosition.FIRST_BASE)
        return ()

    # --- 走者の動き ---

    def _build_advances(self, advances, result, batter_id, occupied, outs):
        """打席の結果に応じて、走者と打者の進塁を組み立てる。

        走者を動かすたびに `occupied` を書き換えるので、後続の走者からは
        前を走る走者が空けた塁が空いて見える。
        """
        if result is P.HOME_RUN:
            self._send_home(advances, occupied)
            self._batter_to(advances, batter_id, Base.HOME, R.BATTED_BALL, occupied)
            return

        if result.is_hit:
            self._advance_on_hit(advances, result, occupied)
            self._batter_to(advances, batter_id, result.default_batter_base, R.BATTED_BALL, occupied)
            return

        if result in (P.WALK, P.INTENTIONAL_WALK, P.HIT_BY_PITCH):
            self._push_forced(advances, occupied)
            self._batter_to(advances, batter_id, Base.FIRST, R.AWARDED_BASE, occupied)
            return

        if result is P.REACHED_ON_ERROR:
            self._advance_all(advances, occupied, R.ERROR, error_index=0)
            self._batter_to(advances, batter_id, Base.FIRST, R.ERROR, occupied, error_index=0)
            return

        if result is P.FIELDERS_CHOICE:
            self._put_out(advances, occupied, Base.FIRST, R.FORCE_OUT)
            self._batter_to(advances, batter_id, Base.FIRST, R.FIELDERS_CHOICE, occupied)
            return

        if result is P.SACRIFICE_BUNT:
            self._advance_all(advances, occupied, R.BATTED_BALL)
            self._batter_to(advances, batter_id, Base.OUT, R.PUT_OUT, occupied)
            return

        if result is P.SACRIFICE_FLY:
            runner = occupied.pop(Base.THIRD)
            advances.append(RunnerAdvance(runner, Base.THIRD, Base.HOME, R.TAG_UP))
            self._batter_to(advances, batter_id, Base.OUT, R.PUT_OUT, occupied)
            return

        if result is P.GROUND_OUT:
            self._ground_out(advances, batter_id, occupied, outs)
            return

        # 三振・フライ・ライナー・邪飛。走者は動かない
        self._batter_to(advances, batter_id, Base.OUT, R.PUT_OUT, occupied)

    def _ground_out(self, advances, batter_id, occupied, outs):
        """ゴロアウト。一塁に走者がいれば併殺になることがある。"""
        if Base.FIRST in occupied and outs < OUTS_PER_INNING - 1 and self.rng.random() < DOUBLE_PLAY_RATIO:
            self._put_out(advances, occupied, Base.FIRST, R.FORCE_OUT)
        elif self.rng.random() < ADVANCE_ON_GROUND_OUT_RATIO:
            self._advance_all(advances, occupied, R.BATTED_BALL)
        self._batter_to(advances, batter_id, Base.OUT, R.PUT_OUT, occupied)

    def _advance_on_hit(self, advances, result, occupied):
        """安打での走者の動き。先の塁の走者から順に決める。"""
        for base in reversed(Base.occupiable()):
            if base in occupied:
                self._place(advances, base, self._destination_on_hit(result, base), occupied, R.BATTED_BALL)

    def _destination_on_hit(self, result, base):
        """その安打で走者がどこまで行くか（上限）。"""
        if result is P.TRIPLE:
            return Base.HOME
        if result is P.DOUBLE:
            if base is Base.FIRST:
                return Base.HOME if self.rng.random() < SCORE_ON_DOUBLE_FROM_FIRST else Base.THIRD
            return Base.HOME
        # 単打
        if base is Base.THIRD:
            return Base.HOME
        if base is Base.SECOND:
            return Base.HOME if self.rng.random() < SCORE_ON_SINGLE_FROM_SECOND else Base.THIRD
        return Base.THIRD if self.rng.random() < EXTRA_BASE_ON_SINGLE_FROM_FIRST else Base.SECOND

    def _advance_all(self, advances, occupied, reason, *, error_index=None):
        """塁上の走者を1つずつ進める。詰まっていれば進めない走者も出る。"""
        for base in reversed(Base.occupiable()):
            if base in occupied:
                self._place(advances, base, Base(base.value + 1), occupied, reason, error_index)

    @staticmethod
    def _push_forced(advances, occupied):
        """打者が一塁を与えられたときの押し出し。詰まっている走者だけが進む。"""
        for base in reversed(_forced_bases(occupied)):
            runner = occupied.pop(base)
            target = Base(base.value + 1)
            advances.append(RunnerAdvance(runner, base, target, R.FORCED))
            if target.occupies_base:
                occupied[target] = runner

    @staticmethod
    def _send_home(advances, occupied):
        for base in reversed(Base.occupiable()):
            if base in occupied:
                advances.append(RunnerAdvance(occupied.pop(base), base, Base.HOME, R.BATTED_BALL))

    @staticmethod
    def _put_out(advances, occupied, base, reason):
        advances.append(RunnerAdvance(occupied.pop(base), base, Base.OUT, reason))

    @staticmethod
    def _batter_to(advances, batter_id, target, reason, occupied, error_index=None):
        advances.append(RunnerAdvance(batter_id, Base.BATTER, target, reason, error_index=error_index))
        if target.occupies_base:
            occupied[target] = batter_id

    @staticmethod
    def _place(advances, base, wanted, occupied, reason, error_index=None):
        """走者を空いている塁まで進める。先を走る走者が止まっていれば手前で止まる。

        前を走る走者から順に呼ぶので、その走者が空けた塁はもう `occupied` に無い。
        隣の塁まで塞がっている走者は動かない（記録も残さない）。
        """
        target = wanted
        while target.occupies_base and target in occupied:
            if target.value <= base.value + 1:
                return  # これ以上手前には下がれない。進塁できない
            target = Base(target.value - 1)
        runner = occupied.pop(base)
        advances.append(RunnerAdvance(runner, base, target, reason, error_index=error_index))
        if target.occupies_base:
            occupied[target] = runner

    # --- 明細（打席から導く） ---

    def _batting_lines(self, sides, plate_appearances):
        """打撃成績を打席から導く。数えるのはドメインのサービスに任せる。"""
        return [
            {
                "player": entry.player,
                "batting_order": entry.batting_order,
                "slot_sequence": entry.slot_sequence,
                "fielding_position": entry.fielding_position,
                "line": domain_services.batting_line_for(plate_appearances, entry.player.id),
            }
            for side in sides.values()
            for entry in side.appearances
        ]

    def _pitching_lines(self, game, sides, plate_appearances):
        """投球成績を打席から導き、勝敗・セーブ・ホールドをドメインに決めさせる。

        判定はイニングスコアと継投から一意に決まるので、手動入力と同じ
        ドメインサービスに委ねる。ここで独自に書くと2つの実装がずれる。
        """
        team_of = {}
        derived = {}
        for side in sides.values():
            for outing in side.outings:
                # 打席を投手ごとに数え直すのは重いので1度だけ行う
                derived[outing.player.id] = domain_services.pitching_line_for(plate_appearances, outing.player.id)
                team_of[outing.player.id] = side.team.id
                game.record_pitching(
                    outing.player.id,
                    derived[outing.player.id],
                    appearance_order=outing.appearance_order,
                    entered_inning=outing.entered_inning,
                )

        decisions = domain_services.pitching_decisions(game, team_of)
        rows = []
        for side in sides.values():
            for outing in side.outings:
                wins = decisions.wins_for(outing.player.id)
                rows.append(
                    {
                        "player": outing.player,
                        "appearance_order": outing.appearance_order,
                        "entered_inning": outing.entered_inning,
                        # **打席から数えた行に、継投で決まる記録だけを重ねる。**
                        # 組み立て直すと、項目を増やしたときにここだけ古くなる
                        "line": replace(
                            derived[outing.player.id],
                            starts=1 if outing.appearance_order == 1 else 0,
                            wins=wins,
                            losses=decisions.losses_for(outing.player.id),
                            saves=decisions.saves_for(outing.player.id),
                            holds=decisions.holds_for(outing.player.id),
                            relief_wins=wins if outing.appearance_order > 1 else 0,
                        ),
                    }
                )
        return rows

    # --- 保存 ---

    def _save(self, year, played):
        """ためた試合を DB に流す。打席 → 進塁・失策の順に入れる。"""
        batting_rows, pitching_rows, inning_rows, pa_rows = [], [], [], []
        for record in played:
            game = record["game"]
            row = Game.objects.create(
                year=year,
                played_on=game.played_on,
                home_team=record["card"]["home"],
                away_team=record["card"]["away"],
                home_score=game.home_score,
                away_score=game.away_score,
            )
            game.id = row.id
            batting_rows.extend(self._batting_orm_rows(row, record))
            pitching_rows.extend(self._pitching_orm_rows(row, record))
            inning_rows.extend(self._inning_orm_rows(row, game))
            pa_rows.extend(self._plate_appearance_orm_rows(row, game))
            self._count(record)

        GameBattingLine.objects.bulk_create(batting_rows, batch_size=500)
        GamePitchingLine.objects.bulk_create(pitching_rows, batch_size=500)
        GameInningScore.objects.bulk_create(inning_rows, batch_size=500)
        GamePlateAppearance.objects.bulk_create(pa_rows, batch_size=500)

        self._save_advances_and_errors(played)

    @staticmethod
    def _save_advances_and_errors(played):
        """進塁と失策を入れる。打席の主キーは入れ終えてから読み直す。

        bulk_create が主キーを埋めるかは DB に依存するため、(試合, 通し番号) で
        引き直して対応づける。
        """
        game_ids = [record["game"].id for record in played]
        saved = {
            (row.game_id, row.sequence): row.id
            for row in GamePlateAppearance.objects.filter(game_id__in=game_ids).only("id", "game_id", "sequence")
        }

        advance_rows, error_rows = [], []
        for record in played:
            game = record["game"]
            for entry in game.plate_appearances:
                pa_id = saved[(game.id, entry.sequence)]
                entry.id = pa_id
                advance_rows.extend(
                    GameRunnerAdvance(
                        plate_appearance_id=pa_id,
                        runner_id=advance.runner_id,
                        from_base=advance.from_base.value,
                        to_base=advance.to_base.value,
                        reason=advance.reason.value,
                        error_index=advance.error_index,
                    )
                    for advance in entry.advances
                )
                error_rows.extend(
                    GameFieldingError(
                        plate_appearance_id=pa_id,
                        player_id=error.player_id,
                        position=error.position.value,
                        kind=error.kind.value,
                    )
                    for error in entry.errors
                )

        GameRunnerAdvance.objects.bulk_create(advance_rows, batch_size=1000)
        GameFieldingError.objects.bulk_create(error_rows, batch_size=1000)

    @staticmethod
    def _plate_appearance_orm_rows(row, game):
        for entry in game.plate_appearances_in_order():
            yield GamePlateAppearance(
                game=row,
                sequence=entry.sequence,
                inning=entry.inning,
                is_bottom=entry.is_bottom,
                batter_id=entry.batter_id,
                pitcher_id=entry.pitcher_id,
                batting_order=entry.batting_order,
                slot_sequence=entry.slot_sequence,
                result=entry.result.value,
                fielded_by="-".join(position.value for position in entry.fielded_by),
            )

    @staticmethod
    def _batting_orm_rows(row, record):
        """打撃成績の行。**項目は値オブジェクトから引く。**

        ここに項目名を並べると、`BattingLine` に足しても投入コマンドだけが古いまま
        になり、その項目が 0 のまま保存される（実際に起きた。例外にならないので
        画面の欄が空になるまで気づけない）。
        """
        for entry in record["batting"]:
            yield GameBattingLine(
                game=row,
                player=entry["player"],
                batting_order=entry["batting_order"],
                slot_sequence=entry["slot_sequence"],
                fielding_position=entry["fielding_position"].value,
                **{f.name: getattr(entry["line"], f.name) for f in fields(BattingLine)},
            )

    @staticmethod
    def _pitching_orm_rows(row, record):
        """投球成績の行。打撃と同じく項目は値オブジェクトから引く。

        投球回は野球表記（5.2 = 5回と2/3）に直して1列で持ち、先発登板と救援勝利は
        登板順から導くので列に持たない。この3つだけを除く。
        """
        for entry in record["pitching"]:
            line = entry["line"]
            yield GamePitchingLine(
                game=row,
                player=entry["player"],
                appearance_order=entry["appearance_order"],
                entered_inning=entry["entered_inning"],
                innings_pitched=float(line.innings.to_notation()),
                **{f.name: getattr(line, f.name) for f in fields(PitchingLine) if f.name not in PITCHING_NOT_STORED},
            )

    @staticmethod
    def _inning_orm_rows(row, game):
        score = game.line_score
        for is_home, values in ((False, score.away), (True, score.home)):
            for index, runs in enumerate(values, start=1):
                yield GameInningScore(game=row, inning=index, is_home=is_home, runs=runs)

    # --- 報告 ---

    def _count(self, record):
        """投入した水準を数える。NPB の値と並べて確認するために使う。"""
        game = record["game"]
        self.totals["games"] += 1
        self.totals["runs"] += game.home_score + game.away_score
        self.totals["plate_appearances"] += len(game.plate_appearances)
        self.totals["advances"] += sum(len(entry.advances) for entry in game.plate_appearances)
        self.totals["errors"] += sum(len(entry.errors) for entry in game.plate_appearances)
        for entry in record["batting"]:
            line = entry["line"]
            self.totals["at_bats"] += line.at_bats
            self.totals["hits"] += line.hits
            self.totals["home_runs"] += line.home_runs
            self.totals["walks"] += line.walks
            self.totals["sacrifice_bunts"] += line.sacrifice_bunts
            self.totals["stolen_bases"] += line.stolen_bases
            self.totals["caught_stealing"] += line.caught_stealing
            self.totals["double_plays"] += line.double_plays
        for entry in record["pitching"]:
            line = entry["line"]
            self.totals["outs"] += line.innings.outs
            self.totals["earned_runs"] += line.earned_runs
            self.totals["runs_allowed"] += line.runs_allowed
            self.totals["strikeouts"] += line.strikeouts

    def _report_schedule(self, schedule):
        counts = defaultdict(int)
        for card in schedule:
            counts[card["league"].name] += 1
        for league_name, count in sorted(counts.items()):
            self._say(f"  {league_name}: {count}試合")

    def _report_levels(self):
        """リーグ全体の水準。NPB の目安と並べて、投入したデータの現実味を見る。"""
        games = max(1, self.totals["games"])
        at_bats = max(1, self.totals["at_bats"])
        innings = max(1.0, self.totals["outs"] / OUTS_PER_INNING)

        per_team_game = games * 2
        rows = [
            ("1試合平均得点", self.totals["runs"] / per_team_game, 3.9),
            ("リーグ打率", self.totals["hits"] / at_bats, 0.255),
            ("リーグ防御率", self.totals["earned_runs"] * 9 / innings, 3.50),
            ("K/9", self.totals["strikeouts"] * 9 / innings, 7.5),
            ("BB/9", self.totals["walks"] * 9 / innings, 2.7),
            ("HR/9", self.totals["home_runs"] * 9 / innings, 0.9),
            ("1試合の失策", self.totals["errors"] / games, 1.2),
            # 打席から数えられるようになった項目。1チーム1試合あたりで見る
            ("犠打", self.totals["sacrifice_bunts"] / per_team_game, 0.6),
            ("盗塁", self.totals["stolen_bases"] / per_team_game, 0.55),
            ("盗塁刺", self.totals["caught_stealing"] / per_team_game, 0.2),
            ("併殺打", self.totals["double_plays"] / per_team_game, 0.7),
            ("失点", self.totals["runs_allowed"] / per_team_game, 3.9),
        ]
        self._say("  リーグ全体の水準（括弧内は NPB の目安）:")
        for label, value, target in rows:
            self._say(f"    {label}: {value:.3f}（{target}）")
