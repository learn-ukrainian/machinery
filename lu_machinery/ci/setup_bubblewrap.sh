#!/usr/bin/env bash
# Install bubblewrap and grant user namespaces on disposable CI VMs only.
# Optional package arguments keep shard dependencies in the same apt transaction.
# Use image package lists first; switch to fallback mirrors only if that fails.
set -euo pipefail

: "${RUNNER_TEMP:?RUNNER_TEMP must be set}"

sudo rm -f /etc/apt/sources.list.d/*chrome* /etc/apt/sources.list.d/*google* \
  /var/lib/apt/lists/*chrome* /var/lib/apt/lists/*google*
missing=()
for pkg in bubblewrap "$@" apparmor; do
  dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
done
if [ "${#missing[@]}" -gt 0 ]; then
  echo "Installing missing native packages: ${missing[*]}"
  apt_opts=(-o Acquire::Retries=0 -o Acquire::http::Timeout=10 -o Acquire::https::Timeout=10)
  # Hard process-level budgets (40 s + 15 s + 65 s + 55 s = 175 s including 5 s kill grace)
  # leave 65 s inside test_env.sh's 240 s wait budget for apparmor/bwrap and PostgreSQL startup.
  if ! sudo timeout --kill-after=5 35 apt-get "${apt_opts[@]}" install -y --no-install-recommends "${missing[@]}"; then
    # During the 2026-11-11 Canonical *.ubuntu.com DNS outage (status.canonical.com),
    # azure.archive.ubuntu.com, archive.ubuntu.com and security.ubuntu.com all failed to
    # resolve, and refreshing the default mirror list first blew test_env.sh's 240 s budget.
    # Switch immediately to Tier-1 HTTPS Ubuntu mirrors outside *.ubuntu.com (keeping
    # archive.ubuntu.com last); apt still verifies Signed-By ubuntu-archive-keyring.gpg.
    echo 'apt install failed against the image package lists; switching to fallback HTTPS Ubuntu mirrors'
    sudo timeout --kill-after=5 10 dpkg --configure -a
    printf '%s\tpriority:%d\n' \
      'https://mirrors.edge.kernel.org/ubuntu/' 1 \
      'https://ftp.osuosl.org/pub/ubuntu/' 2 \
      'https://archive.ubuntu.com/ubuntu/' 3 | sudo tee /etc/apt/apt-mirrors.txt >/dev/null
    sudo chmod 0644 /etc/apt/apt-mirrors.txt
    if [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then
      sudo sed -i -E \
        's@^(URIs:[[:space:]]+).*(https?://([a-z0-9.-]+\.)?ubuntu\.com/ubuntu/?|mirror\+file:/etc/apt/apt-mirrors\.txt)([[:space:]]|$).*@\1mirror+file:/etc/apt/apt-mirrors.txt@' \
        /etc/apt/sources.list.d/ubuntu.sources
    fi
    if [ -f /etc/apt/sources.list ]; then
      sudo sed -i -E \
        '/^[[:space:]]*#/! s@https?://([a-z0-9.-]+\.)?ubuntu\.com/ubuntu/?([[:space:]]|$)@mirror+file:/etc/apt/apt-mirrors.txt\2@g' \
        /etc/apt/sources.list
    fi
    sudo timeout --kill-after=5 60 apt-get "${apt_opts[@]}" update -qq
    sudo timeout --kill-after=5 50 apt-get "${apt_opts[@]}" install -y --no-install-recommends "${missing[@]}"
  fi
fi

# The write boundary's private Git and HOME layers use bubblewrap's --overlay-src (0.10 and later);
# Ubuntu 24.04 ships 0.9.0. Install Ubuntu's own 0.11.1 build, pinned by digest (its dependencies,
# libc6 >= 2.38, libcap2 and libselinux1, are on the image), before the userns grant below applies to
# /usr/bin/bwrap. The 51 KB fetch tries kernel.org first (outside *.ubuntu.com, see the outage note
# above); the snapshot URL keeps the pinned file reachable after the archive pool drops it. Worst case
# 3 x 12 s + 20 s = 56 s on top of the 175 s above stays inside test_env.sh's 240 s wait budget.
has_overlay_src() {
  local help
  help="$(bwrap --help 2>&1 || true)"
  case "$help" in *--overlay-src*) return 0 ;; *) return 1 ;; esac
}
if ! has_overlay_src; then
  deb=bubblewrap_0.11.1-1ubuntu0.3_amd64.deb
  digest=d3a6c1b6b0e0474eaed6dbd055c0d601fd875af153976a0dc4aae75f4ea10868
  fetched=
  for mirror in https://mirrors.edge.kernel.org/ubuntu https://archive.ubuntu.com/ubuntu \
    https://snapshot.ubuntu.com/ubuntu/20261113T000000Z; do
    if curl -fsSL --connect-timeout 4 --max-time 12 -o "${RUNNER_TEMP}/${deb}" \
      "${mirror}/pool/main/b/bubblewrap/${deb}"; then
      fetched=1
      break
    fi
  done
  if [ -z "$fetched" ]; then
    echo "::error::${deb} could not be fetched from any mirror"
    exit 1
  fi
  echo "${digest}  ${RUNNER_TEMP}/${deb}" | sha256sum -c -
  sudo timeout --kill-after=5 15 dpkg -i "${RUNNER_TEMP}/${deb}"
  if ! has_overlay_src; then
    echo "::error::bubblewrap still lacks --overlay-src after installing ${deb}"
    exit 1
  fi
fi

# Ubuntu-hosted runners require an application-specific userns grant. This
# profile belongs only to the disposable CI VM.
if [ -f /proc/sys/kernel/apparmor_restrict_unprivileged_userns ]; then
  cat > "${RUNNER_TEMP}/v4-ci-bwrap.apparmor" <<'PROFILE'
abi <abi/4.0>,
include <tunables/global>
profile v4_ci_bwrap /usr/bin/bwrap flags=(unconfined) {
  userns,
}
PROFILE
  sudo apparmor_parser -r "${RUNNER_TEMP}/v4-ci-bwrap.apparmor"
fi
bwrap --unshare-user --unshare-pid --ro-bind / / -- /usr/bin/true
