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

1. Build once, outside any checkout, with the build pins of the
   `Installed wheel` job (`build-requirements.txt` in `ci-gate.yml`):

   ```bash
   git archive --format=tar "$SHA" | tar -x -C "$WORK/src"
   "$BUILD_VENV/bin/python" -I -m build --sdist --wheel --no-isolation --outdir "$WORK/dist" "$WORK/src"
   (cd "$WORK/dist" && sha256sum lu_machinery-"$V"-py3-none-any.whl lu_machinery-"$V".tar.gz > SHA256SUMS)
   ```

2. Assemble a wheelhouse: the wheel plus
   `pip download --require-hashes --only-binary=:all: --no-deps -r requirements-machinery.lock`.
   The consumer lock is the release line followed by the dependency lock:

   ```text
   lu-machinery==$V \
       --hash=sha256:<wheel sha256>
   <requirements-machinery.lock at $SHA>
   ```

3. Accept it in a clean venv outside any checkout, with `$P` that venv's
   absolute interpreter:

   ```bash
   "$P" -m pip install --no-index --find-links "$WHEELHOUSE" --require-hashes -r requirements-machinery.lock
   "$P" -m pip check
   env -u PYTHONPATH "$P" -I -c 'import importlib.metadata as m, lu_machinery; print(m.version("lu-machinery")); print(lu_machinery.__file__)'
   ```

   Provision the wheel's Node dependencies under the runtime user and umask
   022. Install Node matching the major in the shipped `.nvmrc`, with npm
   beside that Node executable. The runtime admits no product dependency
   tree, global ACP package or package-exec fallback. Set `$NPM_CACHE` to an
   empty release-owned cache for the first install:

   ```bash
   umask 022
   env -u PYTHONPATH "$P" -I -c 'from pathlib import Path; import sys; from lu_machinery.agent_runtime.adapters.acpx import provision_node_dependencies; print(provision_node_dependencies(cache=Path(sys.argv[1])))' "$NPM_CACHE"
   ```

   This explicit install runs `npm ci --ignore-scripts --no-audit --no-fund`
   into `$VENV/share/lu-machinery/node/<version>-<lock-sha256>`. The version
   must match the wheel, and the full lock digest distinguishes dependency
   sets. It retains the installed packages' integrity-addressed archives.
   Runtime admission verifies the copied contract, package versions, archive
   integrity, installed file bytes, contained executable links, owners and
   writable modes before any dependency is executed. An ACP request never
   installs or repairs dependencies. A failed provisioning attempt leaves a
   refusing prefix for diagnosis; restore it from the retained release set
   before retrying. Do not run `npm ci` against an active prefix.

   Accept Node provisioning in a clean environment with no product checkout,
   ancestor `node_modules`, global ACP packages or npm cache. Record the wheel
   SHA-256, lock SHA-256, Node version and resolved prefix/module/executable
   paths. For every enabled ACP route, including the text server, complete
   initialization, session creation, a known-answer prompt and a terminal
   response with deterministic peers, then run a bounded authenticated canary
   for each enabled provider. With registry access disabled, remove a package,
   introduce an incompatible version and tamper with an executable in separate
   restored copies. Each must refuse before prompt delivery. Import success
   is insufficient.

   Then check that every packaged resource resolves from the installed wheel
   and exercise lazy runtime paths. Running the agent deployer's plan and apply
   against a scratch consumer is one such path. Import alone is not
   acceptance.

4. Tag and publish. Push the annotated tag `v$V` on `$SHA`, then create the
   release with all three assets in one call:

   ```bash
   "$P" -m lu_machinery.publish release-create --repo learn-ukrainian/machinery --tag "v$V" \
     --title "lu-machinery $V" --notes-file "$NOTES" \
     --assets "$WORK/dist/lu_machinery-$V-py3-none-any.whl" \
     --assets "$WORK/dist/lu_machinery-$V.tar.gz" --assets "$WORK/dist/SHA256SUMS"
   ```

   Download the published assets and compare them with `SHA256SUMS` before
   reporting the release.

5. Install the wheelhouse files on the host (host operation, outside this
   repository).

## Consumers

A consumer adopts a release with one PR that replaces its
`requirements-machinery.lock` with the consumer lock above. Shared locks name
packages, versions and hashes only, never a host path. On the server,
consumers install from the host wheelhouse with the step 3 command. In CI, a
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

Offline reinstall uses the retained wheelhouse and npm cache. Invoke
`provision_node_dependencies(cache=Path(...), offline=True)` from the restored
wheel's interpreter to run `npm ci --ignore-scripts --offline`. An intact
retained prefix is validated and reused without npm or network access. Never
mix a previous wheel with the current release's prefix. Product dependency
removal is residual `R83-product`, owned by claude-cto for v0.2.0 after the
release drill; this release does not remove product packages.
