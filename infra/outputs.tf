output "public_url" {
  description = "公開 URL"
  value       = "https://${local.hostname}/"
}

output "instance_ip" {
  description = "VM の公開 IP（SSH 先。ubuntu ユーザー）"
  value       = aws_lightsail_instance.app.public_ip_address
}

output "backup_bucket" {
  description = "バックアップの R2 バケット"
  value       = cloudflare_r2_bucket.backup.name
}

output "turnstile_site_key" {
  description = "Turnstile のサイトキー（公開してよい値）"
  value       = cloudflare_turnstile_widget.this.sitekey
}
