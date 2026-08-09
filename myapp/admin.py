from django.contrib import admin
from .models import League, Team, Player, PlayerStats

# 管理画面で野球のデータを作成・編集できるように登録
admin.site.register(League)
admin.site.register(Team)
admin.site.register(Player)
admin.site.register(PlayerStats)