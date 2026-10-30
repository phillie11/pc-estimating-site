provider "aws" {
  region = "eu-west-2" # London

  # Refuse to run against any other AWS account (set aws_account_id in terraform.tfvars)
  allowed_account_ids = [var.aws_account_id]

  default_tags {
    tags = {
      Project   = "siteline"
      Env       = local.env
      ManagedBy = "terraform"
    }
  }
}
