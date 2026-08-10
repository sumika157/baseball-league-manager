"""仮想の選手データを投入する。

各チーム最大40人（実運用に近い28〜40人でばらつかせる）まで、既存の選手・在籍は
残したまま不足分を架空の選手で埋める。ポジション構成・年齢分布はMLBの40人ロース
ターの構成比をおおまかに参考にする。

一部（約12%）は出身地を海外にした「助っ人」風の選手にする。国籍を区別する専用
フィールドは現時点でこのモデルに存在しないため、出身地（birthplace）の値で見分け
られるようにしてある。専用フィールドが追加された際は、
    Player.objects.exclude(birthplace__in=JAPANESE_PREFECTURES)
のように birthplace が国内47都道府県のいずれでもない選手を対象に、当スクリプトが
選んだ国名（例: 'アメリカ合衆国'）から国籍値へ変換して差し込めばよい。
"""

import random
from datetime import date

from django.core.management.base import BaseCommand
from django.db import transaction

from myapp.domain.value_objects import Handedness, Position
from myapp.models import Player, PlayerStint, Team

JAPANESE_PREFECTURES = [
    '北海道', '青森県', '岩手県', '宮城県', '秋田県', '山形県', '福島県',
    '茨城県', '栃木県', '群馬県', '埼玉県', '千葉県', '東京都', '神奈川県',
    '新潟県', '富山県', '石川県', '福井県', '山梨県', '長野県', '岐阜県',
    '静岡県', '愛知県', '三重県', '滋賀県', '京都府', '大阪府', '兵庫県',
    '奈良県', '和歌山県', '鳥取県', '島根県', '岡山県', '広島県', '山口県',
    '徳島県', '香川県', '愛媛県', '高知県', '福岡県', '佐賀県', '長崎県',
    '熊本県', '大分県', '宮崎県', '鹿児島県', '沖縄県',
]

JP_SURNAMES = [
    '佐藤', '鈴木', '高橋', '田中', '伊藤', '渡辺', '山本', '中村', '小林', '加藤',
    '吉田', '山田', '佐々木', '山口', '松本', '井上', '木村', '林', '斎藤', '清水',
    '山崎', '森', '池田', '橋本', '阿部', '石川', '前田', '藤田', '後藤', '岡田',
    '長谷川', '村上', '近藤', '石井', '坂本', '遠藤', '青木', '藤井', '西村', '福田',
    '太田', '三浦', '岡本', '松田', '中川', '中野', '原田', '小野', '田村', '竹内',
]

JP_GIVEN_NAMES = [
    '翔太', '大輔', '健太', '直樹', '拓也', '亮太', '陽介', '涼介', '康太', '大樹',
    '悠斗', '颯太', '蓮', '大和', '陸', '樹', '陽翔', '湊', '蒼', '悠真',
    '大翔', '匠', '龍之介', '秀樹', '誠', '隆', '学', '剛', '修', '亮',
    '智也', '雄大', '一輝', '光', '健吾', '俊', '慶介', '裕貴', '春希', '光誠',
    '涼', '康介', '拓真', '直人', '和也', '昇', '大地', '幹太', '佑樹', '竜也',
]

# 出身地は「都道府県 + 汎用の種別」の組み合わせで、実在の学校名と混同しない
# 架空の名前にする。
HIGH_SCHOOL_SUFFIXES = ['高校', '第一高校', '工業高校', '商業高校', '学園高校']
UNIVERSITIES = [
    '東湾大学', '中央国際大学', '北陵大学', '西海学院大学', '南央大学',
    '国際経済大学', '青嵐大学', '緑丘大学', '白鷺大学', '甲南学芸大学',
]
CORPORATE_TEAMS = [
    '東海重工', '北陸電機', '大和製鋼', '扶桑紡績', '中京運輸',
    '山陽化学', '関東製粉', '九州電子工業', '北都銀行', '太平洋物産',
]

FOREIGN_GROUPS = [
    {
        'country': 'アメリカ合衆国',
        'given': ['マイケル', 'ジェームズ', 'ジョン', 'ロバート', 'デイビッド',
                   'クリス', 'ジャスティン', 'ケビン', 'ブランドン', 'タイラー'],
        'surname': ['ジョンソン', 'ウィリアムズ', 'ブラウン', 'ミラー', 'デイビス',
                    'ウィルソン', 'アンダーソン', 'トンプソン', 'マルティネス', 'ロビンソン'],
    },
    {
        'country': 'ドミニカ共和国',
        'given': ['ホセ', 'フアン', 'ペドロ', 'ラファエル', 'ミゲル',
                   'ルイス', 'カルロス', 'ラモン', 'ワンダー', 'エルビス'],
        'surname': ['マルティネス', 'ロドリゲス', 'ペレス', 'サンチェス', 'ラミレス',
                    'クルーズ', 'レイエス', 'ペーニャ', 'ゴメス', 'トーレス'],
    },
    {
        'country': 'ベネズエラ',
        'given': ['アレハンドロ', 'エドゥアルド', 'ガブリエル', 'ロナルド', 'サルバドール',
                   'アンヘル', 'ウィルソン', 'フレディ', 'ヨンダー', 'オジー'],
        'surname': ['ゴンザレス', 'エルナンデス', 'グズマン', 'マルカーノ', 'ロンドン',
                    'サラザール', 'アリアス', 'ベジョ', 'サンブラーノ', 'アポンテ'],
    },
    {
        'country': 'キューバ',
        'given': ['ヤシエル', 'ヨルダン', 'アロルディス', 'ユリ', 'アレクセイ',
                   'エリスベル', 'フレデリク', 'ロエル', 'ルルデス', 'ユニオル'],
        'surname': ['セペダ', 'グリエル', 'デスパイネ', 'アブレイユ', 'ラモス',
                    'イグレシアス', 'スアレス', 'プイグ', 'セスペデス', 'バルデス'],
    },
    {
        'country': '大韓民国',
        'given': ['ミンジュン', 'ジフン', 'スンヒョン', 'ドンウォン', 'ジェヨン',
                   'ヒョヌ', 'ソンミン', 'テヤン', 'キョンミン', 'ジュノ'],
        'surname': ['キム', 'イ', 'パク', 'チェ', 'チョン',
                    'カン', 'ユン', 'ハン', 'オ', 'ソ'],
    },
    {
        'country': '台湾',
        'given': ['チーウェイ', 'チュンシェン', 'ウェイイン', 'ジエミン', 'ユーチェン',
                   'ポージュー', 'カイウェイ', 'チェンユー', 'モンシュエン', 'イーチェン'],
        'surname': ['チェン', 'リン', 'ワン', 'チャン', 'ウー',
                    'ホアン', 'シュー', 'クオ', 'ツァイ', 'ヤン'],
    },
    {
        'country': 'オーストラリア',
        'given': ['リアム', 'ジャック', 'ライアン', 'ネイサン', 'トッド',
                   'グラント', 'デイミアン', 'トレント', 'シェーン', 'アダム'],
        'surname': ['スミス', 'テイラー', 'ホワイト', 'クラーク', 'ヒューズ',
                    'ターナー', 'ワトソン', 'ベル', 'マーシュ', 'クーパー'],
    },
]

# MLBの40人ロースター構成をおおまかに参考にした比率。合計は1.0。
POSITION_RATIOS = {
    Position.PITCHER.value: 0.475,
    Position.CATCHER.value: 0.0625,
    Position.INFIELDER.value: 0.2375,
    Position.OUTFIELDER.value: 0.1875,
    Position.DESIGNATED_HITTER.value: 0.025,
}

# ポジションごとの体格レンジ（cm, kg）。MLB選手の体格傾向を参考にした目安。
PHYSIQUE_RANGES = {
    Position.PITCHER.value: ((178, 196), (78, 98)),
    Position.CATCHER.value: ((172, 185), (75, 92)),
    Position.INFIELDER.value: ((170, 186), (68, 88)),
    Position.OUTFIELDER.value: ((175, 190), (72, 92)),
    Position.DESIGNATED_HITTER.value: ((178, 193), (82, 100)),
}

FOREIGN_PLAYER_RATIO = 0.12
MIN_ROSTER, MAX_ROSTER = 28, 40


def largest_remainder(total, ratios):
    """比率(dict)にしたがって total を整数配分する（最大剰余法）。"""
    raw = {key: total * ratio for key, ratio in ratios.items()}
    floored = {key: int(value) for key, value in raw.items()}
    remainder = total - sum(floored.values())
    order = sorted(raw, key=lambda key: raw[key] - floored[key], reverse=True)
    for key in order[:remainder]:
        floored[key] += 1
    return floored


def make_japanese_name(used_names):
    for _ in range(50):
        name = random.choice(JP_SURNAMES) + random.choice(JP_GIVEN_NAMES)
        if name not in used_names:
            used_names.add(name)
            return name
    # 50回試して衝突する確率は極めて低いが、念のため連番で確定させる
    name = f"{random.choice(JP_SURNAMES)}{random.choice(JP_GIVEN_NAMES)}{len(used_names)}"
    used_names.add(name)
    return name


def make_foreign_name(used_names):
    group = random.choice(FOREIGN_GROUPS)
    for _ in range(50):
        name = f"{random.choice(group['given'])}・{random.choice(group['surname'])}"
        if name not in used_names:
            used_names.add(name)
            return name, group['country']
    name = f"{name}{len(used_names)}"
    used_names.add(name)
    return name, group['country']


def make_birth_date(today, age):
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    had_birthday_this_year = (month, day) <= (today.month, today.day)
    birth_year = today.year - age if had_birthday_this_year else today.year - age - 1
    return date(birth_year, month, day)


def make_amateur_career(is_foreign):
    """(high_school, university, corporate_team, debut_age) を返す。"""
    if is_foreign:
        return '', '', '', random.randint(19, 30)

    path = random.choices(['high_school', 'university', 'corporate'], weights=[30, 50, 20])[0]
    high_school = f"{random.choice(JAPANESE_PREFECTURES)}{random.choice(HIGH_SCHOOL_SUFFIXES)}"
    if path == 'high_school':
        return high_school, '', '', 18
    if path == 'university':
        return high_school, random.choice(UNIVERSITIES), '', 22
    return high_school, '', random.choice(CORPORATE_TEAMS), random.randint(23, 26)


class Command(BaseCommand):
    help = '各チームに仮想の選手データを投入する（既存の選手・在籍は残す）'

    def add_arguments(self, parser):
        parser.add_argument('--seed', type=int, default=None, help='乱数シード（再現用）')
        parser.add_argument('--dry-run', action='store_true', help='投入せず件数だけ表示する')

    def handle(self, *args, **options):
        if options['seed'] is not None:
            random.seed(options['seed'])

        today = date.today()
        used_names = set(Player.objects.values_list('name', flat=True))

        created_players = 0
        created_stints = 0
        foreign_count = 0
        team_reports = []

        with transaction.atomic():
            for team in Team.objects.all():
                active_stints = list(
                    PlayerStint.objects.filter(team=team, to_year__isnull=True)
                    .select_related('player')
                )
                existing_count = len(active_stints)
                used_numbers = {s.number for s in active_stints}
                existing_by_position = {}
                for s in active_stints:
                    existing_by_position[s.player.position] = (
                        existing_by_position.get(s.player.position, 0) + 1
                    )

                roster_size = random.randint(MIN_ROSTER, MAX_ROSTER)
                if existing_count >= roster_size:
                    team_reports.append((team.name, existing_count, 0))
                    continue

                to_add = roster_size - existing_count
                target_by_position = largest_remainder(roster_size, POSITION_RATIOS)
                add_by_position = {
                    position: max(0, target_by_position[position] - existing_by_position.get(position, 0))
                    for position in POSITION_RATIOS
                }
                # 端数調整で合計が to_add からずれる場合は内野手で吸収する
                diff = to_add - sum(add_by_position.values())
                add_by_position[Position.INFIELDER.value] += diff

                available_numbers = [n for n in range(1, 100) if n not in used_numbers]
                random.shuffle(available_numbers)

                added_this_team = 0
                for position, count in add_by_position.items():
                    height_range, weight_range = PHYSIQUE_RANGES[position]
                    for _ in range(count):
                        if added_this_team >= to_add:
                            break

                        is_foreign = random.random() < FOREIGN_PLAYER_RATIO
                        if is_foreign:
                            name, country = make_foreign_name(used_names)
                            birthplace = country
                        else:
                            name = make_japanese_name(used_names)
                            birthplace = random.choice(JAPANESE_PREFECTURES)

                        age = max(19, min(40, round(random.gauss(27.5, 3.8))))
                        birth_date = make_birth_date(today, age)
                        high_school, university, corporate_team, debut_age = make_amateur_career(is_foreign)
                        debut_age = min(debut_age, age)
                        debut_year = birth_date.year + debut_age

                        if position == Position.CATCHER.value:
                            throws = Handedness.RIGHT.value if random.random() < 0.92 else Handedness.LEFT.value
                        else:
                            throws = Handedness.RIGHT.value if random.random() < 0.75 else Handedness.LEFT.value
                        bats = random.choices(
                            [Handedness.RIGHT.value, Handedness.LEFT.value, Handedness.BOTH.value],
                            weights=[55, 30, 15],
                        )[0]

                        if not available_numbers:
                            available_numbers = [n for n in range(100, 1000) if n not in used_numbers]
                            random.shuffle(available_numbers)
                        number = available_numbers.pop()
                        used_numbers.add(number)

                        if options['dry_run']:
                            created_players += 1
                            created_stints += 1
                            added_this_team += 1
                            if is_foreign:
                                foreign_count += 1
                            continue

                        player = Player.objects.create(
                            name=name,
                            position=position,
                            birth_date=birth_date,
                            throws=throws,
                            bats=bats,
                            height_cm=random.randint(*height_range),
                            weight_kg=random.randint(*weight_range),
                            birthplace=birthplace,
                            debut_year=debut_year,
                            high_school=high_school,
                            university=university,
                            corporate_team=corporate_team,
                        )
                        PlayerStint.objects.create(
                            player=player,
                            team=team,
                            number=number,
                            from_year=debut_year,
                            to_year=None,
                        )
                        created_players += 1
                        created_stints += 1
                        added_this_team += 1
                        if is_foreign:
                            foreign_count += 1

                team_reports.append((team.name, existing_count, added_this_team))

            if options['dry_run']:
                transaction.set_rollback(True)

        for name, existing, added in team_reports:
            self.stdout.write(f"{name}: 既存{existing}人 + 新規{added}人 = {existing + added}人")

        prefix = '[dry-run] ' if options['dry_run'] else ''
        self.stdout.write(self.style.SUCCESS(
            f"{prefix}選手 {created_players}人 / 在籍 {created_stints}件 を作成しました"
            f"（うち海外出身 {foreign_count}人）"
        ))
