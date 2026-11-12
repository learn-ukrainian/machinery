# Releasing lu-machinery

Consumers install `lu-machinery` from a released wheel, pinned by version and
SHA-256 in their own `requirements-machinery.lock`. There is no editable
install and no machinery checkout on `PYTHONPATH`. Releases are manual, cut
when a consumer needs a change; there is no release schedule or automation.

## Identity

A release is identified by its exact version, the machinery `main` commit it
was built from, and the SHA-256 of its wheel. The annotated tag `v<version>`
points at that commit. Each version is built once. A defect in a published
release is fixed by a new version, never by rebuilding or replacing assets.

## What a release holds

- One wheel and one sdist, built once from the tagged commit.
- `SHA256SUMS` listing both (`sha256sum` format).
- The GitHub release `v<version>` with those three files as assets.
- The host wheelhouse (its location is host configuration in infra-private)
  holds the same wheel bytes plus every wheel named in this commit's
  `requirements-machinery.lock`. It keeps the previous release's wheel and
  dependencies, so a consumer can roll back offline.
- The machinery-owned `package.json`, `package-lock.json` and `.nvmrc` ship
  with the wheel at `share/lu-machinery/node-contract` under its environment.
  Retain the release's npm cache and provisioned Node prefix alongside its
  wheelhouse, with the same current/previous-release retention policy. The
  prefix includes lock-verified package archives for offline integrity checks.

## Dependency lock

`requirements-machinery.lock` pins every transitive runtime dependency with
the hash of the one wheel installed on the server and on GitHub-hosted
`ubuntu-24.04` (CPython 3.12, x86_64). Its header records the resolution
command. Resolve against learn-ukrainian's `requirements-lock.txt` as
constraints, so the machinery lock agrees with the main consumer's pins. After
resolving, take each hash from the wheel `pip download --only-binary=:all:`
selects for both targets. Use `--platform` for the hosted runner's glibc
ladder. If the targets select different wheels, list both hashes. The
`Installed wheel` job of CI Gate installs the lock with `--require-hashes`.
It fails when a dependency in `pyproject.toml` has no pin.

## Cut a release

From a machinery `main` commit whose CI Gate is green, with `$V` the version
in `pyproject.toml` and `$SHA` the full commit:

1. Check for outstanding second-review debt (label `carbon-debt`). This command must succeed and print nothing:

   ```bash
   gh pr list -R learn-ukrainian/machinery --state merged --label carbon-debt
   ```

   Any result blocks the release. The publisher adds `carbon-debt` before admitting
   a machinery PR without an exact-head second review. Only a model of the catalog's
   ordered `review_scheduler.second_reviewer` list (Muse, Carbon, then Sol and Opus as last
   resort) on its listed route counts, eligible by the list's family rules
   ([review policy](../agents_extensions/shared/skills/review-loop/references/policy.md)).
   Record a completed second review with `record_cf_verdict --second-review --pr <N>`
   for the PR's merged head, meaning the reviewed PR head at merge, not the squash commit.
   A timeout or incomplete review does not clear the debt. Reproduce findings and fix every confirmed defect in a follow-up
   PR before release. Remove the label only after recording that verdict and verifying the
   fixes. Repeat the check immediately before tagging and publishing, so a debt incurred
   during the release drill cannot pass unnoticed.

2. Build once, outside any checkout, with the build pins of the
   `Installed wheel` job (`build-requirements.txt` in `ci-gate.yml`):

   ```bash
   git archive --format=tar "$SHA" | tar -x -C "$WORK/src"
   "$BUILD_VENV/bin/python" -I -m build --sdist --wheel --no-isolation --outdir "$WORK/dist" "$WORK/src"
   (cd "$WORK/dist" && sha256sum lu_machinery-"$V"-py3-none-any.whl lu_machinery-"$V".tar.gz > SHA256SUMS)
   ```

3. Assemble a wheelhouse: the wheel plus
   `pip download --require-hashes --only-binary=:all: --no-deps -r requirements-machinery.lock`.
   The consumer lock is the release line followed by the dependency lock:

   ```text
   lu-machinery==$V \
       --hash=sha256:<wheel sha256>
   <requirements-machinery.lock at $SHA>
   ```

4. Accept it in a clean venv outside any checkout, with `$P` that venv's
   absolute interpreter:

   ```bash
   "$P" -m pip install --no-index --find-links "$WHEELHOUSE" --require-hashes -r requirements-machinery.lock
   "$P" -m pip check
   env -u PYTHONPATH "$P" -I -c 'import importlib.metadata as m, lu_machinery; print(m.version("lu-machinery")); print(lu_machinery.__file__)'
   ```

   Provision the wheel's Node dependencies under the runtime user and umask
   022. Set `$NPM_CACHE` to an absolute, empty release-owned cache for the
   first install. The release prepares its version-and-lock-derived prefix,
   selects Node matching that prefix's `.nvmrc`, and resolves the npm CLI
   inside that Node installation. Python never executes npm. Selection takes
   the highest matching-major Node among `PATH`, `/opt/homebrew/bin` and
   `/usr/local/bin`; `PATH` order breaks only ties. A minimal `PATH` that omits
   the host's pinned Node selects a distro Node of the same major. Put the
   pinned Node's directory on `PATH`, make sure no higher matching-major Node
   is also there, and check that `node --version` prints the pinned version.
   Afterwards confirm that `"$NODE" --version` and `"$NPM" --version` match
   the pinned versions; if not, discard that prefix and cache and rerun with
   a corrected `PATH`. Run this exact block after the wheel install:

   ```bash
   set -eu
   umask 022
   PREFIX="$(env -u PYTHONPATH "$P" -I -c 'from lu_machinery.agent_runtime.adapters.acpx import prepare_node_dependencies; print(prepare_node_dependencies() or "")')"
   if [ -n "$PREFIX" ]; then
     NODE="$(env -u PYTHONPATH "$P" -I -c 'from lu_machinery.agent_runtime.adapters.acpx import node_toolchain; print(node_toolchain()[0])')"
     NPM="$(env -u PYTHONPATH "$P" -I -c 'from lu_machinery.agent_runtime.adapters.acpx import node_toolchain; print(node_toolchain()[1])')"
     PATH="$(dirname "$NODE"):$PATH" "$NODE" "$NPM" ci --ignore-scripts --no-audit --no-fund --prefix="$PREFIX" --cache="$NPM_CACHE"
     env -u PYTHONPATH "$P" -I -c 'from pathlib import Path; import sys; from lu_machinery.agent_runtime.adapters.acpx import seal_node_dependencies; print(seal_node_dependencies(cache=Path(sys.argv[1])))' "$NPM_CACHE"
   fi
   ```

   The prefix is `$VENV/share/lu-machinery/node/<version>-<lock-sha256>`.
   Its version must match the wheel, and the full lock digest distinguishes
   dependency sets. Sealing retains integrity-addressed archives from npm's
   cache. Runtime admission verifies the copied contract, package versions,
   archive integrity, installed bytes, executable targets, owners and writable
   modes before any provider is spawned. Every applicable optional package is
   required: only lock OS/CPU/libc constraints excluding this host permit an
   optional package to be absent. Unknown host targets refuse admission.
   Every peer a present package declares, including names listed only under
   `peerDependenciesMeta`, must resolve from that package's own location to
   the nearest lock entry, and that entry must apply to the host. Otherwise
   Node would search ancestor and global module directories that admission
   does not authenticate. The one exemption is `b4a`'s `react-native-b4a`,
   which Node selects only under the `react-native` export condition. It is
   pinned to the exact lock entry in `acpx.py`, and any change to that entry
   refuses admission until the exemption is reviewed again.
   Python and Node must use the same host architecture. A missing, mismatched or unsealed
   prefix refuses and names this install-and-seal step. An ACP request never
   installs or repairs dependencies.

   An intact existing prefix is validated and reused: preparation prints an
   empty value, so the block skips npm. A failed or partial installation is
   retained for diagnosis and refuses reuse. Restore it from the retained
   release set before retrying; never run npm against an active prefix.

   Accept Node provisioning in a clean environment with no product checkout,
   ancestor `node_modules`, global ACP packages or npm cache. Record the wheel
   SHA-256, lock SHA-256, Node version and resolved prefix/module/executable
   paths. For every enabled ACP route, including the text server, complete
   initialization, session creation, a known-answer prompt and a terminal
   response with deterministic peers, then run a bounded authenticated canary
   for each enabled provider. Authenticated provider canaries run with the
   account HOME (the runtime user's real HOME). A scratch HOME is used only
   for the deterministic, tamper and marker-static steps. The deterministic
   Claude run is in the host namespace, not a synthetic root. Run the Claude
   canary in the host PID namespace, not a synthetic-root one, while the
   caller has same-account processes whose `/proc` entries it cannot read; it
   must return the known answer with `ok` true and leave no new launcher
   copy. Agent CLI lookup without an explicit search path (such as codex and
   grok) uses only `$HOME/.local/bin` and `$HOME/.opencode/bin`, not `PATH`.
   Claude is looked up on `PATH` first, then `$HOME/.local/bin/claude`;
   control `PATH` so the intended Claude is the one tested. The Claude
   launcher refuses any world-writable ancestor, so the canary venv and
   scratch `$HOME` must live outside `/tmp`. With registry access disabled, remove
   a package, introduce an incompatible version and tamper with an executable
   in separate restored copies. Each must refuse before prompt delivery. Import success
   is insufficient.

   Prove the prefix is the only module source. Plant a marker package, which
   records its own execution, in the prefix's parent `node_modules` and in a
   `NODE_PATH` directory. Plant one for every lock peer name and every lock
   package absent from the prefix. As positive controls, each marker must run
   when required from outside the prefix. Then run every enabled route
   through a terminal response, and the lazy require sites: `debug` with
   colours enabled, which reads `supports-color`, and
   `@modelcontextprotocol/sdk/validation/cfworker` in both CommonJS and ESM.
   Also run `b4a`, `bare-events`, `bare-stream` and `express`. Run the
   prefix's ACPX shim with `--version` under `PATH=/usr/bin:/bin`. It must
   succeed. Each route and each lazy site must execute zero markers. Repeat
   the routes with `NODE_OPTIONS=--conditions=react-native` and `NODE_PATH`
   set in the caller's environment. ACP Node processes never receive either
   variable, so the markers stay at zero, while a direct `node` run with that
   `NODE_OPTIONS` loading `b4a` must execute the `react-native-b4a` marker as
   the positive control. Remove the markers and confirm the prefix still
   validates.

   Then check that every packaged resource resolves from the installed wheel
   and exercise lazy runtime paths. Running the agent deployer's plan and apply
   against a scratch consumer is one such path. Import alone is not
   acceptance.

5. Tag and publish. Push the annotated tag `v$V` on `$SHA`, then create the
   release with all three assets in one call. The installed publisher resolves
   its repository context from the environment, so set
   `LEARN_UKRAINIAN_PRIMARY_REPO_ROOT` to the absolute primary git checkout
   that holds `batch_state/`; without it the command refuses:

   ```bash
   "$P" -m lu_machinery.publish release-create --repo learn-ukrainian/machinery --tag "v$V" \
     --title "lu-machinery $V" --notes-file "$NOTES" \
     --assets "$WORK/dist/lu_machinery-$V-py3-none-any.whl" \
     --assets "$WORK/dist/lu_machinery-$V.tar.gz" --assets "$WORK/dist/SHA256SUMS"
   ```

   Download the published assets and compare them with `SHA256SUMS` before
   reporting the release.

6. Install the wheelhouse files on the host (host operation, outside this
   repository).

## Consumers

A consumer adopts a release with one PR that replaces its
`requirements-machinery.lock` with the consumer lock above. Shared locks name
packages, versions and hashes only, never a host path. On the server,
consumers install from the host wheelhouse with the step 4 command. In CI, a
consumer downloads the exact release asset into a temporary wheelhouse,
downloads the locked dependencies into the same directory, and installs with
the same command. Rolling back restores the previous lock, wheel,
configuration, the matching Node prefix and generated deployments, then
verifies behaviour, including the ACP known-answer checks; reverting
the lock alone is not a rollback.

A consumer Codex session sets `LU_PRODUCT_ROOT` to the absolute product checkout.
The guards shipped in #137 already require that root; they do not infer the
product from the directory the hook file lives in. The interpreter is one rule:
`LEARN_UKRAINIAN_PYTHON` when that variable is an absolute path to an executable
file, and a refusal with no fallback when it is set to anything else; otherwise
the validated primary checkout's `.venv/bin/python`; otherwise a refusal. A
worktree-local `.venv` is not the interpreter, and nothing is discovered on
`PATH`.

A consumer that writes `sources.db` or `vesum.db` declares its own writers in
`scripts/config/sqlite_reference_allowlist.json` in the product checkout. The
file uses the format of machinery's `lu_machinery/hygiene/sqlite_reference_allowlist.json`:
a JSON list of objects, each with a checkout-relative `path` and a `kind`; rows
of kind `writer` are the declared writers. The release's corpus write guard
(`refuse_writable_corpus_database`) judges each stack frame by its real path,
with symlinks resolved. A frame inside the installed package counts only when
machinery's file lists it. Any other frame counts only when it lies inside the
product root that `LU_PRODUCT_ROOT` selects, which must be the checkout whose
code runs, and when the product's file lists it. A missing, unreadable or
malformed product file, or an unset product root, authorizes no product writer.
The guard then raises `ProductWriterDeclarationRefused`, with a path-free
`reason`. The guard reads the product file through descriptors that do not
follow symlinks (`O_PATH`). Product corpus writers are therefore supported on
Linux only, where the corpus store lives in the host checkout's local `data/`.
On a platform without `os.O_PATH` the reason is
`product_declaration_platform_unsupported`. Machinery's own writers are not
affected. The Sources store writer lock and selected-generation checks still
apply after the declaration check.

Offline reinstall uses the retained wheelhouse and npm cache. Restore the
wheel first, set `$P` to its absolute interpreter and `$NPM_CACHE` to the
retained absolute cache path, then run this offline variant:

```bash
set -eu
umask 022
PREFIX="$(env -u PYTHONPATH "$P" -I -c 'from lu_machinery.agent_runtime.adapters.acpx import prepare_node_dependencies; print(prepare_node_dependencies() or "")')"
if [ -n "$PREFIX" ]; then
  NODE="$(env -u PYTHONPATH "$P" -I -c 'from lu_machinery.agent_runtime.adapters.acpx import node_toolchain; print(node_toolchain()[0])')"
  NPM="$(env -u PYTHONPATH "$P" -I -c 'from lu_machinery.agent_runtime.adapters.acpx import node_toolchain; print(node_toolchain()[1])')"
  PATH="$(dirname "$NODE"):$PATH" "$NODE" "$NPM" ci --ignore-scripts --no-audit --no-fund --offline --prefix="$PREFIX" --cache="$NPM_CACHE"
  env -u PYTHONPATH "$P" -I -c 'from pathlib import Path; import sys; from lu_machinery.agent_runtime.adapters.acpx import seal_node_dependencies; print(seal_node_dependencies(cache=Path(sys.argv[1])))' "$NPM_CACHE"
fi
```

An intact retained prefix is validated and reused without npm or network
access. Never mix a previous wheel with the current release's prefix. Product
dependency removal is residual `R83-product`, owned by claude-cto for v0.2.0
after the release drill; this release does not remove product packages.
