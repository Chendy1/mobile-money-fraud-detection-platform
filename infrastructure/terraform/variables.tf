variable "project" {
  description = "Prefix for every resource name."
  type        = string
  default     = "mmrisk"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,20}$", var.project))
    error_message = "project must be 3 to 21 lowercase letters, digits or hyphens, starting with a letter."
  }
}

variable "owner" {
  description = "Who to ask about these resources (a name, not an email address or an account id)."
  type        = string
}

variable "expires" {
  description = "Date (YYYY-MM-DD) after which the resources should be deleted. Shown as a tag."
  type        = string

  validation {
    condition     = can(regex("^\\d{4}-\\d{2}-\\d{2}$", var.expires))
    error_message = "expires must look like 2026-12-31."
  }
}

variable "region" {
  description = "AWS region (ASSUMED us-east-1; see docs/aws-design.md section 11)."
  type        = string
  default     = "us-east-1"
}

variable "availability_zone" {
  description = "One zone only: the design is not highly available (a stated non-goal)."
  type        = string
  default     = "us-east-1a"
}

variable "vpc_cidr" {
  type    = string
  default = "10.20.0.0/24"
}

variable "subnet_cidr" {
  type    = string
  default = "10.20.0.0/26"
}

variable "desired_count" {
  description = "Tasks per service. 0 is the resting state: nothing runs and no compute is billed."
  type        = number
  default     = 0

  validation {
    condition     = var.desired_count >= 0 && var.desired_count <= 1
    error_message = "desired_count is 0 or 1: the API keeps state in memory, so a second copy would disagree with the first."
  }
}

variable "api_cpu" {
  description = "Fargate CPU units for the API (ASSUMED 1024; valid sizes TO VERIFY)."
  type        = number
  default     = 1024

  validation {
    condition     = contains([256, 512, 1024, 2048, 4096], var.api_cpu)
    error_message = "api_cpu must be 256, 512, 1024, 2048 or 4096."
  }
}

variable "api_memory" {
  description = "MiB of memory for the API (ASSUMED 2048)."
  type        = number
  default     = 2048
}

variable "dashboard_cpu" {
  type    = number
  default = 512

  validation {
    condition     = contains([256, 512, 1024, 2048, 4096], var.dashboard_cpu)
    error_message = "dashboard_cpu must be 256, 512, 1024, 2048 or 4096."
  }
}

variable "dashboard_memory" {
  type    = number
  default = 1024
}

variable "api_image_tag" {
  description = "Tag of the API image in ECR. Tags are immutable: use a version, never latest."
  type        = string
  default     = "0.1.0"

  validation {
    condition     = var.api_image_tag != "latest"
    error_message = "Do not use latest: tags are immutable and a moving tag hides what runs."
  }
}

variable "dashboard_image_tag" {
  type    = string
  default = "0.1.0"

  validation {
    condition     = var.dashboard_image_tag != "latest"
    error_message = "Do not use latest."
  }
}

variable "log_retention_days" {
  description = "ASSUMED 14 days."
  type        = number
  default     = 14
}

variable "secret_recovery_days" {
  description = "Days a deleted secret can be recovered (7 to 30)."
  type        = number
  default     = 7

  validation {
    condition     = var.secret_recovery_days >= 7 && var.secret_recovery_days <= 30
    error_message = "secret_recovery_days must be between 7 and 30."
  }
}

variable "enable_execute_command" {
  description = "Lets you reach a task with ECS Exec and an SSM port forward (the only way in)."
  type        = bool
  default     = true
}

variable "monthly_budget_usd" {
  description = "Your monthly limit. A budget alerts; it does not stop spending."
  type        = number

  validation {
    condition     = var.monthly_budget_usd > 0
    error_message = "monthly_budget_usd must be above zero."
  }
}

variable "budget_alert_email" {
  description = "Where budget alerts go. Keep it out of git: use terraform.tfvars (ignored)."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+$", var.budget_alert_email))
    error_message = "budget_alert_email must look like an email address."
  }
}
