"""仮想の試合データを投入する。

チームと選手が登録済みであることを前提に、リーグ内の総当たりで1シーズンぶんの
試合を作り、出場選手の打撃・投球成績まで埋める。順位表・対戦成績・月別成績・
タイトル・ボックススコアといった画面を、データのある状態で確認するためのもの。

成績は**確率分布から引く**（numpy）。「1打数ずつ乱数を引いて数える」ループは
二項分布そのもので、分布として扱えば1回の呼び出しで済み、意図も明示できる。

| 対象 | 分布 | なぜ |
| --- | --- | --- |
| 1試合の得点 | 負の二項分布 | 得点は平均より分散が大きい（大差の試合がある）。ポアソンでは裾が足りない |
| 安打数 | 二項分布 | 「打数ぶん試して安打が出る回数」そのもの |
| 内訳・打点・回ごとの得点の配分 | 多項分布 | 決まった総数を複数の受け皿に配る |
| 被本塁打の割り当て | 多変量超幾何分布 | 被安打という有限の山から本塁打を選ぶので、被安打を超えない |
| 打者の能力 | 多変量正規分布 | 打率・長打力・四球率には相関がある（強打者は歩かされる） |
| 投手の能力 | 対数正規分布 | 失点の倍率なので正の値だけを取り、右に裾を引く |

**明細どうしが食い違わないように作る。** 乱数で別々に決めると、5-3 の試合なのに
打点の合計が 0 だったり、リーグ全体の本塁打と被本塁打が一致しなかったりする。
実際のスコアブックと同じく、片方から他方を導く。

- 打点の合計 ＝ その試合の得点
- **投手の被安打・被本塁打・与四死球 ＝ 相手打線が記録した安打・本塁打・四死球**
- 投手の失点 ＝ その投手が投げた回に相手が挙げた得点

**回ごとの得点経過を持つ。** 勝敗・セーブ・ホールドはNPBの公式ルールで決まり、
どの条件も「継投した時点のスコア」を見る必要がある。最終スコアだけでは
「3点差以内で登板して抑えた」を判定できない。

**得点はチームの戦力に応じて増減させる。** 全チームを同じ分布から引くと、順位が
完全な運任せになり、順位表とチーム打率・チーム防御率が噛み合わなくなる。

**出場選手は毎試合ロスターから選び直さず、チームごとに決めたレギュラーと
先発ローテーションを軸にする。** 全員を均等に出場させると誰も規定打席・規定
投球回に届かず、タイトルや率のランキングが空になってしまう。

試合日は4月から9月に散らす。1日に固めると月別成績が1行しか出ず、
月ごとの推移を確認できない。
"""

from collections import defaultdict
from datetime import date, timedelta

import numpy as np
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from myapp.domain import services as domain_services
from myapp.domain.entities import Game as DomainGame
from myapp.domain.value_objects import (
    BattingLine,
    FieldingPosition,
    InningsPitched,
    LineScore,
    PitchingLine,
    Position,
    Season,
)
from myapp.models import (
    Game,
    GameBattingLine,
    GameInningScore,
    GamePitchingLine,
    League,
    PlayerStint,
)

# --- 現実的な数値に寄せるための調整値 ---
#
# 目標はNPBの近年の水準。1試合平均得点 3.9、リーグ打率 .255、リーグ防御率 3.5、
# WHIP 1.25、K/9 7.5、BB/9 2.7、9回あたり被本塁打 0.9 あたりに落ち着く。

INNINGS_PER_GAME = 9
OUTS_PER_INNING = 3

# 1試合の得点。負の二項分布の平均と形状。形状が小さいほど裾が長い（大差の試合が増える）。
RUNS_MEAN = 3.9
RUNS_SHAPE = 4.0

# 同点のまま引分で終わる確率。両チームの得点を独立に引くと1割以上が同点になるが、
# 実際は延長で決着する試合が大半で、NPBの引分は年5試合前後（3〜4%）。
TIE_SURVIVAL_RATIO = 0.30

# 自責点は失点のうち何割か。残りは失策がらみの「自責点にならない失点」。
EARNED_RUN_RATIO = 0.90

# チーム戦力が得点に効く強さ。0 なら戦力を無視（順位が完全な運任せ）、
# 1 なら戦力比をそのまま得点比にする。NPBのチーム得点は平均の±15%程度に
# 収まるので、能力差をそのまま反映させると開きすぎる。
OFFENSE_ELASTICITY = 0.55
DEFENSE_ELASTICITY = 0.55

# 先発が受け持つ回。残りは救援で分ける。
STARTER_INNINGS = (5, 7)
# 継投が回の途中で起きる確率と、その際に次の投手へ渡すアウト数。
MID_INNING_CHANGE_RATIO = 0.40

# 打順に並べる人数と、1試合の打数。打数は 3 + Binomial(2, 0.35) で平均 3.7。
LINEUP_SIZE = 9
AT_BATS_BASE = 3
AT_BATS_EXTRA_TRIALS = 2
AT_BATS_EXTRA_RATIO = 0.35

# 代打の出る試合の割合と、1試合に出る人数の上限。
PINCH_HITTER_RATIO = 0.55
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
BATTER_MEANS = np.array([0.242, 0.085, 0.062])
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
# 奪三振に能力を反映させる強さ。1.0 のまま割ると、能力 0.5 の投手が
# K/9 15 という非現実的な値になる。
STRIKEOUT_BOOST = 0.50

# 1アウトあたりの奪三振。9回換算で 7.6 になる値。被安打・与四球は相手打線から
# 導くのでここには持たない。
STRIKEOUTS_PER_OUT = 0.256
BATTER_HIT_BY_PITCH_RATIO = 0.015

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
        plans = self._plans(year, per_pair, rosters)

        if not plans:
            raise CommandError(
                "試合を作れるリーグがありません。"
                "同じリーグに2チーム以上あり、各チームに投手と野手が登録されている必要があります。"
            )

        if options["dry_run"]:
            self.stdout.write(f"{year}年 · {len(plans)}試合を投入します（--dry-run のため未実行）")
            self._report(plans)
            return

        with transaction.atomic():
            if options["replace"] and existing.exists():
                removed = existing.count()
                existing.delete()  # 明細は on_delete=CASCADE で一緒に消える
                self.stdout.write(f"{year}年の既存の試合 {removed} 件を削除しました。")

            batting_rows, pitching_rows, inning_rows = [], [], []
            for plan in plans:
                game = Game.objects.create(
                    year=year,
                    played_on=plan["played_on"],
                    home_team=plan["home"],
                    away_team=plan["away"],
                    home_score=plan["home_score"],
                    away_score=plan["away_score"],
                )
                batting_rows.extend(self._batting_rows(game, plan))
                pitching_rows.extend(self._pitching_rows(game, plan))
                inning_rows.extend(self._inning_rows(game, plan))

            GameBattingLine.objects.bulk_create(batting_rows, batch_size=500)
            GamePitchingLine.objects.bulk_create(pitching_rows, batch_size=500)
            GameInningScore.objects.bulk_create(inning_rows, batch_size=500)

        self.stdout.write(
            self.style.SUCCESS(
                f"{year}年 · {len(plans)}試合を投入しました"
                f"（打撃 {len(batting_rows)}件 / 投球 {len(pitching_rows)}件）"
            )
        )
        self._report(plans)

    @staticmethod
    def _batting_rows(game, plan):
        for entry in plan["batting"]:
            line = entry["line"]
            yield GameBattingLine(
                game=game,
                player=entry["player"],
                batting_order=entry["batting_order"],
                slot_sequence=entry["slot_sequence"],
                fielding_position=entry["fielding_position"].value,
                at_bats=line.at_bats,
                singles=line.singles,
                doubles=line.doubles,
                triples=line.triples,
                home_runs=line.home_runs,
                runs_batted_in=line.runs_batted_in,
                walks=line.walks,
                hit_by_pitch=line.hit_by_pitch,
                sacrifice_flies=line.sacrifice_flies,
            )

    @staticmethod
    def _inning_rows(game, plan):
        score = plan["line_score"]
        for is_home, values in ((False, score.away), (True, score.home)):
            for index, runs in enumerate(values, start=1):
                yield GameInningScore(game=game, inning=index, is_home=is_home, runs=runs)

    @staticmethod
    def _pitching_rows(game, plan):
        for entry in plan["pitching"]:
            line = entry["line"]
            yield GamePitchingLine(
                game=game,
                player=entry["player"],
                appearance_order=entry["appearance_order"],
                entered_inning=entry["segment"]["innings"][0],
                # 保存は野球表記（5.2 = 5回と2/3）。変換は値オブジェクトに任せる
                innings_pitched=float(line.innings.to_notation()),
                wins=line.wins,
                losses=line.losses,
                saves=line.saves,
                holds=line.holds,
                earned_runs=line.earned_runs,
                strikeouts=line.strikeouts,
                hits_allowed=line.hits_allowed,
                walks_allowed=line.walks_allowed,
                home_runs_allowed=line.home_runs_allowed,
                hit_by_pitch_allowed=line.hit_by_pitch_allowed,
            )

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
        roster["bullpen_weights"] = _depth_weights(len(roster["bullpen"]), BULLPEN_DECAY)

        # 打線の戦力と投手陣の戦力。得点の期待値に反映させる
        roster["offense"] = (
            float(np.mean([_expected_ops(np.array([talents[p.id]]))[0] for p in roster["regulars"]]))
            if roster["regulars"]
            else 1.0
        )
        roster["defense"] = float(np.mean([skills[p.id] for p in roster["rotation"]])) if roster["rotation"] else 1.0

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

    def _plans(self, year, per_pair, rosters):
        """投入する試合の一覧を組み立てる。保存はまだしない。"""
        plans = []
        for league in League.objects.prefetch_related("teams"):
            teams = [
                team for team in league.teams.all() if rosters[team.id]["batters"] and rosters[team.id]["pitchers"]
            ]
            if len(teams) < 2:
                continue

            # 戦力はリーグの平均を1とした比で効かせる
            league_offense = float(np.mean([rosters[t.id]["offense"] for t in teams]))
            league_defense = float(np.mean([rosters[t.id]["defense"] for t in teams]))

            cards = [
                (home, away)
                for index, home in enumerate(teams)
                for away in teams[index + 1 :]
                for _ in range(per_pair)
            ]
            # ホームとビジターが偏らないよう半分は入れ替える
            cards = [(away, home) if position % 2 else (home, away) for position, (home, away) in enumerate(cards)]
            cards = self._shuffled(cards)

            # 先発が何番手かを数えるための、チームごとの登板順
            starts = defaultdict(int)
            for played_on, (home, away) in zip(self._dates(year, len(cards)), cards, strict=True):
                plans.append(
                    self._plan(
                        league,
                        home,
                        away,
                        played_on,
                        rosters,
                        starts,
                        league_offense,
                        league_defense,
                    )
                )
        return plans

    def _shuffled(self, items):
        order = self.rng.permutation(len(items))
        return [items[index] for index in order]

    @staticmethod
    def _dates(year, count):
        """試合日を開幕から終了まで均等に散らす。"""
        start = date(year, *SEASON_START)
        span = (date(year, *SEASON_END) - start).days
        return [start + timedelta(days=index * span // max(1, count)) for index in range(count)]

    # --- 1試合ぶんの組み立て ---

    def _plan(
        self,
        league,
        home,
        away,
        played_on,
        rosters,
        starts,
        league_offense,
        league_defense,
    ):
        """1試合ぶんの明細を作る。

        手順は「継投を決める → 回ごとの得点を配る → 打撃を作る →
        相手の打撃から投球を導く → 勝敗・S・H を決める」。
        """
        limit = league.foreign_player_game_limit
        sides = {}
        for team, opponent in ((home, away), (away, home)):
            sides[team.id] = {
                "team": team,
                "opponent": opponent,
                "roster": rosters[team.id],
                "is_home": team is home,
            }

        # 得点の総数を先に決める。抑えを使うかは点差で決まるため、継投より前に要る
        totals = self._totals(sides[home.id], sides[away.id], league_offense, league_defense)

        for team_id, side in sides.items():
            roster = side["roster"]
            side["lineup"] = self._lineup(roster, limit)
            used_foreign = sum(1 for e in side["lineup"] if e["player"].id in roster["foreign"])
            margin = totals[team_id] - totals[side["opponent"].id]
            side["staff"] = self._staff(
                roster,
                starts[team_id],
                None if limit is None else max(0, limit - used_foreign),
                # 勝っている接戦が抑えの出番。大差や負け試合では別の投手が締める
                closing=0 < margin <= SAVE_LEAD_LIMIT,
            )
            starts[team_id] += 1

        # 回ごとの得点。登板中の投手の力量で重みを付ける（打たれる投手の回に集まる）。
        # side['runs_by_inning'] は「その側の投手が投げた回に相手が挙げた点」
        for side in sides.values():
            side["runs_by_inning"] = self._runs_by_inning(totals[side["opponent"].id], side["staff"])

        home_score, away_score = totals[home.id], totals[away.id]
        # ホームの投手が投げた回にビジターが挙げた点が、ビジターのイニングスコア
        line_score = LineScore(
            away=tuple(int(v) for v in sides[home.id]["runs_by_inning"]),
            home=tuple(int(v) for v in sides[away.id]["runs_by_inning"]),
        )

        for side in sides.values():
            self._batting_entries(side, totals[side["team"].id])
        for side in sides.values():
            self._pitching_entries(side, sides[side["opponent"].id])

        # 勝敗はドメインの規則に決めさせる。集約を組んで渡す
        game = DomainGame(
            season=Season(played_on.year),
            played_on=played_on,
            home_team_id=home.id,
            away_team_id=away.id,
            home_score=home_score,
            away_score=away_score,
            line_score=line_score,
        )
        team_of = {}
        for side in sides.values():
            for entry in side["pitching_entries"]:
                team_of[entry["player"].id] = side["team"].id
                game.record_pitching(
                    entry["player"].id,
                    entry["line"],
                    appearance_order=entry["appearance_order"],
                    entered_inning=entry["segment"]["innings"][0],
                )
        self._assign_decisions(game, sides, team_of)

        return {
            "league_name": league.name,
            "home": home,
            "away": away,
            "played_on": played_on,
            "home_score": home_score,
            "away_score": away_score,
            "line_score": line_score,
            "batting": [entry for side in sides.values() for entry in side["batting_entries"]],
            "pitching": [entry for side in sides.values() for entry in side["pitching_entries"]],
        }

    def _totals(self, home_side, away_side, league_offense, league_defense):
        """両チームの得点。戦力比で期待値を動かし、負の二項分布から引く。

        同点の大半は延長で決着させる。独立に引くと1割以上が同点になり、
        引分が年18試合という現実には無い数になる。
        """
        drawn = {}
        for side, opponent in ((home_side, away_side), (away_side, home_side)):
            offense = (side["roster"]["offense"] / league_offense) ** OFFENSE_ELASTICITY
            # 相手投手陣の倍率は大きいほど打たれるので、そのまま得点に効く
            defense = (opponent["roster"]["defense"] / league_defense) ** DEFENSE_ELASTICITY
            mean = max(0.5, RUNS_MEAN * offense * defense)
            probability = RUNS_SHAPE / (RUNS_SHAPE + mean)
            drawn[side["team"].id] = int(self.rng.negative_binomial(RUNS_SHAPE, probability))

        home_id, away_id = home_side["team"].id, away_side["team"].id
        if drawn[home_id] == drawn[away_id] and self.rng.random() >= TIE_SURVIVAL_RATIO:
            winner = home_id if self.rng.random() < 0.5 else away_id
            drawn[winner] += 1
        return drawn

    def _runs_by_inning(self, total, staff):
        """相手が挙げた得点を回ごとに配る。

        重みは、その回に投げている投手の失点の倍率。打たれる投手の回に得点が
        集まるので、同じチームの中でも防御率に差が付く。
        """
        weights = np.array([segment["skill"] for segment in staff for _ in segment["innings"]])
        if weights.size != INNINGS_PER_GAME:
            # 継投が9回を覆えていない場合は均等に配る（起こらない想定の保険）
            weights = np.ones(INNINGS_PER_GAME)
        return self.rng.multinomial(total, weights / weights.sum())

    # --- 出場選手 ---

    def _lineup(self, roster, limit):
        """その試合の打順。レギュラーを基本にし、一部を控えと入れ替える。

        控えは序列の上位から選ばれやすい。均等に散らすと1人あたりの打席が
        少なすぎて、極端な率の選手が一覧の上位に並ぶ。
        代打は打順の途中出場（果次1）として別に加える。
        """
        bench = roster["bench"]
        swap = self.rng.random(len(roster["regulars"])) < BENCH_RATIO

        starters = []
        used = set()
        for order, (regular, replaced) in enumerate(zip(roster["regulars"], swap, strict=True), start=1):
            player = regular
            if replaced and bench:
                picked = bench[self.rng.choice(len(bench), p=roster["bench_weights"])]
                if picked.id not in used:
                    player = picked
            if player.id in used:
                player = regular
            used.add(player.id)
            starters.append(
                {
                    "player": player,
                    "batting_order": order,
                    "slot_sequence": 0,
                    # 控えが先発する場合はレギュラーの守備位置を引き継ぐ
                    "fielding_position": roster["positions"].get(player.id, roster["positions"].get(regular.id))
                    or FieldingPosition.DESIGNATED_HITTER,
                }
            )

        return self._with_pinch_hitters(starters, roster, used, limit)

    def _with_pinch_hitters(self, starters, roster, used, limit):
        """代打を加える。スタメンと控えの区別がボックススコアに出るようにする。"""
        entries = list(starters)
        bench = [p for p in roster["bench"] if p.id not in used]

        if bench and self.rng.random() < PINCH_HITTER_RATIO:
            count = min(len(bench), int(self.rng.integers(1, MAX_PINCH_HITTERS + 1)))
            picks = self.rng.choice(len(bench), size=count, replace=False)
            orders = self.rng.choice(LINEUP_SIZE, size=count, replace=False) + 1
            for index, order in zip(picks, orders, strict=True):
                entries.append(
                    {
                        "player": bench[index],
                        "batting_order": int(order),
                        "slot_sequence": 1,
                        "fielding_position": FieldingPosition.PINCH_HITTER,
                    }
                )

        kept = self._within_quota([e["player"] for e in entries], roster["foreign"], limit)
        allowed = {p.id for p in kept}
        return [e for e in entries if e["player"].id in allowed]

    def _staff(self, roster, start_index, limit, *, closing=False):
        """その試合の投手陣。先発はローテーション順、救援は序列の上位から。

        受け持つ回を先に決める（回の途中の交代はアウト数のやり取りで表す）。
        回を持たせておくと、継投した時点のスコアが分かり、セーブ・ホールドの
        条件を判定できる。

        closing は「勝っている接戦か」。抑えの出番をここで絞る。
        """
        rotation = roster["rotation"]
        starter = rotation[start_index % len(rotation)]

        starter_innings = int(self.rng.integers(*STARTER_INNINGS, endpoint=True))
        remaining = INNINGS_PER_GAME - starter_innings

        pitchers = [starter] + self._relievers(roster, starter, remaining, closing=closing)
        blocks = self._split_innings(starter_innings, len(pitchers))

        staff = []
        # 枠に収まらなかった投手は blocks より少なくなるので、短い方に合わせる
        for order, (pitcher, innings) in enumerate(zip(pitchers, blocks), start=1):  # noqa: B905
            staff.append(
                {
                    "player": pitcher,
                    "appearance_order": order,
                    "innings": innings,
                    "skill": roster["pitching_talent"][pitcher.id],
                }
            )

        self._shave_outs(staff)
        kept = self._within_quota([s["player"] for s in staff], roster["foreign"], limit)
        allowed = {p.id for p in kept}
        if len(allowed) == len(staff):
            return staff
        if not allowed:
            # 全員が枠から外れることは無いが、投手が居ない試合は成立しない
            allowed = {staff[0]["player"].id}
        # 枠で外れた投手の回は、残った投手が引き受ける
        return self._merge_innings(staff, allowed)

    def _relievers(self, roster, starter, remaining, *, closing):
        """救援を投げる順に並べる。最後は抑え（序列1位）が締める。

        抑えを固定しないとセーブが数人に分散し、1人で年30セーブという実際の
        形にならない。中継ぎは序列の上位から選ばれやすい。
        """
        bullpen = [p for p in roster["bullpen"] if p is not starter]
        if not bullpen:
            return []

        wanted = min(len(bullpen), max(1, remaining))
        closer = bullpen[0]
        # 抑えは中継ぎの候補には入れない。候補に残すと序列1位ゆえ中継ぎとしても
        # 頻繁に選ばれ、投球回が実際の抑えの倍を超えてしまう
        pool = bullpen[1:]
        usage = CLOSER_USAGE_IN_SAVE_SITUATION if closing else CLOSER_USAGE_OTHERWISE
        use_closer = bool(pool) and wanted > 1 and self.rng.random() < usage

        middle_wanted = min(len(pool), wanted - 1 if use_closer else wanted)
        picks = (
            self.rng.choice(
                len(pool),
                size=middle_wanted,
                replace=False,
                p=_depth_weights(len(pool), BULLPEN_DECAY),
            )
            if pool and middle_wanted
            else []
        )
        # 中継ぎは序列の下位から先に投げる（良い投手を後ろに残す）
        middle = [pool[index] for index in reversed(sorted(picks))]

        if use_closer:
            return middle + [closer]
        # 抑えを使わない試合は、中継ぎがそのまま締める（抑えが休む日）
        return middle or [closer]

    @staticmethod
    def _split_innings(starter_innings, pitcher_count):
        """9回を投手ぶんの連続した回に分ける。先発が starter_innings 回を持つ。

        投手が1人しかいなければ完投とする（9回すべてを受け持たせないと、
        誰も投げていない回が生まれて失点の帰属先が無くなる）。

        **最後の投手は最終回だけを受け持つ。** 抑えに複数回を投げさせると
        年間130回を超え、実際の抑え（60回前後）から離れてしまう。
        """
        all_innings = list(range(1, INNINGS_PER_GAME + 1))
        if pitcher_count <= 1:
            return [all_innings]

        blocks = [all_innings[:starter_innings]]
        remaining = all_innings[starter_innings:]
        relievers = pitcher_count - 1

        closing = []
        if relievers >= 2 and len(remaining) > 1:
            closing = [remaining[-1]]
            remaining = remaining[:-1]
            relievers -= 1

        # 残りの回を中継ぎに均等に割り振る。端数は後ろの投手に寄せる
        base, extra = divmod(len(remaining), relievers)
        cursor = 0
        for index in range(relievers):
            size = base + (1 if index >= relievers - extra else 0)
            blocks.append(remaining[cursor : cursor + size])
            cursor += size
        if closing:
            blocks.append(closing)
        return [block for block in blocks if block]

    def _shave_outs(self, staff):
        """回の途中での交代を表す。前の投手のアウトを次の投手に渡す。

        こうしないと投球回が常に X.0 になり、5.2 のような表記が出てこない。
        """
        for segment in staff:
            segment["outs"] = len(segment["innings"]) * OUTS_PER_INNING

        for index in range(len(staff) - 1):
            if self.rng.random() >= MID_INNING_CHANGE_RATIO:
                continue
            handed = int(self.rng.integers(1, OUTS_PER_INNING))
            if staff[index]["outs"] - handed < 1:
                continue
            staff[index]["outs"] -= handed
            staff[index + 1]["outs"] += handed

    @staticmethod
    def _merge_innings(staff, allowed):
        """外国人枠で外れた投手の回を、残った投手に引き受けさせる。

        9回すべてに投手が居る状態を保つ。抜けた回をそのまま落とすと、
        誰も投げていない回に失点が帰属できなくなり、勝敗の判定も崩れる。
        """
        merged = []
        # まだ引き受け先が決まっていない区間（先発が外れた場合の先頭の回）
        pending = []
        for segment in staff:
            if segment["player"].id not in allowed:
                if merged:
                    merged[-1]["innings"] += segment["innings"]
                    merged[-1]["outs"] += segment["outs"]
                else:
                    pending.append(segment)
                continue

            kept = dict(segment)
            if pending:
                kept["innings"] = [inning for dropped in pending for inning in dropped["innings"]] + kept["innings"]
                kept["outs"] += sum(dropped["outs"] for dropped in pending)
                pending = []
            merged.append(kept)

        for order, segment in enumerate(merged, start=1):
            segment["appearance_order"] = order
        return merged

    @staticmethod
    def _within_quota(players, foreign_ids, limit):
        """外国人選手が出場枠を超えないように絞る。

        枠は「1試合に出場できる外国人選手の上限」で、リーグが持つ。サイトからの
        入力では検査されるので、投入データも同じ規則に従わせる。
        """
        if limit is None:
            return players

        chosen, foreign_used = [], 0
        for player in players:
            if player.id in foreign_ids:
                if foreign_used >= limit:
                    continue
                foreign_used += 1
            chosen.append(player)
        return chosen

    # --- 明細 ---

    def _batting_entries(self, side, runs_to_drive_in):
        """打順ぶんの打撃成績。打点の合計はその試合の得点に一致させる。

        安打数は二項分布（打数ぶん試して安打が出る回数）。内訳は本塁打・二塁打・
        三塁打・単打の順に、残りから引き算しながら決める。
        代打は1打席だけなので、打数の引き方を分ける。
        """
        entries = side["lineup"]
        talents = side["roster"]["batting_talent"]
        talent = np.array([talents[e["player"].id] for e in entries])
        contact, power, walk_rate = talent[:, 0], talent[:, 1], talent[:, 2]

        is_starter = np.array([e["slot_sequence"] == 0 for e in entries])
        at_bats = np.where(
            is_starter,
            AT_BATS_BASE + self.rng.binomial(AT_BATS_EXTRA_TRIALS, AT_BATS_EXTRA_RATIO, size=len(entries)),
            1,
        )

        hits = self.rng.binomial(at_bats, contact)
        home_runs = self.rng.binomial(hits, power)
        rest = hits - home_runs
        doubles = self.rng.binomial(rest, DOUBLE_SHARE)
        rest = rest - doubles
        triples = self.rng.binomial(rest, TRIPLE_SHARE)
        singles = rest - triples

        walks = self.rng.binomial(at_bats + 1, walk_rate)
        hit_by_pitch = self.rng.binomial(1, BATTER_HIT_BY_PITCH_RATIO, size=len(entries))

        # 打点は得点そのものを分配する。長打力のある打者に寄せる（走者を還すのは
        # 長打で、打点は中軸に集まる）。無安打でも犠飛で打点は付くので下限を置く
        weights = np.maximum(1.0, (hits + home_runs * 3) * (1.0 + power * 4))
        runs_batted_in = self.rng.multinomial(runs_to_drive_in, weights / weights.sum())

        built = []
        for index, entry in enumerate(entries):
            hit_total = int(singles[index] + doubles[index] + triples[index] + home_runs[index])
            built.append(
                dict(
                    entry,
                    line=BattingLine(
                        at_bats=int(at_bats[index]),
                        singles=int(singles[index]),
                        doubles=int(doubles[index]),
                        triples=int(triples[index]),
                        home_runs=int(home_runs[index]),
                        runs_batted_in=int(runs_batted_in[index]),
                        walks=int(walks[index]),
                        hit_by_pitch=int(hit_by_pitch[index]),
                        # 無安打で打点が付いた打者は犠飛として記録の形を整える
                        sacrifice_flies=1 if hit_total == 0 and runs_batted_in[index] else 0,
                    ),
                )
            )
        side["batting_entries"] = built
        return built

    def _pitching_entries(self, side, opponent):
        """投手ぶんの投球成績。被記録は相手打線の記録から配る。

        被安打・被本塁打・与四死球の合計は相手打線の合計と一致する。別に確率で
        引くと、リーグ全体の本塁打と被本塁打が食い違ってしまう。
        失点はその投手が投げた回に相手が挙げた得点。
        """
        staff = side["staff"]
        outs = np.array([segment["outs"] for segment in staff])
        skill = np.array([segment["skill"] for segment in staff])
        weights = outs * skill
        share = weights / weights.sum()

        opponent_lines = [entry["line"] for entry in opponent["batting_entries"]]
        hits_total = sum(line.hits for line in opponent_lines)
        home_runs_total = sum(line.home_runs for line in opponent_lines)
        walks_total = sum(line.walks for line in opponent_lines)
        hit_by_pitch_total = sum(line.hit_by_pitch for line in opponent_lines)

        hits = self.rng.multinomial(hits_total, share)
        # 被本塁打は被安打の内数。有限の山から選ぶので超幾何分布を使う
        home_runs = self.rng.multivariate_hypergeometric(hits, home_runs_total)
        walks = self.rng.multinomial(walks_total, share)
        hit_by_pitch = self.rng.multinomial(hit_by_pitch_total, share)

        strikeouts = self.rng.binomial(outs, np.clip(STRIKEOUTS_PER_OUT / skill**STRIKEOUT_BOOST, 0, 1))

        runs_by_inning = side["runs_by_inning"]
        built = []
        for index, segment in enumerate(staff):
            runs = int(sum(runs_by_inning[inning - 1] for inning in segment["innings"]))
            earned = int(self.rng.binomial(runs, EARNED_RUN_RATIO))
            built.append(
                {
                    "player": segment["player"],
                    "appearance_order": segment["appearance_order"],
                    "segment": segment,
                    "runs": runs,
                    "line": PitchingLine(
                        innings=InningsPitched(outs=int(outs[index])),
                        earned_runs=earned,
                        hits_allowed=int(hits[index]),
                        home_runs_allowed=int(home_runs[index]),
                        walks_allowed=int(walks[index]),
                        hit_by_pitch_allowed=int(hit_by_pitch[index]),
                        strikeouts=int(strikeouts[index]),
                        starts=1 if segment["appearance_order"] == 1 else 0,
                    ),
                }
            )
        side["pitching_entries"] = built
        return built

    # --- 勝敗・セーブ・ホールド ---

    def _assign_decisions(self, game, sides, team_of):
        """勝利・敗戦・セーブ・ホールドを、ドメインの規則に決めさせる。

        判定はイニングスコアと継投から一意に決まるので、手動入力と同じ
        ドメインサービスに委ねる。ここで独自に書くと2つの実装がずれる。
        """
        decisions = domain_services.pitching_decisions(game, team_of)

        for side in sides.values():
            for entry in side["pitching_entries"]:
                player_id = entry["player"].id
                line = entry["line"]
                wins = decisions.wins_for(player_id)
                entry["line"] = PitchingLine(
                    innings=line.innings,
                    earned_runs=line.earned_runs,
                    strikeouts=line.strikeouts,
                    hits_allowed=line.hits_allowed,
                    walks_allowed=line.walks_allowed,
                    home_runs_allowed=line.home_runs_allowed,
                    hit_by_pitch_allowed=line.hit_by_pitch_allowed,
                    starts=line.starts,
                    wins=wins,
                    losses=decisions.losses_for(player_id),
                    saves=decisions.saves_for(player_id),
                    holds=decisions.holds_for(player_id),
                    relief_wins=wins if entry["appearance_order"] > 1 else 0,
                )

    def _report(self, plans):
        counts = defaultdict(int)
        for plan in plans:
            counts[plan["league_name"]] += 1
        for league_name, count in sorted(counts.items()):
            self.stdout.write(f"  {league_name}: {count}試合")
