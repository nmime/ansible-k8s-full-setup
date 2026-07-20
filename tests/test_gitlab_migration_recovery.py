from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_failed_gitlab_migration_job_is_removed_before_helm_reconcile():
    tasks = (ROOT / "roles/gitlab-selfhosted/tasks/main.yml").read_text()

    discover = tasks.index("Discover failed GitLab database migration Jobs")
    remove = tasks.index(
        "Remove failed GitLab database migration Jobs before Helm reconcile"
    )
    install = tasks.index("Install GitLab with Helm")
    assert discover < remove < install

    block = tasks[discover:install]
    assert "app=migrations" in block
    assert "release=gitlab" in block
    assert "selectattr('status.failed', 'gt', 0)" in block
    assert "state: absent" in block
    assert "wait: true" in block
