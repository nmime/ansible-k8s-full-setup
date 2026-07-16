"""Unit tests: component upgrades cannot masquerade as profile migrations."""
import subprocess
import os

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _eval(script_path, current_tier, target_tier):
    """Extract and run get_canary_sequence in isolation."""
    # Read the script and extract just the pieces we need
    with open(script_path) as f:
        content = f.read()

    # Extract get_canary_sequence function
    fn_start = content.find("get_canary_sequence()")
    if fn_start < 0:
        return ""

    # Find function body (next function or end of meaningful section)
    fn_end = content.find("\n\n", fn_start)
    if fn_end < 0:
        fn_end = len(content)
    else:
        # Find the closing brace
        brace_count = 0
        for i, ch in enumerate(content[fn_start:]):
            if ch == '{':
                brace_count += 1
            elif ch == '}':
                brace_count -= 1
                if brace_count == 0:
                    fn_end = fn_start + i + 1
                    break

    fn_body = content[fn_start:fn_end]

    # Build and run a minimal script
    minimal_script = f"""
CURRENT_TIER="{current_tier}"
error() {{ printf '%s\n' "$*" >&2; }}
{fn_body}
get_canary_sequence "{target_tier}"
"""
    r = subprocess.run(
        ["bash", "-c", minimal_script],
        capture_output=True, text=True, timeout=10
    )
    return r.stdout.strip()


class TestCanarySequence:
    def test_profile_migration_has_a_dedicated_entrypoint(self):
        with open(os.path.join(REPO, "scripts", "upgrade-platform.sh")) as f:
            content = f.read()
        assert "migrate-profile.sh plan|execute" in content

    def test_minimal(self):
        seq = _eval(os.path.join(REPO, "scripts", "upgrade-platform.sh"), "minimal", "minimal")
        assert seq == "minimal", f"Got: {repr(seq)}"

    def test_small(self):
        seq = _eval(os.path.join(REPO, "scripts", "upgrade-platform.sh"), "minimal", "small")
        assert seq == "", f"Got: {repr(seq)}"

    def test_medium(self):
        seq = _eval(os.path.join(REPO, "scripts", "upgrade-platform.sh"), "small", "medium")
        assert seq == "", f"Got: {repr(seq)}"

    def test_production(self):
        seq = _eval(os.path.join(REPO, "scripts", "upgrade-platform.sh"), "small", "production")
        assert seq == "", f"Got: {repr(seq)}"


class TestFlags:
    def test_dry_run_in_upgrade(self):
        with open(os.path.join(REPO, "scripts", "upgrade-platform.sh")) as f:
            c = f.read()
        assert "DRY_RUN=false" in c
        assert "$DRY_RUN" in c

    def test_dry_run_in_rollback(self):
        with open(os.path.join(REPO, "scripts", "rollback.sh")) as f:
            assert "DRY_RUN=false" in f.read()

    def test_force_in_upgrade(self):
        with open(os.path.join(REPO, "scripts", "upgrade-platform.sh")) as f:
            c = f.read()
        assert "FORCE=false" in c

    def test_upgrade_health_gates_follow_profile_selection(self):
        with open(os.path.join(REPO, "scripts", "upgrade-platform.sh")) as f:
            c = f.read()
        assert "configure_health_requirements" in c
        assert "HEALTH_REQUIRE_MONGODB" in c
        assert ".databases.mongodb.enabled" in c

    def test_force_in_rollback(self):
        with open(os.path.join(REPO, "scripts", "rollback.sh")) as f:
            assert "FORCE=false" in f.read()
