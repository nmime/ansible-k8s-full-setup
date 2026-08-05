from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_ci_classification_uses_gitlab_metadata_not_named_environment_branches():
    path = ROOT / "templates/gitlab-ci-environment-classification.yml"
    text = path.read_text()
    document = yaml.safe_load(text)
    rules = document[".n0xeid-ci-auto"]["rules"]

    rule_text = "\n".join(rule.get("if", "") for rule in rules)
    assert "preproduction" not in rule_text.lower()
    assert any("CI_DEFAULT_BRANCH" in rule.get("if", "") for rule in rules)
    assert any("CI_COMMIT_REF_PROTECTED" in rule.get("if", "") for rule in rules)
    assert any("CI_PIPELINE_SOURCE" in rule.get("if", "") for rule in rules)
    assert document[".n0xeid-ci-auto"]["tags"] == ["ci-shared"]


def test_every_ci_class_maps_to_a_bounded_kubernetes_priority():
    document = yaml.safe_load(
        (ROOT / "templates/gitlab-ci-environment-classification.yml").read_text()
    )
    rendered = "\n".join(
        rule.get("variables", {}).get(
            "KUBERNETES_PRIORITY_CLASS_NAME_OVERWRITE", ""
        )
        for rule in document[".n0xeid-ci-auto"]["rules"]
    )
    for name in ("production", "environment", "review", "maintenance"):
        assert f"n0xeid-ci-{name}" in rendered


def test_production_deploy_guard_is_protected_and_canonical():
    text = (ROOT / "templates/gitlab-ci-environment-classification.yml").read_text()
    assert "CI_COMMIT_REF_PROTECTED" in text
    assert "CI_ENVIRONMENT_TIER" in text
    assert 'test "$N0XEID_CI_CLASS" = production' in text
    assert "environment.deployment_tier=production" in text
