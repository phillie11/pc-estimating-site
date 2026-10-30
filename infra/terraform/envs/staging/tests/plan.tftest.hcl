# Offline check that the whole configuration plans, with a fake AWS (no account or credentials needed):
#   terraform init -backend=false && terraform test
# You do not need to run this to deploy; it is for whoever changes the Terraform.

mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_availability_zones" {
    defaults = { names = ["eu-west-2a", "eu-west-2b", "eu-west-2c"] }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  # Resources whose ARNs are checked by other resources need ARN-shaped fakes
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/mock" }
  }
  mock_resource "aws_lb" {
    defaults = { arn = "arn:aws:elasticloadbalancing:eu-west-2:123456789012:loadbalancer/app/mock/0123456789abcdef", dns_name = "mock-123.eu-west-2.elb.amazonaws.com" }
  }
  mock_resource "aws_lb_target_group" {
    defaults = { arn = "arn:aws:elasticloadbalancing:eu-west-2:123456789012:targetgroup/mock/0123456789abcdef" }
  }
  mock_resource "aws_acm_certificate" {
    defaults = { arn = "arn:aws:acm:eu-west-2:123456789012:certificate/00000000-0000-0000-0000-000000000000" }
  }
  mock_resource "aws_ecs_cluster" {
    defaults = { arn = "arn:aws:ecs:eu-west-2:123456789012:cluster/mock" }
  }
  mock_resource "aws_ecs_task_definition" {
    defaults = {
      arn                  = "arn:aws:ecs:eu-west-2:123456789012:task-definition/mock:1"
      arn_without_revision = "arn:aws:ecs:eu-west-2:123456789012:task-definition/mock"
    }
  }
  mock_resource "aws_sesv2_email_identity" {
    defaults = { arn = "arn:aws:ses:eu-west-2:123456789012:identity/mock" }
  }
}

mock_provider "random" {}

variables {
  aws_account_id = "123456789012"
  alert_email    = "owner@pc-estimating.co.uk"
}

run "plans" {
  command = plan

  assert {
    condition     = output.dns_record_app.name == var.app_domain
    error_message = "The app DNS record should be for app_domain."
  }
}

# A full apply against the fake AWS: evaluates every expression with made-up IDs
run "applies" {
  command = apply

  assert {
    condition     = length(output.dns_records_certificate_and_email) >= 3
    error_message = "Expected the certificate and email DNS records."
  }
}
