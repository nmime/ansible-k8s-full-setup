# Tests

## Elasticsearch License Compliance Tests

Ensures the Elasticsearch role contains no X-Pack license crack/bypass artifacts.

### Running Tests

```bash
# Shell-based static analysis (15 checks)
bash tests/test_elasticsearch_license_compliance.sh

# Python unit tests (28 tests) — validate defaults and task content
python3 tests/test_elasticsearch_role_structure.py

# Python component tests (32 tests) — validate role structure, security config
python3 tests/test_elasticsearch_component_integration.py

# Python E2E tests (15 tests) — full pipeline validation + shell script execution
python3 tests/test_elasticsearch_e2e_yaml.py
```

### Test Coverage

| Suite | Type | Count | Scope |
|-------|------|-------|-------|
| `test_elasticsearch_license_compliance.sh` | Static | 15 | Shell-based artifact detection |
| `test_elasticsearch_role_structure.py` | Unit | 28 | Defaults, tasks, file existence |
| `test_elasticsearch_component_integration.py` | Component | 32 | Variable consistency, security config, network policies |
| `test_elasticsearch_e2e_yaml.py` | E2E | 15 | Full pipeline, shell script execution |
| **Total** | | **86** | |

### What the Tests Check

- No `platinum_license.json` or other forged license files
- No `es-crack-script` ConfigMap or `patch_xpack` init containers
- No `License.java`/`LicenseVerifier.java` compilation or JAR replacement
- No `es-platinum-license` Secret or license application Job
- `es_license_type` is set to `basic` in defaults
- `xpack.license.self_generated.type: basic` is set in container env vars
- No `crack` references anywhere in the role
- TLS, security, and non-root settings are properly configured
- All expected resources (StatefulSets, Services, PDBs) are defined
