# ---- Cloudflare ----

variable "cloudflare_account_id" {
  description = "Cloudflare のアカウント ID（ダッシュボードの URL か、R2 の画面に出ている）"
  type        = string
}

variable "zone_name" {
  description = "Cloudflare に追加済みのゾーン（ドメイン）の名前。例: example.com"
  type        = string
}

variable "hostname" {
  description = "公開するホスト名。空ならゾーンの名前そのもの（apex）を使う。例: baseball.example.com"
  type        = string
  default     = ""
}

variable "admin_emails" {
  description = "/admin/ を Cloudflare Access で通すメールアドレス（ワンタイムコードで認証する）"
  type        = list(string)

  validation {
    condition     = length(var.admin_emails) > 0
    error_message = "admin_emails には 1 つ以上のメールアドレスを指定してください（空だと /admin/ に誰も入れません）。"
  }
}

variable "login_rate_limit_enabled" {
  description = "/accounts/login/ への回数制限の規則を作るか"
  type        = bool
  default     = true
}

variable "login_rate_limit_requests" {
  description = "回数制限: 計測期間（10 秒。無料プランで選べる唯一の値）に許すリクエスト数。無料プランでは GET も数える"
  type        = number
  default     = 5
}

variable "login_rate_limit_post_only" {
  description = "true なら POST だけを数える。メソッドを条件にできるのは有料プランだけ（無料プランで true にすると apply が失敗する）"
  type        = bool
  default     = false
}

variable "turnstile_widget_name" {
  description = "Turnstile のウィジェットの名前"
  type        = string
  default     = "baseball-league-manager"
}

variable "backup_bucket_name" {
  description = "DB のバックアップを置く R2 のバケット名（state のバケットとは別）"
  type        = string
  default     = "baseball-backups"
}

variable "backup_retention_days" {
  description = "R2 のバックアップを何日で削除するか（無料枠は 10GB）"
  type        = number
  default     = 30
}

variable "backup_bucket_location" {
  description = "R2 のバケットの場所のヒント（apac・eur・enam・weur・wnam・oc）。null なら Cloudflare に任せる"
  type        = string
  default     = null
}

variable "backup_r2_access_key_id" {
  description = "手入力にするときだけ: バックアップ用の R2 のアクセスキー ID。空なら Terraform が R2 専用の API トークンを作ってそこから導く"
  type        = string
  default     = ""
}

variable "backup_r2_secret_access_key" {
  description = "手入力にするときだけ: 上のシークレット"
  type        = string
  default     = ""
  sensitive   = true
}

# ---- AWS / Lightsail ----

variable "aws_region" {
  description = "Lightsail のリージョン"
  type        = string
  default     = "ap-northeast-1"
}

variable "availability_zone" {
  description = "Lightsail のアベイラビリティゾーン（aws_region の中）"
  type        = string
  default     = "ap-northeast-1a"
}

variable "instance_name" {
  description = "Lightsail のインスタンス名"
  type        = string
  default     = "baseball-league-manager"
}

# 確かめ方: aws lightsail get-blueprints --region <リージョン> / aws lightsail get-bundles --region <リージョン>
# （1GB・IPv4 付きは micro_3_0。512MB は nano_3_0）
variable "blueprint_id" {
  description = "OS のイメージ（Ubuntu 24.04 LTS）。aws lightsail get-blueprints で確かめる"
  type        = string
  default     = "ubuntu_24_04"
}

variable "bundle_id" {
  description = "プラン（1GB メモリ・IPv4 付き）。aws lightsail get-bundles で確かめる"
  type        = string
  default     = "micro_3_0"
}

variable "ssh_public_key" {
  description = "VM に登録する SSH の公開鍵（ssh-ed25519 AAAA... の1行）"
  type        = string
}

variable "ssh_private_key_path" {
  description = "Terraform が VM へ SSH するときの秘密鍵のファイルのパス。make tf ではコンテナの /ssh/<ファイル名>（~/.ssh を読み取り専用でマウントしている）"
  type        = string
  default     = "/ssh/id_ed25519"
}

variable "admin_ssh_cidrs" {
  description = "SSH（22）を許す接続元の CIDR。terraform apply を流す場所の IP を必ず含める（起動のために SSH で入るため）。例: [\"203.0.113.10/32\"]"
  type        = list(string)

  validation {
    condition     = length(var.admin_ssh_cidrs) > 0
    error_message = "admin_ssh_cidrs が空だと Terraform が VM に入れません。apply を流す場所の IP を指定してください。"
  }
}

variable "allow_lightsail_browser_ssh" {
  description = "Lightsail のブラウザ SSH からの接続も許すか"
  type        = bool
  default     = true
}

# ---- アプリ ----

variable "repo_url" {
  description = "clone するリポジトリ（公開なので認証は要らない）"
  type        = string
  default     = "https://github.com/sumika157/baseball-league-manager.git"
}

variable "repo_branch" {
  description = "起動に使うブランチ（compose と手順だけ。イメージは GHCR の latest）"
  type        = string
  default     = "main"
}
