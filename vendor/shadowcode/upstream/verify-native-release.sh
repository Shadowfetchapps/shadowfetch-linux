#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
# Offline verification only. No execution, installation, download, or trust update.
set -euo pipefail
export LC_ALL=C
umask 077
fail() { printf 'Release authentication refused: %s\n' "$*" >&2; exit 1; }
usage() { printf '%s\n' 'Usage: verify-native-release.sh --bundle-dir DIR --trust-dir TRUST --artifact BASENAME --stage-dir NEW-DIR [--previous-dir DIR] [--expect-version VERSION] [--expect-commit SHA]' >&2; exit 2; }
BUNDLE='' TRUST='' ARTIFACT='' OUTPUT='' PREVIOUS='' EXPECT_VERSION='' EXPECT_COMMIT=''
declare -A SEEN=()
while (( $# )); do
  [[ $# -ge 2 && -n "$2" ]] || usage
  [[ -z "${SEEN[$1]:-}" ]] || usage
  SEEN[$1]=1
  case "$1" in
    --bundle-dir) BUNDLE=$2 ;; --trust-dir) TRUST=$2 ;; --artifact) ARTIFACT=$2 ;;
    --stage-dir) OUTPUT=$2 ;; --previous-dir) PREVIOUS=$2 ;;
    --expect-version) EXPECT_VERSION=$2 ;; --expect-commit) EXPECT_COMMIT=$2 ;;
    *) usage ;;
  esac
  shift 2
done
[[ -n "$BUNDLE" && -n "$TRUST" && -n "$ARTIFACT" && -n "$OUTPUT" ]] || usage
[[ "$ARTIFACT" =~ ^[A-Za-z0-9_.-]+$ ]] || fail 'artifact must be a basename'
[[ -d "$BUNDLE" && -d "$TRUST" ]] || fail 'bundle and independently trusted key directory are required'
[[ ! -e "$OUTPUT" && ! -L "$OUTPUT" ]] || fail 'staging destination already exists'
command -v openssl >/dev/null || fail 'OpenSSL 3 is required'
[[ "$(openssl version)" == 'OpenSSL 3.'* ]] || fail 'OpenSSL 3 is required'
PARENT=$(realpath -- "$(dirname -- "$OUTPUT")") || fail 'staging parent missing'
BASE=$(basename -- "$OUTPUT")
[[ "$BASE" != . && "$BASE" != .. ]] || fail 'invalid staging destination'
OUTPUT="$PARENT/$BASE"
SCRATCH=$(mktemp -d "$PARENT/.shadowcode-auth.XXXXXX")
finish() { rm -rf -- "$SCRATCH"; }
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# shellcheck source=native-release-auth-lib.sh
source "$(dirname "$0")/native-release-auth-lib.sh"
load_trust_policy
copy_file "$BUNDLE/RELEASE-AUTH" "$SCRATCH/RELEASE-AUTH" 4096
copy_file "$BUNDLE/RELEASE-AUTH.sig" "$SCRATCH/RELEASE-AUTH.sig" 64
verify_envelope "$SCRATCH/RELEASE-AUTH" "$SCRATCH/RELEASE-AUTH.sig" 0
CURRENT_VERSION=$EN_VERSION; CURRENT_EPOCH=$EN_EPOCH; CURRENT_HASH=$(hash_file "$SCRATCH/RELEASE-AUTH")
[[ -z "$EXPECT_VERSION" || "$EXPECT_VERSION" == "$EN_VERSION" ]] || fail 'unexpected version'
[[ -z "$EXPECT_COMMIT" || "$EXPECT_COMMIT" == "$EN_COMMIT" ]] || fail 'unexpected commit'
if [[ -n "$PREVIOUS" ]]; then
  copy_file "$PREVIOUS/RELEASE-AUTH" "$SCRATCH/previous-auth" 4096
  copy_file "$PREVIOUS/RELEASE-AUTH.sig" "$SCRATCH/previous-auth.sig" 64
  verify_envelope "$SCRATCH/previous-auth" "$SCRATCH/previous-auth.sig" 1
  (( CURRENT_EPOCH >= EN_EPOCH )) || fail 'signing epoch rollback'
  version_compare "$CURRENT_VERSION" "$EN_VERSION"; (( CMP >= 0 )) || fail 'release downgrade'
  if (( CMP == 0 )); then [[ "$CURRENT_HASH" == "$(hash_file "$SCRATCH/previous-auth")" ]] || fail 'changed release under accepted version'; fi
  parse_envelope "$SCRATCH/RELEASE-AUTH"
fi
copy_file "$BUNDLE/RELEASE-MANIFEST.json" "$SCRATCH/RELEASE-MANIFEST.json" 1048576
copy_file "$BUNDLE/SHA256SUMS" "$SCRATCH/SHA256SUMS" 4096
[[ "$(hash_file "$SCRATCH/RELEASE-MANIFEST.json")" == "$EN_MANIFEST" ]] || fail 'manifest digest mismatch'
[[ "$(hash_file "$SCRATCH/SHA256SUMS")" == "$EN_SUMS" ]] || fail 'checksums digest mismatch'
SELECTED=-1
for i in 0 1 2; do
  printf '%s  %s\n' "${EN_HASHES[$i]}" "${EN_NAMES[$i]}" >> "$SCRATCH/expected-sums"
  [[ "$ARTIFACT" != "${EN_NAMES[$i]}" ]] || SELECTED=$i
done
cmp -s -- "$SCRATCH/SHA256SUMS" "$SCRATCH/expected-sums" || fail 'checksums asset set mismatch'
(( SELECTED >= 0 )) || fail 'artifact must be one exact authenticated basename'
copy_file "$BUNDLE/$ARTIFACT" "$SCRATCH/$ARTIFACT" "$MAX_ARTIFACT"
[[ "$(stat -c %s -- "$SCRATCH/$ARTIFACT")" == "${EN_SIZES[$SELECTED]}" && "$(hash_file "$SCRATCH/$ARTIFACT")" == "${EN_HASHES[$SELECTED]}" ]] || fail 'artifact digest/size mismatch'
rm -f -- "$SCRATCH/trust-policy" "$SCRATCH/public-key.pem" "$SCRATCH/public-key.der" "$SCRATCH/public-key.canonical.pem" "$SCRATCH/expected-sums" "$SCRATCH/previous-auth" "$SCRATCH/previous-auth.sig"
# Same-filesystem rename; no existing output may be replaced. The successful
# output contains only verified private snapshots, never the live inputs.
mv -T -n -- "$SCRATCH" "$OUTPUT" || fail 'could not retain verified stage'
[[ ! -e "$SCRATCH" ]] || fail 'staging destination appeared concurrently'
trap - EXIT INT TERM
printf 'Publisher signature verified: version=%s artifact=%s sha256=%s\n' "$EN_VERSION" "$ARTIFACT" "${EN_HASHES[$SELECTED]}"
