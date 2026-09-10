#!/usr/bin/env bash
set -Eeuo pipefail

readonly DEFAULT_CRR_IMAGE="ccr.ccs.tencentyun.com/taidai-holobase-168/omnipower_vifa"
readonly DEFAULT_VERSION="0.1.0"
readonly DEFAULT_PLATFORM="linux/amd64"
readonly LOCAL_IMAGE_NAME="vifa-m3"

usage() {
  cat <<'EOF'
Build the VIFA M3 linux/amd64 image and push it to Tencent Cloud CCR.

Usage:
  m3/deploy/build-and-push.sh

Optional environment variables:
  CRR_IMAGE   Remote repository (default: ccr.ccs.tencentyun.com/taidai-holobase-168/omnipower_vifa)
  VERSION     Release tag (default: 0.1.0)
  PLATFORM    Docker target platform (default: linux/amd64)

The script pushes both VERSION and <git-short-sha>-amd64 tags.
The Git worktree must be clean so the revision tag identifies the built code.
EOF
}

if [[ ${1:-} == "--help" || ${1:-} == "-h" ]]; then
  usage
  exit 0
fi

if [[ $# -ne 0 ]]; then
  echo "Unexpected argument: $1" >&2
  usage >&2
  exit 64
fi

for command_name in docker git grep; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Required command not found: $command_name" >&2
    exit 69
  fi
done

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(git -C "$script_dir" rev-parse --show-toplevel 2>/dev/null)" || {
  echo "VIFA Git repository could not be resolved from: $script_dir" >&2
  exit 69
}

if [[ ! -f "$repo_root/m3/deploy/Dockerfile" ]]; then
  echo "M3 Dockerfile not found: $repo_root/m3/deploy/Dockerfile" >&2
  exit 66
fi

if [[ -n "$(git -C "$repo_root" status --porcelain --untracked-files=normal)" ]]; then
  echo "Git worktree is not clean. Commit or stash changes before publishing." >&2
  git -C "$repo_root" status --short >&2
  exit 65
fi

crr_image="${CRR_IMAGE:-$DEFAULT_CRR_IMAGE}"
version="${VERSION:-$DEFAULT_VERSION}"
platform="${PLATFORM:-$DEFAULT_PLATFORM}"
crr_host="${crr_image%%/*}"

if [[ "$crr_image" != */* || "$crr_host" == "$crr_image" ]]; then
  echo "CRR_IMAGE must include a registry host and repository path: $crr_image" >&2
  exit 64
fi

if [[ ! "$version" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]]; then
  echo "VERSION is not a valid Docker tag: $version" >&2
  exit 64
fi

if [[ "$platform" != "linux/amd64" ]]; then
  echo "This release script only supports PLATFORM=linux/amd64; got: $platform" >&2
  exit 64
fi

docker_config_dir="${DOCKER_CONFIG:-${HOME}/.docker}"
docker_config_file="$docker_config_dir/config.json"
if [[ ! -r "$docker_config_file" ]] || ! grep -Fq "\"$crr_host\"" "$docker_config_file"; then
  echo "Docker login for $crr_host was not found." >&2
  echo "Run: docker login $crr_host" >&2
  exit 77
fi

if ! docker info >/dev/null 2>&1; then
  echo "Docker daemon is unavailable. Start Docker and try again." >&2
  exit 69
fi

commit_sha="$(git -C "$repo_root" rev-parse HEAD)"
short_sha="$(git -C "$repo_root" rev-parse --short=7 HEAD)"
revision_tag="${short_sha}-amd64"
local_ref="${LOCAL_IMAGE_NAME}:${version}"
version_ref="${crr_image}:${version}"
revision_ref="${crr_image}:${revision_tag}"

echo "Building $local_ref from commit $commit_sha for $platform"
docker buildx build \
  --platform "$platform" \
  --file "$repo_root/m3/deploy/Dockerfile" \
  --label "org.opencontainers.image.revision=$commit_sha" \
  --tag "$local_ref" \
  --load \
  "$repo_root"

built_platform="$(docker image inspect "$local_ref" --format '{{.Os}}/{{.Architecture}}')"
if [[ "$built_platform" != "$platform" ]]; then
  echo "Built image platform mismatch: expected $platform, got $built_platform" >&2
  exit 70
fi

docker tag "$local_ref" "$revision_ref"
docker tag "$local_ref" "$version_ref"

echo "Pushing immutable revision tag: $revision_ref"
docker push "$revision_ref"

echo "Pushing release tag: $version_ref"
docker push "$version_ref"

echo "Verifying published release manifest"
docker buildx imagetools inspect "$version_ref"

cat <<EOF

Published successfully:
  $revision_ref
  $version_ref

Production update:
  cd /userdata/holo/pyfiles/vifa-m3
  docker pull $version_ref
  docker tag $version_ref vifa-m3:0.1.0
  docker compose -f m3/deploy/compose.yaml up -d --no-build --force-recreate vifa-m3-worker vifa-m3-dashboard
  docker compose -f m3/deploy/compose.yaml ps
EOF
