# 認証情報はコードにも tfvars にも書かず、環境変数か ~/.aws のプロファイルで渡す（手順は Wiki「本番公開」）。
#   AWS（Lightsail） : AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY、または AWS_PROFILE（~/.aws の設定）
#   Cloudflare       : CLOUDFLARE_API_TOKEN
# state の R2 のキーは AWS_* と衝突するので、環境変数ではなく backend.hcl に書く（backend.hcl.example）。
provider "aws" {
  region = var.aws_region
}

provider "cloudflare" {}
