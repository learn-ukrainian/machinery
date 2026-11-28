# Fleet Security Workflow (`.github/workflows/fleet-security.yml`)

Reusable, SHA-pinned security scanning workflow for public and private repositories across the fleet (`learn-ukrainian/machinery#1080`).

## Calling the workflow

Pin the workflow by full 40-character commit SHA (never `@main` or a mutable tag):

```yaml
jobs:
  fleet-security:
    uses: learn-ukrainian/machinery/.github/workflows/fleet-security.yml@<40-char-commit-SHA>
    permissions:
      contents: read
    with:
      run-actionlint: true
      run-shellcheck: true
      shellcheck-paths: |
        .github
        scripts|warning
      run-zizmor: true
      run-secret-scan: true
      run-trivy-fs: true
      trivy-fs-targets: |
        package-lock.json
        uv.lock
```

Every caller receives the exact same scanner binaries, GitHub Action SHAs and rule-bundle pins without any repository-name branching.

## Select a runner

The workflow uses `ubuntu-26.04` by default. Set `runs-on` to a JSON string or list of labels to use another Linux x64 runner. This example uses a repository variable for one label and defaults to `ubuntu-26.04` when the variable is unset.

```yaml
with:
  runs-on: ${{ toJSON(vars.SECURITY_RUNNER || 'ubuntu-26.04') }}
```

Use a Linux x64 runner with glibc, Bash, curl, jq, and standard GNU utilities, including coreutils, gzip, sed, and tar. Make Git available in `/usr/bin` or `/bin` and Python 3.10 or later at `/usr/bin/python3`. The workflow downloads and verifies its scanner binaries. On a runner without Docker, leave `run-trivy-image` disabled. Use self-hosted runners with private repositories. [GitHub runner guidance](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners)

## Security and private-repository guarantees

- **Self-contained:** requires no caller-local `.github/actions/`, no caller `uv.lock`, no secrets and no paid GitHub Advanced Security features (no `security-events: write`, no SARIF upload).
- **Least privilege:** top-level `permissions: {}`; every scanner job takes only `contents: read` and checks out with `persist-credentials: false`.
- **Process isolation:** every scanner runs under `env -i` with a dedicated temporary `HOME` and `TMPDIR` so no runner token or environment variable reaches any scanner.
- **Verified binaries:** `actionlint`, `shellcheck`, `zizmor`, `trufflehog` and `trivy` are downloaded from their public GitHub release URLs over HTTPS without sending `github.token`, verified against pinned SHA-256 checksums before every extraction (including cache hits), and version-checked before running. Fork PRs restore caches read-only.
- **No implicit Trivy config:** every `trivy` invocation passes `--config ""` so a repository-local `trivy.yaml` cannot silently suppress findings or override flags; suppressions go only through explicit workflow inputs or a reviewed `.trivyignore`.
- **Redacted evidence:** raw scanner stderr is redirected to a temp file that is never printed (only its line count is logged). Logs, property-escaped annotations and `fleet-security-receipt.v1` receipts print only counts, repo-relative paths, line/column numbers, rule/detector/CVE/AVD/licence IDs and severities—never secret values or source snippets.
- **Fail closed:** enabling zero checks, omitting required target inputs, naming a missing or invalid path (absolute, `..`, symlink escaping the workspace, control characters), matching zero files/packages/configs, passing a floating image tag without `@sha256:...`, or receiving malformed scanner JSON all fail the job and `Fleet security gate`.

## Inputs

| Input | Type | Default | Meaning |
| --- | --- | --- | --- |
| `runs-on` | string | `'"ubuntu-26.04"'` | JSON runner label or label array for every job. |
| `run-actionlint` | boolean | `false` | Lint workflows under `workflow-dir` with pinned `actionlint` + `shellcheck`. |
| `workflow-dir` | string | `.github/workflows` | Repo-relative workflow directory for `actionlint`. |
| `run-shellcheck` | boolean | `false` | Run pinned `shellcheck` over `shellcheck-paths`. |
| `shellcheck-paths` | string | `''` | Newline-separated repo-relative files or dirs (`path` or `path\|severity`). Required when `run-shellcheck: true`. |
| `shellcheck-severity` | string | `style` | Default minimum severity (`error`, `warning`, `info`, `style`). |
| `shellcheck-exclude-paths` | string | `''` | Optional newline-separated repo-relative paths excluded from `shellcheck`. |
| `run-zizmor` | boolean | `false` | Run pinned `zizmor` offline over workflow and action YAML files. |
| `zizmor-paths` | string | `''` | Optional newline-separated repo-relative paths. Empty defaults to `.github/workflows` (required) + `.github/actions` (if present). Explicit paths fail closed if missing or empty. |
| `zizmor-config` | string | `''` | Optional repo-relative zizmor config file path (must exist if set). |
| `run-secret-scan` | boolean | `false` | Run pinned `trufflehog` over the event commit range (`--no-verification --results=verified,unknown,unverified`; no candidate is sent to a provider). |
| `secret-scan-base` | string | `''` | Optional explicit 40-hex base commit SHA for `push` events. |
| `secret-scan-require-base` | boolean | `false` | On `push`, require `secret-scan-base` instead of falling back to `github.event.before`. |
| `trufflehog-exclude-paths` | string | `.trufflehogignore` | Repo-relative TruffleHog exclusion file. |
| `require-trufflehog-ignore` | boolean | `false` | Fail closed when `.trufflehogignore` is missing. Explicit non-default paths always fail closed if missing. |
| `secret-scan-required-extensions` | string | `''` | Comma-separated extensions (e.g. `.py`) that must remain in scope after exclusions. |
| `run-trivy-fs` | boolean | `false` | Run pinned `trivy fs` for dependency CVEs and licences. |
| `trivy-fs-targets` | string | `''` | Newline-separated repo-relative files or directories. Required when `run-trivy-fs: true` unless pip lock files are supplied. |
| `trivy-fs-pip-lock-files` | string | `''` | Optional newline-separated repo-relative pip lock files with exact version pins. Each file gets its own target receipt. |
| `trivy-scanners` | string | `vuln,license` | Comma-separated subset of `vuln,license,misconfig`. |
| `trivy-severity` | string | `HIGH,CRITICAL` | Comma-separated severities (`UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL`). |
| `trivy-ignore-unfixed` | boolean | `false` | Ignore unfixed vulnerabilities in `trivy fs`. |
| `trivy-include-dev-deps` | boolean | `true` | Include dev dependencies in `trivy fs` (`npm`, `yarn`, `gradle`). |
| `trivy-ignorefile` | string | `''` | Optional repo-relative `.trivyignore` file (empty uses root `.trivyignore` if present, else disables loading). |
| `trivy-ignored-licenses` | string | `''` | Optional comma-separated SPDX licence IDs to ignore. |
| `trivy-fs-license-dispositions` | string | `''` | Optional exact licence dispositions, one `target\|PkgName\|Name` tuple per line. Applies to `trivy fs`. |
| `run-trivy-iac` | boolean | `false` | Run pinned `trivy config` with the SHA-256-pinned `trivy-checks` bundle. |
| `trivy-iac-targets` | string | `''` | Newline-separated repo-relative IaC files or directories. Required when `run-trivy-iac: true`. |
| `trivy-iac-severity` | string | `HIGH,CRITICAL` | Comma-separated severities for `trivy config`. |
| `trivy-iac-scanners` | string | all supported | Comma-separated misconfig scanners (`dockerfile`, `terraform`, `kubernetes`, `ansible`, `helm`, `cloudformation`, `azure-arm`, `terraformplan-json`, `terraformplan-snapshot`). |
| `trivy-iac-skip-dirs` | string | `''` | Optional newline-separated repo-relative directories to skip (reviewed training fixtures). |
| `trivy-iac-skip-files` | string | `''` | Optional newline-separated repo-relative files to skip. |
| `run-trivy-image` | boolean | `false` | Run pinned `trivy image` on built or digest-pinned images. |
| `trivy-image-targets` | string | `''` | Newline-separated specs: `build\|<dockerfile>\|<context>[\|<stage>]` or `image\|<ref@sha256:64-hex>`. Floating tags are refused. |
| `trivy-image-severity` | string | `HIGH,CRITICAL` | Comma-separated severities for `trivy image`. |
| `trivy-image-scanners` | string | `vuln` | Comma-separated subset of `vuln,license` (defaults to `vuln` so Linux base-OS GPL utilities do not false-fail image scans). |
| `trivy-image-ignore-unfixed` | boolean | `false` | Ignore unfixed vulnerabilities in `trivy image`. |

## Scan pip lock files

When your resolved Python dependencies live in a custom-named pip lock file, set
`trivy-fs-pip-lock-files`. You can also scan ordinary targets in the same job.

```yaml
with:
  run-trivy-fs: true
  trivy-fs-targets: |
    package-lock.json
  trivy-fs-pip-lock-files: |
    requirements-machinery.lock
    tools/requirements-dev.lock
```

List one repo-relative regular file per line. The workflow rejects missing files,
directories, symlinks, and paths that traverse outside the checkout. Trivy uses
its pip parser for each listed file and reports the package count and findings
in that file's receipt. A file with zero detected pip packages fails the scan.
A `requirements.txt` with only lower bounds such as `package>=1.2` has no
resolved packages to scan. Supply the lock file with exact pins such as
`package==1.2`.

## Trivy filesystem licence dispositions

Use a reviewed tuple to accept a licence finding for one scan target and package. Copy the target path from `trivy-fs-targets` or `trivy-fs-pip-lock-files`. Copy the package name and full SPDX expression from the finding's `PkgName` and `Name` fields. For example, `package-lock.json|example-package|GPL-3.0-only`.

All three fields match literally, including case and spaces within an expression. Blank lines are allowed. Each tuple must have three nonempty fields, with no surrounding whitespace, control characters, or extra separators. Use a repo-relative target path or `.`. Repeated tuples count as one disposition. A malformed tuple fails the step before Trivy runs.

The target receipt lists each accepted finding's tuple in `accepted_license_findings` and includes those findings in the `licenses` total. Findings without a match still fail the step. The scan warns about unused tuples and lists them in `stale_license_dispositions` in the job receipt.

## Scanner policy on pull requests and merge groups

On `pull_request` and `merge_group`, scanner config and ignore files come from the event's base commit. This covers Actionlint and Zizmor config files, ShellCheck configuration in the source directory hierarchy, and the Trivy and TruffleHog ignore files. Explicit repo-relative config and ignore paths also resolve at the base commit.

Each enabled scanner job records the base commit in `scanner-policy.base_sha` and lists changed policy paths in `scanner-policy.changed`. If the candidate adds a policy file, the scan reports the addition and scans without that file's settings. If the candidate edits or removes an existing policy file, the scan uses the base version. For ShellCheck, pre-existing base directories named `.shellcheckrc` or `shellcheckrc` are skipped during policy export; adding or converting a policy path to a symlink, directory, or other nonregular Git entry in the candidate still fails the job. Other events use the checked-out policy.

## Output receipt (`fleet-security-receipt.v1`)

`Fleet security gate` runs `if: always()`, verifies every enabled job succeeded and every disabled job was skipped, writes a compact redacted JSON receipt to `$GITHUB_STEP_SUMMARY`, and exposes it as workflow output `receipt`:

```json
{
  "schema": "fleet-security-receipt.v1",
  "status": "pass",
  "pins": {
    "actionlint": "1.7.12",
    "shellcheck": "0.11.0",
    "zizmor": "1.31.1",
    "trufflehog": "3.99.0",
    "trivy": "0.76.2",
    "trivy_checks_digest": "sha256:b591e9190cff4aa4e385b1a25bccfd9ecf178d3c77fd194e1410b7ce7e1df058"
  },
  "jobs": { "...": { "enabled": true, "result": "success", "receipt": {} } }
}
```

## Example: private infrastructure repository

```yaml
jobs:
  fleet-security:
    uses: learn-ukrainian/machinery/.github/workflows/fleet-security.yml@<40-char-commit-SHA>
    permissions:
      contents: read
    with:
      run-actionlint: true
      run-zizmor: true
      run-shellcheck: true
      shellcheck-paths: |
        scripts
      run-secret-scan: true
      run-trivy-iac: true
      trivy-iac-targets: |
        terraform
        ansible
```

## Example: public repository with built container image

```yaml
jobs:
  fleet-security:
    uses: learn-ukrainian/machinery/.github/workflows/fleet-security.yml@<40-char-commit-SHA>
    permissions:
      contents: read
    with:
      run-actionlint: true
      run-zizmor: true
      run-shellcheck: true
      shellcheck-paths: |
        scripts
      run-secret-scan: true
      run-trivy-fs: true
      trivy-fs-targets: |
        package-lock.json
        site/package-lock.json
        uv.lock
      run-trivy-iac: true
      trivy-iac-targets: |
        docker/pocketbase/Dockerfile
      run-trivy-image: true
      trivy-image-targets: |
        build|docker/pocketbase/Dockerfile|docker/pocketbase
```

## Updating pins

All action SHAs, tool versions, release asset IDs, archive SHA-256 digests and Trivy checks bundle digests are recorded with upstream evidence in `.github/workflows/pins.txt`. Bump `.github/workflows/pins.txt`, `.github/workflows/fleet-security.yml` and `.github/workflows/ci-gate.yml` (and `.github/workflows/tests.yml` where applicable) together in one PR, then update callers to the merged commit SHA.
