from django.contrib import admin

from .infrastructure.orm_models import (
    League,
    PitcherStats,
    Player,
    PlayerStats,
    Team,
)

# サイト側で registration/password_change_*.html を上書きしているため、
# 同名テンプレートを使う管理画面までサイトの見た目になってしまう。
# 管理画面には管理画面の体裁を保たせる。
admin.site.password_change_template = 'admin/password_change_form.html'
admin.site.password_change_done_template = 'admin/password_change_done.html'

# 管理画面で野球のデータを作成・編集できるように登録
admin.site.register(League)
admin.site.register(Team)
admin.site.register(Player)
admin.site.register(PlayerStats)
admin.site.register(PitcherStats)
