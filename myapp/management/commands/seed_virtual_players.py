"""仮想の選手データを投入する。

各チーム最大40人（実運用に近い28〜40人でばらつかせる）まで、既存の選手・在籍は
残したまま不足分を架空の選手で埋める。ポジション構成・年齢分布はMLBの40人ロース
ターの構成比をおおまかに参考にする。

一部（約12%）は「助っ人」として nationality / is_foreign_player を設定する
（既存データは 0024_backfill_foreign_players_from_birthplace で移行済み）。

氏名は「苗字＋名前」を漢字プールから組み合わせて作るため、よみがな（カタカナ）と
背ネーム（ユニフォーム背面のヘボン式アルファベット表記）もプール側に持たせてある。
背ネームは同じチーム内に同姓の選手がいる場合、ファーストネームの頭文字＋ピリオドを
先頭に付けて区別する（例: 「K.SATO」）。
"""

import random
from collections import Counter, defaultdict
from datetime import date

from django.core.management.base import BaseCommand
from django.db import transaction

from myapp.domain.pennant.world import WorldScope
from myapp.domain.value_objects import Position
from myapp.domain.virtual_players import generator, pools
from myapp.domain.virtual_players.generator import MAX_ROSTER, MIN_ROSTER, POSITION_RATIOS, largest_remainder
from myapp.infrastructure.scoping import players_in, stints_in, teams_in
from myapp.models import Player, PlayerStint

# 投入・補正の対象は実データだけ。ペナントの世界の球団・選手には触れない
REAL_SCOPE = WorldScope.real()

# このコマンドの初回投入より前から存在した選手。--rename-existing で名前を
# 選び直す対象から除外し、--assign-readings では固定のよみがな・ローマ字を使う
# （架空データではなく実際に登録された選手であり、生成プールに読みを持たないため）。
ORIGINAL_PLAYER_READINGS = {
    "藤井健吾": ("フジイケンゴ", "FUJII", "KENGO"),
    "中島亮太": ("ナカジマリョウタ", "NAKAJIMA", "RYOTA"),
    "渡辺春希": ("ワタナベハルキ", "WATANABE", "HARUKI"),
    "大田光誠": ("オオタコウセイ", "OTA", "KOSEI"),
    "山中俊": ("ヤマナカシュン", "YAMANAKA", "SHUN"),
    "坂上康太": ("サカウエコウタ", "SAKAUE", "KOTA"),
    "黒田裕貴": ("クロダユウキ", "KURODA", "YUKI"),
    "高橋誠司": ("タカハシセイジ", "TAKAHASHI", "SEIJI"),
    "大岩蓮": ("オオイワレン", "OIWA", "REN"),
    "遠藤慶介": ("エンドウケイスケ", "ENDO", "KEISUKE"),
    "佐藤涼介": ("サトウリョウスケ", "SATO", "RYOSUKE"),
}
ORIGINAL_PLAYER_NAMES = list(ORIGINAL_PLAYER_READINGS)


class Command(BaseCommand):
    help = "各チームに仮想の選手データを投入する（既存の選手・在籍は残す）"

    def add_arguments(self, parser):
        parser.add_argument("--seed", type=int, default=None, help="乱数シード（再現用）")
        parser.add_argument("--dry-run", action="store_true", help="投入せず件数だけ表示する")
        parser.add_argument(
            "--rename-existing",
            action="store_true",
            help=(
                "このコマンドで過去に投入した架空の日本人選手（助っ人を除く）の"
                "氏名を、珍しい苗字・名前を混ぜたプールで選び直す。新規投入はしない。"
            ),
        )
        parser.add_argument(
            "--assign-readings",
            action="store_true",
            help=("全選手（実在・架空とも）によみがなと背ネームを付与する。氏名や新規投入はしない。"),
        )
        parser.add_argument(
            "--refresh-schools",
            action="store_true",
            help=(
                "このコマンドで過去に投入した架空の日本人選手（助っ人を除く）の"
                "出身高校・大学・社会人チームを選び直す。入団年など他の項目は変えない。"
            ),
        )

    def handle(self, *args, **options):
        # 乱数源はこのコマンドの中だけで持つ（生成の関数は rng を引数に取る）
        rng = random.Random(options["seed"])

        if options["rename_existing"]:
            self._rename_existing(rng, dry_run=options["dry_run"])
            return

        if options["assign_readings"]:
            self._assign_readings(dry_run=options["dry_run"])
            return

        if options["refresh_schools"]:
            self._refresh_schools(rng, dry_run=options["dry_run"])
            return

        today = date.today()
        used_names = set(players_in(REAL_SCOPE).values_list("name", flat=True))

        created_players = 0
        created_stints = 0
        foreign_count = 0
        team_reports = []

        with transaction.atomic():
            for team in teams_in(REAL_SCOPE).select_related("league"):
                active_stints = list(
                    PlayerStint.objects.filter(team=team, to_year__isnull=True).select_related("player")
                )
                existing_count = len(active_stints)
                used_numbers = {s.number for s in active_stints}
                # 外国人選手の登録枠（リーグが持つ。None は無制限）。ORM に直接書くため
                # Team 集約の ensure_foreign_player_quota を通らないので、ここで同じ検査をする
                foreign_limit = team.league.foreign_player_roster_limit
                foreign_on_team = sum(1 for s in active_stints if s.player.is_foreign_player)
                existing_by_position: dict[Position, int] = {}
                for s in active_stints:
                    existing = Position.from_label(s.player.position)
                    existing_by_position[existing] = existing_by_position.get(existing, 0) + 1

                roster_size = rng.randint(MIN_ROSTER, MAX_ROSTER)
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
                add_by_position[Position.INFIELDER] += diff

                available_numbers = [n for n in range(1, 100) if n not in used_numbers]
                rng.shuffle(available_numbers)

                new_players = []
                added_this_team = 0
                for position, count in add_by_position.items():
                    for _ in range(count):
                        if added_this_team >= to_add:
                            break

                        is_foreign = rng.random() < generator.FOREIGN_PLAYER_RATIO
                        if is_foreign and foreign_limit is not None and foreign_on_team >= foreign_limit:
                            # 枠が埋まっていれば日本人選手にする
                            is_foreign = False
                        if is_foreign:
                            foreign_on_team += 1
                            name, name_kana, country, surname_romaji, given_romaji = generator.foreign_name(
                                rng, used_names
                            )
                            birthplace = country
                        else:
                            name, name_kana, surname_romaji, given_romaji = generator.japanese_name(rng, used_names)
                            birthplace = generator.prefecture(rng)

                        age = max(19, min(40, round(rng.gauss(27.5, 3.8))))
                        birth_date = generator.birth_date_for(rng, age=age, as_of=today)
                        career = generator.amateur_career(rng, is_foreign=is_foreign, birthplace=birthplace)
                        debut_age = min(career.debut_age, age)
                        debut_year = birth_date.year + debut_age

                        throws, bats = generator.handedness(rng, position)
                        height_cm, weight_kg = generator.physique(rng, position)

                        if not available_numbers:
                            available_numbers = [n for n in range(100, 1000) if n not in used_numbers]
                            rng.shuffle(available_numbers)
                        number = available_numbers.pop()
                        used_numbers.add(number)

                        if options["dry_run"]:
                            created_players += 1
                            created_stints += 1
                            added_this_team += 1
                            if is_foreign:
                                foreign_count += 1
                            continue

                        player = Player.objects.create(
                            name=name,
                            name_kana=name_kana,
                            position=position.value,
                            birth_date=birth_date,
                            throws=throws.value,
                            bats=bats.value,
                            height_cm=height_cm,
                            weight_kg=weight_kg,
                            birthplace=birthplace,
                            debut_year=debut_year,
                            high_school=career.high_school,
                            university=career.university,
                            corporate_team=career.corporate_team,
                            nationality=birthplace if is_foreign else "",
                            is_foreign_player=is_foreign,
                        )
                        PlayerStint.objects.create(
                            player=player,
                            team=team,
                            number=number,
                            from_year=debut_year,
                            to_year=None,
                        )
                        new_players.append((player, surname_romaji, given_romaji))
                        created_players += 1
                        created_stints += 1
                        added_this_team += 1
                        if is_foreign:
                            foreign_count += 1

                if not options["dry_run"]:
                    self._apply_back_names(team, new_players)

                team_reports.append((team.name, existing_count, added_this_team))

            if options["dry_run"]:
                transaction.set_rollback(True)

        for name, existing, added in team_reports:
            self.stdout.write(f"{name}: 既存{existing}人 + 新規{added}人 = {existing + added}人")

        prefix = "[dry-run] " if options["dry_run"] else ""
        self.stdout.write(
            self.style.SUCCESS(
                f"{prefix}選手 {created_players}人 / 在籍 {created_stints}件 を作成しました"
                f"（うち海外出身 {foreign_count}人）"
            )
        )

    def _apply_back_names(self, team, new_players):
        """新規追加分を、既存の在籍者も含めたチーム内の同姓関係で背ネーム付けする。"""
        if not new_players:
            return

        existing_players = [
            s.player
            for s in PlayerStint.objects.filter(team=team, to_year__isnull=True)
            .select_related("player")
            .exclude(player_id__in=[p.id for p, _, _ in new_players])
        ]

        # 既存選手の苗字ローマ字は、保存済みの背ネームから復元する
        # （末尾側が苗字。ピリオドが付いていれば取り除く）。
        surname_romaji_by_id = {}
        given_initial_by_id = {}
        for p in existing_players:
            base = p.back_name.split(".")[-1] if p.back_name else ""
            surname_romaji_by_id[p.id] = base
            given_initial_by_id[p.id] = p.back_name[0] if "." in (p.back_name or "") else ""
        for player, surname_romaji, given_romaji in new_players:
            surname_romaji_by_id[player.id] = surname_romaji
            given_initial_by_id[player.id] = given_romaji[0] if given_romaji else ""

        counts = Counter(surname_romaji_by_id.values())

        to_update = []
        for player, _, _given_romaji in new_players:
            surname_romaji = surname_romaji_by_id[player.id]
            if counts[surname_romaji] > 1:
                player.back_name = f"{given_initial_by_id[player.id]}.{surname_romaji}"
            else:
                player.back_name = surname_romaji
            to_update.append(player)
        Player.objects.bulk_update(to_update, ["back_name"])

        # 同姓が新たに発生した既存選手側の背ネームも、頭文字付きに揃え直す
        stale = [
            p for p in existing_players if counts[surname_romaji_by_id[p.id]] > 1 and "." not in (p.back_name or "")
        ]
        for p in stale:
            p.back_name = f"{given_initial_by_id[p.id]}.{surname_romaji_by_id[p.id]}"
        if stale:
            Player.objects.bulk_update(stale, ["back_name"])

    def _refresh_schools(self, rng, *, dry_run):
        targets = list(players_in(REAL_SCOPE).filter(is_foreign_player=False).exclude(name__in=ORIGINAL_PLAYER_NAMES))
        to_update = []
        for player in targets:
            career = generator.amateur_career(rng, is_foreign=False, birthplace=player.birthplace)
            player.high_school = career.high_school
            player.university = career.university
            player.corporate_team = career.corporate_team
            to_update.append(player)

        if not dry_run:
            with transaction.atomic():
                Player.objects.bulk_update(to_update, ["high_school", "university", "corporate_team"], batch_size=500)

        prefix = "[dry-run] " if dry_run else ""
        self.stdout.write(
            self.style.SUCCESS(f"{prefix}選手 {len(to_update)}人の出身高校・大学・社会人チームを選び直しました")
        )

    def _rename_existing(self, rng, *, dry_run):
        targets = players_in(REAL_SCOPE).filter(is_foreign_player=False).exclude(name__in=ORIGINAL_PLAYER_NAMES)
        used_names = set(players_in(REAL_SCOPE).values_list("name", flat=True))
        renamed = 0

        with transaction.atomic():
            for player in targets:
                used_names.discard(player.name)
                new_name, name_kana, surname_romaji, given_romaji = generator.japanese_name(rng, used_names)
                if not dry_run:
                    player.name = new_name
                    player.name_kana = name_kana
                    player.save(update_fields=["name", "name_kana"])
                renamed += 1

            if dry_run:
                transaction.set_rollback(True)

        prefix = "[dry-run] " if dry_run else ""
        self.stdout.write(self.style.SUCCESS(f"{prefix}選手 {renamed}人の氏名を選び直しました"))
        if not dry_run:
            self.stdout.write("よみがな・背ネームは --assign-readings で付け直してください。")

    def _assign_readings(self, *, dry_run):
        """既存の氏名からよみがな・苗字ローマ字・名前ローマ字を復元し、背ネームを決める。

        新規に選び直すわけではなく、すでに確定している漢字名に対応する読みを
        プールから逆引きする。プールにない氏名（実在の元選手）は
        ORIGINAL_PLAYER_READINGS を使う。
        """
        surname_lookup = {kanji: (kana, romaji) for kanji, kana, romaji in pools.JP_SURNAMES + pools.RARE_JP_SURNAMES}
        given_lookup = {
            kanji: (kana, romaji) for kanji, kana, romaji in pools.JP_GIVEN_NAMES + pools.RARE_JP_GIVEN_NAMES
        }
        surnames_by_length = sorted(surname_lookup, key=len, reverse=True)

        foreign_given_lookup = {kana: romaji for group in pools.FOREIGN_GROUPS for kana, romaji in group["given"]}
        foreign_surname_lookup = {kana: romaji for group in pools.FOREIGN_GROUPS for kana, romaji in group["surname"]}

        players = list(players_in(REAL_SCOPE))
        surname_romaji_by_id = {}
        given_romaji_by_id = {}
        name_kana_by_id = {}
        unresolved = []

        for player in players:
            if player.name in ORIGINAL_PLAYER_READINGS:
                kana, surname_romaji, given_romaji = ORIGINAL_PLAYER_READINGS[player.name]
            elif player.is_foreign_player and "・" in player.name:
                given_kana, surname_kana = player.name.split("・", 1)
                given_romaji = foreign_given_lookup.get(given_kana)
                surname_romaji = foreign_surname_lookup.get(surname_kana)
                kana = player.name
                if given_romaji is None or surname_romaji is None:
                    unresolved.append(player.name)
                    continue
            else:
                match = None
                for surname_kanji in surnames_by_length:
                    if player.name.startswith(surname_kanji):
                        given_kanji = player.name[len(surname_kanji) :]
                        if given_kanji in given_lookup:
                            match = (surname_kanji, given_kanji)
                            break
                if match is None:
                    unresolved.append(player.name)
                    continue
                surname_kanji, given_kanji = match
                surname_kana, surname_romaji = surname_lookup[surname_kanji]
                given_kana, given_romaji = given_lookup[given_kanji]
                kana = surname_kana + given_kana

            name_kana_by_id[player.id] = kana
            surname_romaji_by_id[player.id] = surname_romaji
            given_romaji_by_id[player.id] = given_romaji

        # チームごとに同姓を数え、背ネームを決める（在籍していない選手は苗字のみ）。
        team_by_player_id = {s.player_id: s.team_id for s in stints_in(REAL_SCOPE).filter(to_year__isnull=True)}
        counts_by_team = defaultdict(Counter)
        for player_id, surname_romaji in surname_romaji_by_id.items():
            team_id = team_by_player_id.get(player_id)
            counts_by_team[team_id][surname_romaji] += 1

        to_update = []
        for player in players:
            if player.id not in name_kana_by_id:
                continue
            team_id = team_by_player_id.get(player.id)
            surname_romaji = surname_romaji_by_id[player.id]
            given_romaji = given_romaji_by_id[player.id]
            if counts_by_team[team_id][surname_romaji] > 1:
                back_name = f"{given_romaji[0]}.{surname_romaji}"
            else:
                back_name = surname_romaji

            player.name_kana = name_kana_by_id[player.id]
            player.back_name = back_name
            to_update.append(player)

        if not dry_run:
            with transaction.atomic():
                Player.objects.bulk_update(to_update, ["name_kana", "back_name"], batch_size=500)

        prefix = "[dry-run] " if dry_run else ""
        self.stdout.write(self.style.SUCCESS(f"{prefix}選手 {len(to_update)}人によみがな・背ネームを付与しました"))
        if unresolved:
            self.stdout.write(
                self.style.WARNING(f"読みを特定できず未対応のまま: {len(unresolved)}人 {unresolved[:10]}")
            )
