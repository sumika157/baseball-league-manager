from django.apps import AppConfig


class MyappConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "myapp"
    # 管理画面の見出し。既定だと 'Myapp' と表示されてしまう
    verbose_name = "野球データ"
