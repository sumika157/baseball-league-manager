"""ワークスペースを、実データ（リーグ・チーム・選手・試合）・メンバー・招待ごと消す（運営者向け。W4 #80）。

    docker compose exec web python manage.py workspace_delete --workspace <key>
    docker compose exec web python manage.py workspace_delete --workspace <key> --yes   # 確認を省く

消す前に件数を出して確認する（`yes` と打つと消す）。ペナントの世界が1つでも残っていれば消せない
（先に `pennant_delete --world <id>` で消す）。ほかのワークスペースにも在籍の記録がある選手がいるときと、
ほかのワークスペースの行がこのワークスペースの行を外せない形で指しているとき（試合の相手など）も消せない。
在籍の記録が無い選手はどのワークスペースにも属さないので消えずに残る。元に戻せないので、必要なら先に
`backup_db` でバックアップを取る。
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from myapp.domain.exceptions import DomainError
from myapp.management.workspaces import add_workspace_argument, real_scope_for_key
from myapp.presentation.views import build_workspace_management_query, build_workspace_service

CONFIRMATION = "yes"


class Command(BaseCommand):
    help = "ワークスペースを、実データ・メンバー・招待ごと消す"

    def add_arguments(self, parser: Any) -> None:
        add_workspace_argument(parser, help="消すワークスペースの key（必須）")
        parser.add_argument("--yes", action="store_true", help="確認を省いて消す")

    def handle(self, *args: Any, **options: Any) -> None:
        key = options["workspace"]
        workspace_id = real_scope_for_key(key).workspace_id
        contents = build_workspace_management_query(workspace_id).contents()
        if (reason := contents.deletion_blocker) is not None:
            worlds = "\n".join(
                f"  - id={world.world_id} {world.name}（オーナー: {world.owner_name or 'なし'}）"
                for world in contents.worlds
            )
            raise CommandError(f"{reason}\n{worlds}\n世界は pennant_delete --world <id> で消せます。")
        self.stdout.write(
            f"ワークスペース {key} を消します: リーグ {contents.league_count}・チーム {contents.team_count}・"
            f"選手 {contents.player_count}・試合 {contents.game_count}（メンバーと招待も消えます。元に戻せません）"
        )
        if not options["yes"]:
            try:
                answer = input(f"消すには {CONFIRMATION} と入力してください: ")
            except EOFError:  # 入力の無い実行（`exec -T` など）は、確かめられないので取りやめる
                answer = ""
            if answer.strip() != CONFIRMATION:
                raise CommandError("取りやめました（何も消していません）。")
        try:
            build_workspace_service().delete(workspace_id)
        except DomainError as error:
            raise CommandError(str(error)) from error
        self.stdout.write(self.style.SUCCESS(f"ワークスペース {key} を消しました"))
