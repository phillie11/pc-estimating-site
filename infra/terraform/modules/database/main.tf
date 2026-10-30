# RDS PostgreSQL 16 in the private subnets, encrypted, SSL forced, with automated backups (point-in-time restore).
#
# Password: Terraform generates it and keeps it in Secrets Manager (the ECS tasks read it from there).
# We do not use the RDS-managed master password because RDS rotates that every 7 days and the app reads
# DB_PASSWORD once when a task starts, so every running task would lose its database a week later.

resource "aws_db_subnet_group" "this" {
  name        = var.name
  description = "Private subnets for ${var.name}"
  subnet_ids  = var.subnet_ids
}

resource "aws_db_parameter_group" "this" {
  name        = "${var.name}-pg16"
  family      = "postgres16"
  description = "Siteline: force SSL on every connection"

  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "pending-reboot"
  }

  # Log statements slower than 2 seconds, to find slow pages
  parameter {
    name  = "log_min_duration_statement"
    value = "2000"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "random_password" "db" {
  length  = 40
  special = false # letters and digits only: nothing RDS or a connection string can trip over
}

resource "aws_db_instance" "this" {
  identifier     = var.name
  engine         = "postgres"
  engine_version = var.engine_version
  instance_class = var.instance_class

  allocated_storage     = var.allocated_storage_gb
  max_allocated_storage = var.max_allocated_storage_gb
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = "siteline"
  username = "siteline_admin"
  password = random_password.db.result
  port     = 5432

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [var.security_group_id]
  parameter_group_name   = aws_db_parameter_group.this.name
  publicly_accessible    = false
  multi_az               = var.multi_az

  backup_retention_period  = var.backup_retention_days # also the point-in-time-restore window
  backup_window            = "01:30-02:30"             # UTC
  maintenance_window       = "sun:03:00-sun:04:00"     # UTC
  copy_tags_to_snapshot    = true
  delete_automated_backups = false

  auto_minor_version_upgrade  = true
  allow_major_version_upgrade = false
  apply_immediately           = var.apply_immediately

  deletion_protection       = var.deletion_protection
  skip_final_snapshot       = !var.final_snapshot
  final_snapshot_identifier = var.final_snapshot ? "${var.name}-final" : null

  tags = { Name = var.name }
}

resource "aws_secretsmanager_secret" "db" {
  name                    = "siteline/${var.env}/db"
  description             = "Siteline ${var.env} PostgreSQL login (used by the ECS tasks)"
  recovery_window_in_days = var.secret_recovery_days
}

resource "aws_secretsmanager_secret_version" "db" {
  secret_id = aws_secretsmanager_secret.db.id
  secret_string = jsonencode({
    username = aws_db_instance.this.username
    password = random_password.db.result
    host     = aws_db_instance.this.address
    port     = aws_db_instance.this.port
    dbname   = aws_db_instance.this.db_name
  })
}
