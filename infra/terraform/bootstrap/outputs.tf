output "state_bucket" {
  description = "Use this in the -backend-config of terraform init for staging and prod"
  value       = aws_s3_bucket.state.bucket
}

output "init_command" {
  description = "Run this in infra/terraform/envs/staging and again in infra/terraform/envs/prod"
  value       = "terraform init -backend-config=\"bucket=${aws_s3_bucket.state.bucket}\""
}
