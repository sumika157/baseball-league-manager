# アプリの起動まで（初回）。アプリの更新（デプロイ）は Terraform では行わない（Wiki「本番公開」の「デプロイ」）。
# 秘密（.env・R2 のキー）は SSH で VM に書く。cloud-init には入れない。秘密が残るのは state だけ。

# Django の秘密鍵。.env の値に $ を含めてはいけない（Docker Compose が展開して壊す）ので記号は使わない
resource "random_password" "django_secret_key" {
  length  = 64
  special = false
}

locals {
  app_dir = "/home/ubuntu/baseball-league-manager"

  env_values = {
    secret_key           = random_password.django_secret_key.result
    hostname             = local.hostname
    tunnel_token         = data.cloudflare_zero_trust_tunnel_cloudflared_token.this.token
    turnstile_site_key   = cloudflare_turnstile_widget.this.sitekey
    turnstile_secret_key = cloudflare_turnstile_widget.this.secret
  }

  env_file = templatefile("${path.module}/templates/env.tftpl", local.env_values)

  aws_credentials = templatefile("${path.module}/templates/aws-credentials.tftpl", {
    access_key_id     = local.backup_access_key_id
    secret_access_key = local.backup_secret_access_key
  })

  aws_config = file("${path.module}/templates/aws-config.tftpl")

  crontab = templatefile("${path.module}/templates/crontab.tftpl", {
    app_dir    = local.app_dir
    bucket     = cloudflare_r2_bucket.backup.name
    account_id = var.cloudflare_account_id
  })
}

resource "terraform_data" "bootstrap" {
  # .env などの中身が変わったら再実行する（ハッシュだけを持つ。中身は渡さない）
  triggers_replace = [
    aws_lightsail_instance.app.id,
    sha256(local.env_file),
    sha256(local.aws_credentials),
    sha256(local.aws_config),
    sha256(local.crontab),
    var.repo_url,
    var.repo_branch,
  ]

  lifecycle {
    precondition {
      condition     = nonsensitive(alltrue([for v in values(local.env_values) : !strcontains(v, "$")]))
      error_message = ".env に書く値に $ が含まれています。Docker Compose が展開して壊すので、$ を含まない値にしてください。"
    }
  }

  connection {
    type        = "ssh"
    host        = aws_lightsail_instance.app.public_ip_address
    user        = "ubuntu"
    private_key = file(var.ssh_private_key_path)
    timeout     = "10m"
  }

  # cloud-init（Docker などの導入）が終わるまで待つ
  provisioner "remote-exec" {
    inline = [
      "for i in $(seq 1 120); do [ -f /var/lib/baseball-init-done ] && exit 0; sleep 10; done",
      "echo 'VM の初期設定が終わっていません（/var/log/baseball-init.log を見る）' >&2; exit 1",
    ]
  }

  # ソース（公開リポジトリなので認証は要らない）。backups/ が先にあるので clone ではなく fetch で取る
  provisioner "remote-exec" {
    inline = [
      "cd ${local.app_dir}",
      "[ -d .git ] || git init -q",
      "git remote remove origin 2>/dev/null || true",
      "git remote add origin ${var.repo_url}",
      "git fetch --depth 1 origin ${var.repo_branch}",
      "git checkout -q -f -B ${var.repo_branch} FETCH_HEAD",
    ]
  }

  # 秘密は一時ファイルに置いて 600 で本来の場所へ移す（ホームは 750 なので他の利用者には見えない）
  provisioner "file" {
    content     = local.env_file
    destination = "/home/ubuntu/.env.upload"
  }

  provisioner "file" {
    content     = local.aws_credentials
    destination = "/home/ubuntu/.aws-credentials.upload"
  }

  provisioner "file" {
    content     = local.aws_config
    destination = "/home/ubuntu/.aws-config.upload"
  }

  provisioner "file" {
    content     = local.crontab
    destination = "/home/ubuntu/.crontab.upload"
  }

  provisioner "remote-exec" {
    inline = [
      "install -m 600 /home/ubuntu/.env.upload ${local.app_dir}/.env",
      "mkdir -p /home/ubuntu/.aws && chmod 700 /home/ubuntu/.aws",
      "install -m 600 /home/ubuntu/.aws-credentials.upload /home/ubuntu/.aws/credentials",
      "install -m 600 /home/ubuntu/.aws-config.upload /home/ubuntu/.aws/config",
      "crontab /home/ubuntu/.crontab.upload",
      "rm -f /home/ubuntu/.env.upload /home/ubuntu/.aws-credentials.upload /home/ubuntu/.aws-config.upload /home/ubuntu/.crontab.upload",
    ]
  }

  # 起動（イメージは GHCR から pull。初回の migrate は entrypoint が行う）
  provisioner "remote-exec" {
    inline = [
      "cd ${local.app_dir}",
      "docker compose -f docker-compose.prod.yml pull",
      "docker compose -f docker-compose.prod.yml up -d",
    ]
  }

  depends_on = [
    aws_lightsail_instance_public_ports.app,
    cloudflare_zero_trust_tunnel_cloudflared_config.this,
    cloudflare_dns_record.app,
    cloudflare_r2_bucket_lifecycle.backup,
  ]
}
