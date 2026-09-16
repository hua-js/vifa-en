#!/usr/bin/env bash
set -Eeuo pipefail

readonly DEFAULT_CRR_IMAGE="ccr.ccs.tencentyun.com/taidai-holobase-168/vifa-m4"

usage() {
  cat <<'EOF'
Build VIFA M4 locally for linux/amd64 or linux/arm64 and push to a registry.

Usage: bash m4/deploy/build-and-push.sh

Optional environment variables:
  CRR_IMAGE     Repository (default: Tencent CCR namespace / vifa-m4)
  RELEASE_DIR   New output directory for HTML/Flow/backend/manual/ZIP
  PYTHON_IMAGE  Build base image (default: python:3.12-slim-bookworm)
  PLATFORM      linux/amd64 (default) or linux/arm64

Requires Docker with buildx, python3, registry login, and a clean Git tree.
Builds an exported Git snapshot; no runtime secrets or local data are copied.
Pushes 0.1.0-<architecture> and m4-<git-sha12>-<architecture> tags.
Deployment packages pin the revision tag. Never starts production services.
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
readonly version="0.1.0"
platform="${PLATFORM:-linux/amd64}"
case "$platform" in linux/amd64|linux/arm64) ;; *)
  echo "PLATFORM must be linux/amd64 or linux/arm64." >&2; exit 64 ;;
esac
architecture="${platform#linux/}"
crr_host="${crr_image%%/*}"
if [[ ! "$crr_image" =~ ^[a-z0-9][a-z0-9.-]*(:[0-9]+)?/[a-z0-9][a-z0-9._/-]*$ ]]; then
  echo "CRR_IMAGE must be a registry/repository without a tag or digest." >&2; exit 64
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
revision_ref="${crr_image}:m4-${short_sha}-${architecture}"
version_ref="${crr_image}:${version}-${architecture}"
local_ref="vifa-m4:${version}-${architecture}"
release_dir="${RELEASE_DIR:-${repo_root}/outputs/m4/releases/${version}-${short_sha}-$(date +%Y%m%d-%H%M%S)-$$}"
if [[ -e "$release_dir" || -e "${release_dir}.zip" ]]; then
  echo "Release directory or ZIP already exists; choose a new RELEASE_DIR." >&2; exit 73
fi

snapshot_dir="$(mktemp -d "${TMPDIR:-/tmp}/m4-release.XXXXXXXX")"
trap 'rm -rf -- "$snapshot_dir"' EXIT
git -C "$repo_root" archive "$commit_sha" -- \
  m4/deploy 'm4/web/M4优化调度控制台-线上版.html' \
  m4/__init__.py m4/settings m4/optimizer m4/orchestrator m4/selection docs/m4/deploy \
  shared config/projects scripts/check_project.py 'docs/产品化配置与交付.md' | tar -x -C "$snapshot_dir"
python3 "$snapshot_dir/m4/deploy/build_package.py" \
  --output "$release_dir" --image "$revision_ref" --revision "$commit_sha" --platform "$platform"

echo "Building $local_ref from $commit_sha for $platform"
docker buildx build --platform "$platform" \
  --file "$release_dir/backend/Dockerfile" \
  --build-arg "PYTHON_IMAGE=${PYTHON_IMAGE:-python:3.12-slim-bookworm}" \
  --label "org.opencontainers.image.revision=$commit_sha" \
  --tag "$local_ref" --load "$release_dir/backend"
built_platform="$(docker image inspect "$local_ref" --format '{{.Os}}/{{.Architecture}}')"
if [[ "$built_platform" != "$platform" ]]; then
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
Before first deployment, configure the project instance name, backend alias, config path, port and data volume in backend/.env.
For updates, preserve those project-specific settings and credentials.
On the server, run the same commands for every update:
  cd /userdata/holo/pyfiles/m4/backend
  export M4_IMAGE=$revision_ref
  export M4_PLATFORM=$platform
  docker compose -f compose.yaml -f m4-production.override.yaml pull m4-api
  docker compose -f compose.yaml -f m4-production.override.yaml up -d --no-build --force-recreate m4-api
  docker compose -f compose.yaml -f m4-production.override.yaml ps
Preserve existing credentials and data. Follow the manual before updating a running service.
EOF
