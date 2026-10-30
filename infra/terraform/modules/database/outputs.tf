output "address" {
  value = aws_db_instance.this.address
}

output "port" {
  value = aws_db_instance.this.port
}

output "db_name" {
  value = aws_db_instance.this.db_name
}

output "instance_id" {
  value = aws_db_instance.this.identifier
}

output "secret_arn" {
  value = aws_secretsmanager_secret.db.arn
}
