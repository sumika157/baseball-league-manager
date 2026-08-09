from django.contrib import admin

from .infrastructure.orm_models import (
    League,
    PitcherStats,
    Player,
    PlayerStats,
    Team,
)

# 管理画面で野球のデータを作成・編集できるように登録
admin.site.register(League)
admin.site.register(Team)
admin.site.register(Player)
admin.site.register(PlayerStats)
admin.site.register(PitcherStats)
