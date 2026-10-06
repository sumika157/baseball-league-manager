resource "aws_lightsail_key_pair" "admin" {
  name       = "${var.instance_name}-admin"
  public_key = var.ssh_public_key
}

# DB（SQLite のボリューム）が載る VM。消えると DB も消えるので destroy を止める。
# どうしても作り直すときは、バックアップを確かめてから、この lifecycle を一時的に外す。
resource "aws_lightsail_instance" "app" {
  name              = var.instance_name
  availability_zone = var.availability_zone
  blueprint_id      = var.blueprint_id
  bundle_id         = var.bundle_id
  key_pair_name     = aws_lightsail_key_pair.admin.name
  ip_address_type   = "ipv4"

  # 初期設定のみ。秘密は入れない（Lightsail のコンソールから user_data が見えるため）
  user_data = file("${path.module}/templates/cloud-init.sh")

  lifecycle {
    prevent_destroy = true
  }
}

# ポートは SSH（22）だけ。HTTP/HTTPS は Cloudflare Tunnel が VM の内側から接続するので開けない
resource "aws_lightsail_instance_public_ports" "app" {
  instance_name = aws_lightsail_instance.app.name

  port_info {
    protocol          = "tcp"
    from_port         = 22
    to_port           = 22
    cidrs             = var.admin_ssh_cidrs
    cidr_list_aliases = var.allow_lightsail_browser_ssh ? ["lightsail-connect"] : []
  }
}
