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
configuration and generated deployments, then verifies behaviour; reverting
the lock alone is not a rollback.
