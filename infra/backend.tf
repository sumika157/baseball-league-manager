# state は Cloudflare R2 の非公開バケット（S3 互換）に置く。バケットだけは手で作る（Wiki「本番公開」）。
# 接続先は backend.hcl に書き、`terraform init -backend-config=backend.hcl` で渡す
# （アカウント ID などをコードに埋めないため。雛形は backend.hcl.example）。
# R2 のキーも backend.hcl に書く（環境変数 AWS_* は Lightsail の認証に使うので、衝突させない）。
terraform {
  backend "s3" {}
}
