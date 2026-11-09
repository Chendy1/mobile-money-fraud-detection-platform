"""Phase 22: structural and policy checks on the plan-only Terraform.

Neither `terraform validate` nor `plan` can run here (no provider registry, no AWS account), so
these tests parse the HCL and assert the design's rules: no public path, least-privilege IAM,
private encrypted buckets, nothing stored in state that should not be, a resting state of zero.
`fmt -check` runs when a terraform or tofu binary is on PATH.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

hcl2 = pytest.importorskip("hcl2")

TF = Path(__file__).resolve().parents[2] / "infrastructure" / "terraform"
FILES = sorted(TF.glob("*.tf"))
SRC = {p.name: p.read_text(encoding="utf-8") for p in FILES}
ALL = "\n".join(SRC.values())


def unq(s):
    return s.strip('"') if isinstance(s, str) else s


def parse():
    kinds = {"resource": {}, "data": {}, "variable": {}, "output": {}, "provider": {}, "locals": {}}
    for p in FILES:
        tree = hcl2.loads(p.read_text(encoding="utf-8"))
        for kind in ("resource", "data"):
            for block in tree.get(kind, []):
                for rtype, named in block.items():
                    for name, body in named.items():
                        kinds[kind][(unq(rtype), unq(name))] = body
        for block in tree.get("variable", []):
            for name, body in block.items():
                kinds["variable"][unq(name)] = body
        for block in tree.get("output", []):
            for name, body in block.items():
                kinds["output"][unq(name)] = body
        for block in tree.get("locals", []):
            kinds["locals"].update(block)
        for block in tree.get("provider", []):
            for name, body in block.items():
                kinds["provider"][unq(name)] = body
    return kinds


T = parse()
RES = T["resource"]


def code_only(text):
    keep = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#") or s.startswith(("description", "error_message")):
            continue
        keep.append(line)
    return "\n".join(keep)


CODE = code_only(ALL)


def of_type(rtype):
    return {n: b for (t, n), b in RES.items() if t == rtype}


def statements(node, out=None):
    out = [] if out is None else out
    if isinstance(node, dict):
        if "actions" in node and "resources" in node:
            out.append(node)
        for v in node.values():
            statements(v, out)
    elif isinstance(node, list):
        for v in node:
            statements(v, out)
    return out


def test_every_file_parses_and_the_expected_files_exist():
    assert {p.name for p in FILES} >= {
        "versions.tf", "variables.tf", "network.tf", "s3.tf", "ecr.tf", "secrets.tf",
        "logs.tf", "iam.tf", "ecs.tf", "budgets.tf", "outputs.tf",
    }  # fmt: skip
    assert RES


@pytest.mark.skipif(not (shutil.which("terraform") or shutil.which("tofu")), reason="no binary")
def test_the_files_are_formatted():
    binary = shutil.which("terraform") or shutil.which("tofu")
    done = subprocess.run(
        [binary, "fmt", "-check", "-diff", str(TF)], capture_output=True, text=True
    )
    assert done.returncode == 0, done.stdout[-800:]


def test_every_reference_resolves_and_every_variable_is_used():
    declared_vars = set(T["variable"])
    used_vars = set(re.findall(r"\bvar\.(\w+)", CODE))
    assert used_vars <= declared_vars, used_vars - declared_vars
    assert declared_vars <= used_vars, f"unused variables: {declared_vars - used_vars}"
    names = set(RES)
    for rtype, name in re.findall(r"(?<![\w.])(aws_\w+)\.(\w+)", CODE):
        assert (rtype, name) in names, f"{rtype}.{name} is not defined"
    for rtype, name in re.findall(r"\bdata\.(aws_\w+)\.(\w+)", CODE):
        assert (rtype, name) in T["data"], f"data.{rtype}.{name} is not defined"
    local_names = set(T["locals"])
    for name in re.findall(r"\blocal\.(\w+)", CODE):
        assert name in local_names, f"local.{name} is not defined"


def test_the_inputs_that_belong_to_the_owner_have_no_default():
    for name in ("owner", "expires", "monthly_budget_usd", "budget_alert_email"):
        assert "default" not in T["variable"][name], name


def test_nothing_runs_by_default_and_a_second_copy_is_refused():
    v = T["variable"]["desired_count"]
    assert v["default"] == 0
    assert "<= 1" in str(v["validation"])
    for svc in ("api", "dashboard"):
        assert of_type("aws_ecs_service")[svc]["desired_count"] == "${var.desired_count}"


def test_images_cannot_use_the_latest_tag_and_repositories_are_immutable_and_scanned():
    for name in ("api_image_tag", "dashboard_image_tag"):
        assert '!= "latest"' in str(T["variable"][name]["validation"])
    for repo in of_type("aws_ecr_repository").values():
        assert unq(repo["image_tag_mutability"]) == "IMMUTABLE"
        assert repo["image_scanning_configuration"][0]["scan_on_push"] is True
        assert repo.get("force_delete") is False


def test_no_path_from_the_internet_exists():
    forbidden = {
        "aws_internet_gateway", "aws_nat_gateway", "aws_eip", "aws_lb", "aws_alb",
        "aws_lb_listener", "aws_api_gateway_rest_api", "aws_apigatewayv2_api",
        "aws_cloudfront_distribution", "aws_db_instance", "aws_rds_cluster",
        "aws_instance", "aws_vpc_peering_connection",
    }  # fmt: skip
    assert forbidden.isdisjoint({t for t, _ in RES})
    assert "0.0.0.0/0" not in ALL and "::/0" not in ALL
    assert not re.search(r"cidr_(blocks|ipv4|ipv6)", CODE.replace("cidr_block ", "cidr_block"))
    assert "map_public_ip_on_launch" in CODE and "map_public_ip_on_launch = true" not in CODE
    assert "assign_public_ip = true" not in CODE
    assert CODE.count("assign_public_ip = false") == 2
    assert not re.search(r"^\s*ingress\s*\{", CODE, re.M)


def test_only_the_endpoints_accept_inbound_traffic_and_only_from_the_tasks():
    ingress = of_type("aws_vpc_security_group_ingress_rule")
    assert set(ingress) == {"endpoints_from_tasks"}
    rule = ingress["endpoints_from_tasks"]
    assert rule["security_group_id"] == "${aws_security_group.endpoints.id}"
    assert rule["referenced_security_group_id"] == "${aws_security_group.tasks.id}"
    egress = of_type("aws_vpc_security_group_egress_rule")
    assert set(egress) == {"tasks_to_endpoints", "tasks_to_s3"}
    for r in egress.values():
        assert r["from_port"] == 443 and r["to_port"] == 443


def test_every_bucket_is_private_encrypted_tls_only_and_never_force_destroyed():
    buckets = of_type("aws_s3_bucket")["this"]
    assert buckets["force_destroy"] is False
    pab = of_type("aws_s3_bucket_public_access_block")["this"]
    for flag in ("block_public_acls", "block_public_policy", "ignore_public_acls",
                 "restrict_public_buckets"):  # fmt: skip
        assert pab[flag] is True, flag
    enc = of_type("aws_s3_bucket_server_side_encryption_configuration")["this"]
    assert "AES256" in str(enc)
    assert "BucketOwnerEnforced" in str(of_type("aws_s3_bucket_ownership_controls")["this"])
    doc = T["data"][("aws_iam_policy_document", "tls_only")]
    st = statements(doc)[0]
    assert unq(st["effect"]) == "Deny" and "aws:SecureTransport" in str(doc)
    assert "false" in str(doc)
    assert set(T["locals"]["buckets"].keys()) == {"lake", "registry", "backups"}
    assert "registry = { versioned = true }" in ALL and "backups  = { versioned = true }" in ALL


def test_iam_has_no_wildcard_action_and_the_only_wildcard_resources_are_the_ones_aws_requires():
    allowed_star = {
        "ecr:GetAuthorizationToken", "ssmmessages:CreateControlChannel",
        "ssmmessages:CreateDataChannel", "ssmmessages:OpenControlChannel",
        "ssmmessages:OpenDataChannel",
    }  # fmt: skip
    seen_star = set()
    for (t, n), doc in T["data"].items():
        if t != "aws_iam_policy_document":
            continue
        for st in statements(doc):
            effect = unq(st.get("effect", '"Allow"'))
            actions = [unq(a) for a in st["actions"]]
            raw = st["resources"]
            resources = [unq(r) for r in (raw if isinstance(raw, list) else [raw])]
            # a computed list ("${[for ...]}") is one expression, not the wildcard
            if effect == "Allow":
                assert not any(a == "*" or a.endswith(":*") for a in actions), (n, actions)
                if "*" in resources:
                    assert set(actions) <= allowed_star, (n, actions)
                    seen_star |= set(actions)
    assert seen_star == allowed_star  # every exception is real, none is unused


def test_no_users_no_access_keys_no_secret_values_no_inline_credentials():
    kinds = {t for t, _ in RES}
    for bad in ("aws_iam_user", "aws_iam_access_key", "aws_iam_user_login_profile",
                "aws_secretsmanager_secret_version", "aws_iam_openid_connect_provider",
                "aws_ssm_parameter"):  # fmt: skip
        assert bad not in kinds, bad
    assert not re.search(r"AKIA[0-9A-Z]{16}", ALL)
    assert not re.search(r"\b\d{12}\b", ALL)
    assert not re.search(r"access_key|secret_key|\bprofile\s*=|assume_role\s*\{", CODE)
    assert "backend " not in CODE  # state stays local and ignored; no bucket named here


def test_the_secrets_are_empty_containers_with_a_recovery_window():
    secrets = of_type("aws_secretsmanager_secret")["this"]
    assert secrets["recovery_window_in_days"] == "${var.secret_recovery_days}"
    for name in ("api_keys", "metrics_key", "tls_cert", "tls_key", "dashboard_hash"):
        assert f'"{name}"' in SRC["secrets.tf"]


def test_tasks_are_non_root_read_only_without_capabilities_and_log_to_cloudwatch():
    ecs = SRC["ecs.tf"]
    assert ecs.count('user                   = "10001"') == 2
    assert ecs.count("readonlyRootFilesystem = true") == 2
    assert ecs.count('capabilities       = { drop = ["ALL"] }') == 2
    assert ecs.count('logDriver = "awslogs"') == 2
    assert ecs.count('entryPoint             = ["/app/aws-entrypoint.sh"]') == 2
    assert "privileged" not in ecs and "hostPort" not in ecs
    assert 'cpu_architecture        = "X86_64"' in ecs
    assert "--allow-insecure-external" not in ecs
    assert "--auth-keys" in ecs and "--tls-cert" in ecs and "--tls-key" in ecs


def test_the_known_gap_is_written_where_the_next_reader_will_see_it():
    head = SRC["ecs.tf"][:1200]
    assert "KNOWN GAP" in head and "aws-entrypoint.sh" in head and "desired_count" in head


def test_the_execution_role_reads_only_the_named_secrets_and_the_task_role_only_reads_s3():
    docs = T["data"]
    ex = str(docs[("aws_iam_policy_document", "execution")])
    assert "secretsmanager:GetSecretValue" in ex and "aws_secretsmanager_secret.this" in ex
    task = str(docs[("aws_iam_policy_document", "task")])
    assert "s3:GetObject" in task and "s3:PutObject" not in task and "s3:Delete" not in task
    assert "secretsmanager" not in task
    assert 'aws_s3_bucket.this["backups"]' not in task  # the API never touches backups


def test_log_groups_have_retention_and_the_budget_alerts_actual_and_forecast():
    group = of_type("aws_cloudwatch_log_group")["this"]
    assert group["retention_in_days"] == "${var.log_retention_days}"
    assert T["variable"]["log_retention_days"]["default"] > 0
    budget = SRC["budgets.tf"]
    assert 'notification_type          = "ACTUAL"' in budget
    assert 'notification_type          = "FORECASTED"' in budget
    assert "[50, 80, 100]" in budget and 'limit_unit   = "USD"' in budget
    assert "does not stop anything" in budget


def test_the_provider_tags_everything_and_carries_no_credentials():
    tags = str(T["provider"]["aws"])
    for key in ("Project", "Phase", "Owner", "Expires"):
        assert key in tags
    assert "region" in tags and "access" not in tags and "token" not in tags


def test_state_plans_and_real_variable_files_are_ignored_and_absent():
    ignore = (TF.parents[1] / ".gitignore").read_text()
    for pattern in (".terraform/", "*.tfstate", "*.tfstate.*", "*.tfplan", "*.tfvars",
                    "!*.tfvars.example"):  # fmt: skip
        assert pattern in ignore, pattern
    for bad in ("terraform.tfstate", "terraform.tfvars", ".terraform"):
        assert not (TF / bad).exists(), bad
    example = (TF / "terraform.tfvars.example").read_text()
    assert "example.com" in example and "your-name" in example


def test_the_example_variables_validate_against_the_declared_rules():
    for name, good, bad in (
        ("project", "mmrisk", "Bad_Name"),
        ("expires", "2026-12-31", "tomorrow"),
        ("budget_alert_email", "a@b.co", "not-an-email"),
    ):
        pattern = re.search(r'regex\("(.+?)", var\.', str(T["variable"][name]["validation"]))
        rx = pattern.group(1)
        while "\\\\" in rx:
            rx = rx.replace("\\\\", "\\")
        assert re.search(rx, good), name
        assert not re.search(rx, bad), name
