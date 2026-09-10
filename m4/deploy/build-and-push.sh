#!/usr/bin/env bash
set -Eeuo pipefail

readonly DEFAULT_CRR_IMAGE="ccr.ccs.tencentyun.com/taidai-holobase-168/omnipower_vifa"

usage() {
  cat <<'EOF'
Build VIFA M4 locally for linux/amd64 and push to Tencent Cloud CCR.

Usage: bash m4/deploy/build-and-push.sh

Optional environment variables:
  CRR_IMAGE     Repository (same default as M3)
  VERSION       Release tag, must start with m4- (default: m4-0.1.0)
  RELEASE_DIR   New output directory for HTML/Flow/backend/manual/ZIP
  PYTHON_IMAGE  Build base image (default: python:3.12-slim-bookworm)

Requires Docker with buildx, python3, registry login, and a clean Git tree.
Builds an exported Git snapshot; no runtime secrets or local data are copied.
Pushes VERSION and m4-<git-sha12>-amd64. Never starts production services.
EOF
}

if [[ ${1:-} == --help || ${1:-} == -h ]]; then usage; exit 0; fi
if [[ $# -ne 0 ]]; then usage >&2; exit 64; fi

for command_name in docker git python3 tar; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Required command not found: $command_name" >&2; exit 69;
  }
done

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(git -C "$script_dir" rev-parse --show-toplevel)"
crr_image="${CRR_IMAGE:-$DEFAULT_CRR_IMAGE}"
version="${VERSION:-m4-0.1.0}"
crr_host="${crr_image%%/*}"
if [[ ! "$crr_image" =~ ^[a-z0-9][a-z0-9.-]*(:[0-9]+)?/[a-z0-9][a-z0-9._/-]*$ ]]; then
  echo "CRR_IMAGE must be a registry/repository without a tag or digest." >&2; exit 64
fi
if [[ ! "$version" =~ ^m4-[A-Za-z0-9_][A-Za-z0-9_.-]{0,123}$ ]]; then
  echo "VERSION must be a valid m4-prefixed tag, for example m4-0.1.0." >&2; exit 64
fi
if [[ -n "$(git -C "$repo_root" status --porcelain --untracked-files=normal)" ]]; then
  echo "Git worktree is not clean. Commit the intended release before publishing (same rule as M3)." >&2
  exit 65
fi
docker info >/dev/null
docker buildx version >/dev/null
# Docker itself handles login and credential helpers. Do not read or print credentials.
echo "Using registry $crr_host; run docker login $crr_host first if needed."

commit_sha="$(git -C "$repo_root" rev-parse HEAD)"
short_sha="${commit_sha:0:12}"
revision_ref="${crr_image}:m4-${short_sha}-amd64"
version_ref="${crr_image}:${version}"
local_ref="vifa-m4:${version}"
release_dir="${RELEASE_DIR:-${repo_root}/outputs/m4/releases/${version}-${short_sha}}"
if [[ -e "$release_dir" || -e "${release_dir}.zip" ]]; then
  echo "Release directory or ZIP already exists; choose a new RELEASE_DIR." >&2; exit 73
fi

snapshot_dir="$(mktemp -d "${TMPDIR:-/tmp}/m4-release.XXXXXXXX")"
trap 'rm -rf -- "$snapshot_dir"' EXIT
git -C "$repo_root" archive "$commit_sha" -- \
  m4/deploy 'm4/web/M4优化调度控制台-线上版.html' \
  m4/__init__.py m4/settings m4/optimizer m4/orchestrator m4/selection docs/m4/deploy | tar -x -C "$snapshot_dir"
python3 "$snapshot_dir/m4/deploy/build_package.py" \
  --output "$release_dir" --image "$revision_ref" --revision "$commit_sha"

echo "Building $local_ref from $commit_sha for linux/amd64"
docker buildx build --platform linux/amd64 \
  --file "$release_dir/backend/Dockerfile" \
  --build-arg "PYTHON_IMAGE=${PYTHON_IMAGE:-python:3.12-slim-bookworm}" \
  --label "org.opencontainers.image.revision=$commit_sha" \
  --tag "$local_ref" --load "$release_dir/backend"
built_platform="$(docker image inspect "$local_ref" --format '{{.Os}}/{{.Architecture}}')"
if [[ "$built_platform" != linux/amd64 ]]; then
  echo "Built image platform mismatch: $built_platform" >&2; exit 70
fi

docker tag "$local_ref" "$revision_ref"
docker tag "$local_ref" "$version_ref"
docker push "$revision_ref"
docker push "$version_ref"
docker buildx imagetools inspect "$revision_ref"
docker buildx imagetools inspect "$version_ref"

cat <<EOF

Published:
  $revision_ref
  $version_ref
Matching deployment files: $release_dir
Upload the package contents to /userdata/holo/pyfiles/m4.
On the server, set backend/.env M4_IMAGE=$revision_ref, then run:
  cd /userdata/holo/pyfiles/m4/backend
  docker compose pull m4-api
  docker compose up -d --no-build m4-api
  docker compose ps
Preserve existing credentials and data. Follow the manual before updating a running service.
EOF
