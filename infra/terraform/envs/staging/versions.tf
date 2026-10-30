terraform {
  required_version = ">= 1.10.0, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.69"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.9"
    }
  }

  # State lives in the bucket made by infra/terraform/bootstrap. The bucket name contains your
  # AWS account number, so it is given when you run init (infra/README.md step 9):
  #   terraform init -backend-config="bucket=siteline-terraform-state-123456789012"
  backend "s3" {
    key          = "siteline/staging/terraform.tfstate"
    region       = "eu-west-2"
    encrypt      = true
    use_lockfile = true # locking via a lock file in the same bucket (no DynamoDB table needed)
  }
}
