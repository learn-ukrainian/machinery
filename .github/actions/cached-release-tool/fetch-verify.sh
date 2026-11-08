#!/usr/bin/env bash
set -euo pipefail
archive="$RUNNER_TEMP/release-cache/$TOOL/archive.tar.gz"

case "${1:-}" in
  download)
    mkdir -p "$(dirname "$archive")"
    echo "Downloading $TOOL release archive (cache miss)."
    # The initial authenticated host is fixed; curl strips Authorization
    # on redirects to a different host. No --location-trusted or tracing.
    url="https://api.github.com/repos/$RELEASE_REPOSITORY/releases/assets/$ASSET_ID"
    curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
      --retry 30 --retry-all-errors --retry-delay 10 --retry-max-time 300 \
      --connect-timeout 20 --max-time 60 \
      --header 'Accept: application/octet-stream' \
      --header "Authorization: Bearer $GH_TOKEN" --output "$archive" "$url"
    ;;
  verify)
    printf '%s  %s\n' "$SHA256" "$archive" | sha256sum --check --strict
    mkdir -p "$RUNNER_TEMP/$TOOL"
    tar -xzf "$archive" -C "$RUNNER_TEMP/$TOOL" --strip-components="$STRIP_COMPONENTS" "$MEMBER"
    ;;
  *)
    echo "Usage: fetch-verify.sh download|verify" >&2
    exit 2
    ;;
esac
