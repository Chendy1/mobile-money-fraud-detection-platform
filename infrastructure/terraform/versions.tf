# Phase 22: plan-only Terraform for the design in docs/aws-design.md.
# Nothing here has been applied. There is no backend block: state stays local (and git-ignored).
# No credentials appear in any file: the AWS provider reads them from your environment or profile.

terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project = var.project
      Phase   = "22"
      Owner   = var.owner
      Expires = var.expires
    }
  }
}

data "aws_caller_identity" "current" {}
