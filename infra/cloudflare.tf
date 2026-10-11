locals {
  hostname = var.hostname != "" ? var.hostname : var.zone_name
}

# 既存のゾーン（ドメインの購入とネームサーバーの向け先は手作業）
data "cloudflare_zone" "this" {
  filter = {
    name = var.zone_name
  }
}

# ---- HTTPS ----

# 「Always Use HTTPS」。HSTS は Django が出すので Cloudflare 側では有効にしない（二重になるため）
resource "cloudflare_zone_setting" "always_use_https" {
  zone_id    = data.cloudflare_zone.this.zone_id
  setting_id = "always_use_https"
  value      = "on"
}

# ---- トンネル（設定はリモート管理）と DNS ----

resource "random_id" "tunnel_secret" {
  byte_length = 32
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "this" {
  account_id    = var.cloudflare_account_id
  name          = var.instance_name
  config_src    = "cloudflare"
  tunnel_secret = random_id.tunnel_secret.b64_std
}

# 公開するホスト名 → web:8000。それ以外は 404
resource "cloudflare_zero_trust_tunnel_cloudflared_config" "this" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.this.id

  config = {
    ingress = [
      {
        hostname = local.hostname
        service  = "http://web:8000"
      },
      {
        service = "http_status:404"
      },
    ]
  }
}

# cloudflared に渡すトークン（.env の CLOUDFLARE_TUNNEL_TOKEN）。state にだけ残る
data "cloudflare_zero_trust_tunnel_cloudflared_token" "this" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.this.id
}

resource "cloudflare_dns_record" "app" {
  zone_id = data.cloudflare_zone.this.zone_id
  name    = local.hostname
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.this.id}.cfargotunnel.com"
  proxied = true
  ttl     = 1 # 自動（プロキシ有効のときは 1 しか指定できない）
  comment = "Terraform: cloudflared のトンネル"
}

# ---- /admin/ を Cloudflare Access で守る ----
# 前提（手作業）: Zero Trust の組織が作ってあること（無料プランで足りる）。認証はワンタイムコード（メール）。

resource "cloudflare_zero_trust_access_policy" "admin" {
  account_id = var.cloudflare_account_id
  name       = "${var.instance_name}-admin"
  decision   = "allow"

  include = [for email in var.admin_emails : { email = { email = email } }]
}

resource "cloudflare_zero_trust_access_application" "admin" {
  account_id       = var.cloudflare_account_id
  name             = "${var.instance_name}-admin"
  type             = "self_hosted"
  session_duration = "24h"

  destinations = [
    {
      type = "public"
      uri  = "${local.hostname}/admin"
    },
  ]

  policies = [
    {
      id         = cloudflare_zero_trust_access_policy.admin.id
      precedence = 1
    },
  ]
}

# ---- /accounts/login/ の回数制限（総当たり対策）----
# 無料プランの制約（Cloudflare のドキュメント）: 規則は 1 つ、条件に使えるのはパスと Verified Bot だけ、
# 数えるのは IP、計測期間と遮断時間は 10 秒固定。なのでメソッド（POST）は条件にせず、GET も数える。
resource "cloudflare_ruleset" "login_rate_limit" {
  count = var.login_rate_limit_enabled ? 1 : 0

  zone_id     = data.cloudflare_zone.this.zone_id
  name        = "${var.instance_name}-login-rate-limit"
  description = "Terraform: /accounts/login/ への総当たりを抑える"
  kind        = "zone"
  phase       = "http_ratelimit"

  rules = [
    {
      action      = "block"
      description = "login rate limit"
      enabled     = true
      expression = var.login_rate_limit_post_only ? (
        "(http.request.uri.path eq \"/accounts/login/\" and http.request.method eq \"POST\")"
        ) : (
        "(http.request.uri.path eq \"/accounts/login/\")"
      )
      ratelimit = {
        characteristics     = ["cf.colo.id", "ip.src"]
        period              = 10
        requests_per_period = var.login_rate_limit_requests
        mitigation_timeout  = 10
      }
    },
  ]
}

# ---- Turnstile（まだアプリは使っていない。サイトキーと秘密は .env に書いておく）----

resource "cloudflare_turnstile_widget" "this" {
  account_id = var.cloudflare_account_id
  name       = var.turnstile_widget_name
  domains    = [local.hostname]
  mode       = "managed"
}

# ---- バックアップ用の R2 ----

resource "cloudflare_r2_bucket" "backup" {
  account_id = var.cloudflare_account_id
  name       = var.backup_bucket_name
  location   = var.backup_bucket_location
}

# aws s3 sync は R2 側のファイルを消さないので、規則が無いと写しが増え続ける
resource "cloudflare_r2_bucket_lifecycle" "backup" {
  account_id  = var.cloudflare_account_id
  bucket_name = cloudflare_r2_bucket.backup.name

  rules = [
    {
      id         = "expire-backups"
      enabled    = true
      conditions = { prefix = "baseball-backups/" }
      delete_objects_transition = {
        condition = {
          type    = "Age"
          max_age = var.backup_retention_days * 86400 # 秒
        }
      }
    },
  ]
}

# R2 のこのバケットだけに読み書きできる API トークン。
# S3 互換のキーは、アクセスキー ID = トークンの id、シークレット = トークンの value の SHA-256
# （Cloudflare のドキュメント「R2 API tokens」）。手入力にするなら変数 backup_r2_* を埋める。
data "cloudflare_account_api_token_permission_groups_list" "r2_write" {
  account_id = var.cloudflare_account_id
  name       = "Workers R2 Storage Bucket Item Write"
}

resource "cloudflare_account_token" "backup" {
  count = var.backup_r2_access_key_id == "" ? 1 : 0

  account_id = var.cloudflare_account_id
  name       = "${var.instance_name}-backup-r2"

  policies = [
    {
      effect = "allow"
      permission_groups = [
        { id = data.cloudflare_account_api_token_permission_groups_list.r2_write.result[0].id },
      ]
      resources = jsonencode({
        "com.cloudflare.edge.r2.bucket.${var.cloudflare_account_id}_default_${cloudflare_r2_bucket.backup.name}" = "*"
      })
    },
  ]
}

locals {
  backup_access_key_id = var.backup_r2_access_key_id != "" ? var.backup_r2_access_key_id : cloudflare_account_token.backup[0].id
  backup_secret_access_key = (
    var.backup_r2_access_key_id != "" ? var.backup_r2_secret_access_key : sha256(cloudflare_account_token.backup[0].value)
  )
}
