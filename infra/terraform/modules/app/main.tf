# ECR, ECS Fargate (web, worker, one-off and scheduled tasks), ALB with HTTPS, logs and IAM.
#
# How images get deployed: Terraform builds every task definition from the image tag stored in the SSM
# parameter /siteline/<env>/image_tag. infra/scripts/deploy.sh pushes a new image, writes that parameter,
# registers new task definition revisions, runs migrations and updates the services. `track_latest`
# lets Terraform accept those revisions, so a later `terraform apply` does not roll the image back.

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  container  = "app" # the container name in every task definition (deploy.sh and run-command.sh rely on it)

  image_tag = aws_ssm_parameter.image_tag.insecure_value
  image     = "${aws_ecr_repository.this.repository_url}:${local.image_tag}"

  environment = [for k in sort(keys(var.app_environment)) : { name = k, value = var.app_environment[k] }]
  secrets     = [for k in sort(keys(var.app_secrets)) : { name = k, valueFrom = var.app_secrets[k] }]

  # Every task definition. Web has no command (the image runs gunicorn); the rest run manage.py.
  task_families = merge(
    {
      web     = { command = null, cpu = var.web_cpu, memory = var.web_memory }
      worker  = { command = var.worker_command, cpu = var.worker_cpu, memory = var.worker_memory }
      migrate = { command = ["python", "manage.py", "migrate", "--noinput"], cpu = var.job_cpu, memory = var.job_memory }
      # For one-off commands (infra/scripts/run-command.sh overrides the command)
      manage = { command = ["python", "manage.py", "check", "--deploy"], cpu = var.job_cpu, memory = var.job_memory }
    },
    { for k, job in var.scheduled_jobs : k => { command = job.command, cpu = var.job_cpu, memory = var.job_memory } },
  )
}

# --- Container registry --------------------------------------------------------

resource "aws_ecr_repository" "this" {
  name                 = var.name
  image_tag_mutability = "IMMUTABLE" # a tag always means the same image, so rollbacks are exact

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "this" {
  repository = aws_ecr_repository.this.name
  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Delete untagged images after 7 days"
        selection    = { tagStatus = "untagged", countType = "sinceImagePushed", countUnit = "days", countNumber = 7 }
        action       = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep the ${var.ecr_keep_images} most recent images (older ones cannot be rolled back to)"
        selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = var.ecr_keep_images }
        action       = { type = "expire" }
      },
    ]
  })
}

resource "aws_ssm_parameter" "image_tag" {
  name           = "/siteline/${var.env}/image_tag"
  description    = "Image tag currently deployed to ${var.env}; written by infra/scripts/deploy.sh"
  type           = "String"
  insecure_value = "not-deployed-yet"

  lifecycle {
    ignore_changes = [insecure_value]
  }
}

# --- Logs ----------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${var.name}"
  retention_in_days = var.log_retention_days
}

# --- ECS cluster -----------------------------------------------------------------

resource "aws_ecs_cluster" "this" {
  name = var.name

  setting {
    name  = "containerInsights"
    value = "disabled" # extra CloudWatch cost; turn on if you need per-task CPU/memory graphs
  }

  configuration {
    execute_command_configuration {
      logging = "DEFAULT"
    }
  }
}

resource "aws_ecs_cluster_capacity_providers" "this" {
  cluster_name       = aws_ecs_cluster.this.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }
}

# --- IAM: execution role (pulls the image, reads secrets, writes logs) ----------------

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${var.name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "execution_secrets" {
  name = "read-siteline-secrets"
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "ReadThisEnvironmentsSecrets"
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = var.secret_arns
    }]
  })
}

# --- IAM: task role (what the app itself may do) -----------------------------------

resource "aws_iam_role" "task" {
  name               = "${var.name}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

# Least privilege on the uploaded-files bucket: the same actions as the README's siteline-files-access policy
resource "aws_iam_role_policy" "task_files" {
  name = "siteline-files-access"
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ListBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = "arn:aws:s3:::${var.files_bucket_name}"
      },
      {
        Sid      = "Files"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:GetObjectTagging"]
        Resource = "arn:aws:s3:::${var.files_bucket_name}/*"
      },
    ]
  })
}

# --- Transfer bucket: hands the database export to a one-off task during the data move ------
# Private, encrypted, and everything in it is deleted automatically after 7 days.

resource "aws_s3_bucket" "transfer" {
  bucket        = "${var.name}-transfer-${local.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "transfer" {
  bucket                  = aws_s3_bucket.transfer.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "transfer" {
  bucket = aws_s3_bucket.transfer.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "transfer" {
  bucket = aws_s3_bucket.transfer.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "transfer" {
  bucket = aws_s3_bucket.transfer.id
  rule {
    id     = "delete-after-7-days"
    status = "Enabled"
    filter {}
    expiration {
      days = 7
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

resource "aws_s3_bucket_policy" "transfer" {
  bucket = aws_s3_bucket.transfer.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "HttpsOnly"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.transfer.arn, "${aws_s3_bucket.transfer.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.transfer]
}

resource "aws_iam_role_policy" "task_transfer" {
  name = "read-transfer-bucket"
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "ReadTransferFiles"
      Effect   = "Allow"
      Action   = ["s3:GetObject"]
      Resource = "${aws_s3_bucket.transfer.arn}/*"
    }]
  })
}

# ECS Exec (infra/scripts/run-command.sh --exec) needs these
resource "aws_iam_role_policy" "task_exec" {
  name = "ecs-exec"
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "ssmmessages:CreateControlChannel",
        "ssmmessages:CreateDataChannel",
        "ssmmessages:OpenControlChannel",
        "ssmmessages:OpenDataChannel",
      ]
      Resource = "*"
    }]
  })
}

# --- Task definitions ----------------------------------------------------------------

resource "aws_ecs_task_definition" "this" {
  for_each = local.task_families

  family                   = "${var.name}-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(each.value.cpu)
  memory                   = tostring(each.value.memory)
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = var.cpu_architecture
  }

  container_definitions = jsonencode([merge({
    name        = local.container
    image       = local.image
    essential   = true
    environment = local.environment
    secrets     = local.secrets
    stopTimeout = 30
    linuxParameters = {
      initProcessEnabled = true # tidy shutdowns, and needed for ECS Exec
    }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app.name
        awslogs-region        = var.region
        awslogs-stream-prefix = each.key
      }
    }
    },
    # Only include what is set (null/empty fields make Terraform see changes that are not there):
    # web has no command (the image's own CMD runs gunicorn) and is the only one with a port
    { for k, v in {
      command      = each.value.command
      portMappings = each.key == "web" ? [{ containerPort = var.app_port, protocol = "tcp" }] : null
    } : k => v if v != null },
  )])

  # deploy.sh registers new revisions with a new image; keep following the latest one
  track_latest = true
  # Keep old revisions registered so a deploy can be rolled back
  skip_destroy = true
}

# --- Load balancer + HTTPS -----------------------------------------------------------

resource "aws_acm_certificate" "app" {
  domain_name       = var.app_domain
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

# Waits until the validation CNAME has been added at the DNS host and ACM has issued the certificate
resource "aws_acm_certificate_validation" "app" {
  certificate_arn         = aws_acm_certificate.app.arn
  validation_record_fqdns = [for o in aws_acm_certificate.app.domain_validation_options : o.resource_record_name]

  timeouts {
    create = "45m"
  }
}

resource "aws_lb" "this" {
  name                       = var.name
  load_balancer_type         = "application"
  internal                   = false
  subnets                    = var.public_subnet_ids
  security_groups            = [var.alb_security_group_id]
  idle_timeout               = 60
  drop_invalid_header_fields = true
  enable_deletion_protection = var.alb_deletion_protection
}

resource "aws_lb_target_group" "web" {
  name                 = "${var.name}-web"
  vpc_id               = var.vpc_id
  target_type          = "ip"
  port                 = var.app_port
  protocol             = "HTTP"
  deregistration_delay = 30

  health_check {
    path                = var.health_check_path
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.this.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.app.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.web.arn
  }
}

# --- Services ----------------------------------------------------------------------

resource "aws_ecs_service" "web" {
  name                   = "web"
  cluster                = aws_ecs_cluster.this.id
  task_definition        = aws_ecs_task_definition.this["web"].arn
  desired_count          = var.web_desired_count
  launch_type            = "FARGATE"
  enable_execute_command = true
  propagate_tags         = "SERVICE"

  health_check_grace_period_seconds  = 60
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.public_subnet_ids
    security_groups  = [var.app_security_group_id]
    assign_public_ip = true # lets tasks reach ECR, Secrets Manager, SES and Stripe without a NAT gateway
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.web.arn
    container_name   = local.container
    container_port   = var.app_port
  }

  wait_for_steady_state = false

  depends_on = [aws_lb_listener.https]
}

resource "aws_ecs_service" "worker" {
  name                   = "worker"
  cluster                = aws_ecs_cluster.this.id
  task_definition        = aws_ecs_task_definition.this["worker"].arn
  desired_count          = var.worker_desired_count
  launch_type            = "FARGATE"
  enable_execute_command = true
  propagate_tags         = "SERVICE"

  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.public_subnet_ids
    security_groups  = [var.app_security_group_id]
    assign_public_ip = true
  }

  wait_for_steady_state = false
}

# --- Scheduled tasks (EventBridge Scheduler) -----------------------------------------

resource "aws_scheduler_schedule_group" "this" {
  name = var.name
}

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.name}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "scheduler" {
  name = "run-scheduled-tasks"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "RunScheduledTasks"
        Effect   = "Allow"
        Action   = ["ecs:RunTask"]
        Resource = [for k in keys(var.scheduled_jobs) : "${aws_ecs_task_definition.this[k].arn_without_revision}:*"]
        Condition = {
          ArnEquals = { "ecs:cluster" = aws_ecs_cluster.this.arn }
        }
      },
      {
        Sid      = "TagTasks"
        Effect   = "Allow"
        Action   = ["ecs:TagResource"]
        Resource = "*"
        Condition = {
          StringEquals = { "ecs:CreateAction" = "RunTask" }
        }
      },
      {
        Sid      = "PassTaskRoles"
        Effect   = "Allow"
        Action   = ["iam:PassRole"]
        Resource = [aws_iam_role.execution.arn, aws_iam_role.task.arn]
        Condition = {
          StringLike = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" }
        }
      },
    ]
  })
}

resource "aws_scheduler_schedule" "job" {
  for_each = var.scheduled_jobs

  name                         = each.key
  group_name                   = aws_scheduler_schedule_group.this.name
  description                  = "manage.py ${join(" ", slice(each.value.command, 2, length(each.value.command)))}"
  schedule_expression          = each.value.schedule
  schedule_expression_timezone = "UTC"
  state                        = var.enable_schedules ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_ecs_cluster.this.arn
    role_arn = aws_iam_role.scheduler.arn

    ecs_parameters {
      # No revision number: every run uses the latest revision, i.e. whatever deploy.sh last rolled out
      task_definition_arn     = aws_ecs_task_definition.this[each.key].arn_without_revision
      launch_type             = "FARGATE"
      task_count              = 1
      enable_ecs_managed_tags = true
      propagate_tags          = "TASK_DEFINITION"

      network_configuration {
        subnets          = var.public_subnet_ids
        security_groups  = [var.app_security_group_id]
        assign_public_ip = true
      }
    }

    retry_policy {
      maximum_retry_attempts       = 2
      maximum_event_age_in_seconds = 3600
    }
  }
}

# --- What deploy.sh / run-command.sh / GitHub Actions need to know ----------------------

resource "aws_ssm_parameter" "deploy_config" {
  name        = "/siteline/${var.env}/deploy"
  description = "Read by infra/scripts/deploy.sh and run-command.sh (not secret)"
  type        = "String"
  insecure_value = jsonencode({
    region          = var.region
    cluster         = aws_ecs_cluster.this.name
    repository_url  = aws_ecr_repository.this.repository_url
    container       = local.container
    families        = { for k, td in aws_ecs_task_definition.this : k => td.family }
    services        = { web = aws_ecs_service.web.name, worker = aws_ecs_service.worker.name }
    subnets         = var.public_subnet_ids
    security_groups = [var.app_security_group_id]
    log_group       = aws_cloudwatch_log_group.app.name
    cpu_arch        = var.cpu_architecture
    transfer_bucket = aws_s3_bucket.transfer.bucket
  })
}

# --- GitHub Actions deploy role (OpenID Connect, no stored keys) -------------------------

data "aws_iam_policy_document" "github_assume" {
  count = var.github_repository == "" ? 0 : 1

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = ["arn:aws:iam::${local.account_id}:oidc-provider/token.actions.githubusercontent.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    # Only jobs in this repository that run in the GitHub environment named after this env
    # (the "prod" environment is limited to the main branch in GitHub's settings)
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:environment:${var.env}"]
    }
  }
}

resource "aws_iam_role" "github_deploy" {
  count = var.github_repository == "" ? 0 : 1

  name                 = "${var.name}-github-deploy"
  assume_role_policy   = data.aws_iam_policy_document.github_assume[0].json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "github_deploy" {
  count = var.github_repository == "" ? 0 : 1

  name = "deploy-siteline-${var.env}"
  role = aws_iam_role.github_deploy[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "EcrLogin"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Sid    = "EcrPush"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:CompleteLayerUpload",
          "ecr:DescribeImages", "ecr:GetDownloadUrlForLayer", "ecr:InitiateLayerUpload",
          "ecr:PutImage", "ecr:UploadLayerPart",
        ]
        Resource = aws_ecr_repository.this.arn
      },
      {
        Sid      = "TaskDefinitions"
        Effect   = "Allow"
        Action   = ["ecs:DescribeTaskDefinition", "ecs:RegisterTaskDefinition", "ecs:ListTaskDefinitions"]
        Resource = "*"
      },
      {
        Sid      = "RunTasks"
        Effect   = "Allow"
        Action   = ["ecs:RunTask"]
        Resource = [for td in aws_ecs_task_definition.this : "${td.arn_without_revision}:*"]
        Condition = {
          ArnEquals = { "ecs:cluster" = aws_ecs_cluster.this.arn }
        }
      },
      {
        Sid    = "WatchTasksAndServices"
        Effect = "Allow"
        Action = ["ecs:DescribeTasks", "ecs:DescribeServices", "ecs:UpdateService", "ecs:TagResource"]
        Resource = [
          "arn:aws:ecs:${var.region}:${local.account_id}:task/${aws_ecs_cluster.this.name}/*",
          "arn:aws:ecs:${var.region}:${local.account_id}:service/${aws_ecs_cluster.this.name}/*",
          "arn:aws:ecs:${var.region}:${local.account_id}:task-definition/${var.name}-*",
        ]
      },
      {
        Sid      = "PassTaskRoles"
        Effect   = "Allow"
        Action   = ["iam:PassRole"]
        Resource = [aws_iam_role.execution.arn, aws_iam_role.task.arn]
        Condition = {
          StringLike = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" }
        }
      },
      {
        Sid      = "DeployParameters"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:PutParameter"]
        Resource = "arn:aws:ssm:${var.region}:${local.account_id}:parameter/siteline/${var.env}/*"
      },
      {
        Sid      = "ReadLogs"
        Effect   = "Allow"
        Action   = ["logs:GetLogEvents", "logs:FilterLogEvents", "logs:DescribeLogStreams"]
        Resource = "${aws_cloudwatch_log_group.app.arn}:*"
      },
    ]
  })
}
