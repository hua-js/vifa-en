#!/usr/bin/env bash
set -Eeuo pipefail

readonly DEFAULT_CRR_IMAGE="ccr.ccs.tencentyun.com/taidai-holobase-168/vifa-m4"

usage() {
  cat <<'EOF'
Build VIFA M4 locally for linux/amd64 or linux/arm64 and push to a registry.

Usage: bash m4/deploy/build-and-push.sh

Optional environment variables:
  CRR_IMAGE     Repository (default: Tencent CCR namespace / vifa-m4)
  RELEASE_DIR   New output directory for deployment files and release records
  PYTHON_IMAGE  Build base image (default: python:3.12-slim-bookworm)
  PLATFORM      linux/amd64 (default) or linux/arm64

Requires Docker with buildx, python3 and registry login. No commit required.
Packages the current working tree through build_package.py source allowlists,
including uncommitted/new source files; runtime secrets and local data are excluded.
Does not generate Flow JSON or standalone HTML copies. Update the Node-RED
Template from the canonical workspace HTML when the frontend changes.
Pushes only the fixed 0.1.0-<architecture> tag; the Git revision stays in image labels.
Prints production update commands with preflight checks and rollback image retention.
Never starts production services.
EOF
}

if [[ ${1:-} == --help || ${1:-} == -h ]]; then usage; exit 0; fi
if [[ $# -ne 0 ]]; then usage >&2; exit 64; fi

for command_name in docker git python3; do
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
docker info >/dev/null
docker buildx version >/dev/null
# Docker itself handles login and credential helpers. Do not read or print credentials.
echo "Using registry $crr_host; run docker login $crr_host first if needed."

commit_sha="$(git -C "$repo_root" rev-parse HEAD)"
short_sha="${commit_sha:0:12}"
version_ref="${crr_image}:${version}-${architecture}"
local_ref="vifa-m4:${version}-${architecture}"
release_dir="${RELEASE_DIR:-${repo_root}/outputs/m4/releases/${version}-${short_sha}-$(date +%Y%m%d-%H%M%S)-$$}"
if [[ -e "$release_dir" ]]; then
  echo "Release directory already exists; choose a new RELEASE_DIR." >&2; exit 73
fi

# Only this invocation's mktemp directory is removed, including on failure.
build_root="$(mktemp -d "${TMPDIR:-/tmp}/vifa-m4-build.XXXXXXXX")"
trap 'rm -rf -- "$build_root"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
build_dir="$build_root/package"

python3 "$repo_root/m4/deploy/build_package.py" \
  --output "$build_dir" --image "$version_ref" --revision "$commit_sha" --platform "$platform"
# A commit alone cannot identify uncommitted code. Bind the exact packaged
# content to the image and the printed production verification command.
source_sha="$(python3 -c 'import hashlib,sys; from pathlib import Path; print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest())' "$build_dir/SHA256SUMS")"

echo "Building $local_ref from current workspace (Git base $commit_sha, source $source_sha) for $platform"
docker buildx build --platform "$platform" \
  --file "$build_dir/backend/Dockerfile" \
  --build-arg "PYTHON_IMAGE=${PYTHON_IMAGE:-python:3.12-slim-bookworm}" \
  --label "org.opencontainers.image.revision=$commit_sha" \
  --label "vifa.m4.source-sha256=$source_sha" \
  --tag "$local_ref" --load "$build_dir/backend"
built_platform="$(docker image inspect "$local_ref" --format '{{.Os}}/{{.Architecture}}')"
if [[ "$built_platform" != "$platform" ]]; then
  echo "Built image platform mismatch: $built_platform" >&2; exit 70
fi

docker tag "$local_ref" "$version_ref"
docker push "$version_ref"
docker buildx imagetools inspect "$version_ref"

# Retain deployable assets and content evidence, never the full source tree.
python3 - "$build_dir" "$release_dir" "$source_sha" <<'PY_RELEASE'
import hashlib
import json
import shutil
import sys
from pathlib import Path
source, target = map(Path, sys.argv[1:3])
shutil.copytree(source, target, ignore=shutil.ignore_patterns('app'))
(target / 'SHA256SUMS').rename(target / 'SOURCE_SHA256SUMS')
metadata_path = target / 'release.json'
metadata = json.loads(metadata_path.read_text())
metadata['source_sha256'] = sys.argv[3]
metadata['status'] = 'published'
metadata_path.write_text(json.dumps(metadata, indent=2) + '\n')
manifest = [hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.relative_to(target).as_posix()
            for p in sorted(target.rglob('*')) if p.is_file()]
(target / 'SHA256SUMS').write_text('\n'.join(manifest) + '\n')
PY_RELEASE
rm -rf -- "$build_root"

cat <<EOF

Published:
  $version_ref

服务器更新命令（VIFA 电站2 EMS计划表联调）：
将下面从左括号到右括号的完整代码块复制到服务器终端执行。
The fixed tag is always pulled and checked before recreating the service.
The previous image is retained for rollback; no images are automatically deleted.
Existing project settings, credentials and data volumes are preserved.

(
  set -eu
  cd /userdata/holo/pyfiles/m4/backend
  export M4_IMAGE=$version_ref
  export M4_PLATFORM=$platform
  export M4_AUTO_PLAN_ENABLED=1
  m4_compose() {
    docker compose -f compose.yaml -f m4-production.override.yaml -f m4-ems-table.override.yaml "\$@"
  }
  # Older server Compose files do not forward the exported automatic-plan switch.
  # Preserve the existing override and add the missing environment key only.
  python3 - <<'PY_AUTO'
from datetime import datetime
from pathlib import Path
import re
import shutil

path = Path("m4-ems-table.override.yaml")
text = path.read_text()
if not re.search(r"^\s*M4_AUTO_PLAN_ENABLED\s*:", text, re.M):
    matches = list(re.finditer(r"^([ \t]*)M4_EMS_STATION2_TABLE_WRITES\s*:[^\r\n]*", text, re.M))
    if len(matches) != 1:
        raise SystemExit("Cannot locate a unique EMS switch; override unchanged.")
    match = matches[0]
    backup = path.with_name(path.name + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
    shutil.copy2(path, backup)
    text = text[:match.end()] + '\n' + match.group(1) + 'M4_AUTO_PLAN_ENABLED: "1"' + text[match.end():]
    path.write_text(text)
    print("Automatic-plan switch added; backup:", backup)
PY_AUTO
  m4_previous_container="\$(m4_compose ps -aq m4-api)"
  if [ -n "\$m4_previous_container" ]; then
    m4_previous_image="\$(docker inspect --format '{{.Image}}' "\$m4_previous_container")"
    m4_rollback_tag="vifa-m4-rollback:\$(date +%Y%m%d-%H%M%S)"
    docker image tag "\$m4_previous_image" "\$m4_rollback_tag"
    echo "Rollback image retained: \$m4_rollback_tag"
  fi
  m4_compose pull m4-api
  m4_actual_source="\$(docker image inspect --format '{{index .Config.Labels "vifa.m4.source-sha256"}}' "\$M4_IMAGE")"
  [ "\$m4_actual_source" = "$source_sha" ] || { echo "Source content mismatch; deployment stopped before recreation."; exit 1; }
  m4_expected_image="\$(docker image inspect --format '{{.Id}}' "\$M4_IMAGE")"
  m4_check_code='
import os
from shared.project import get_project, load_project

assert os.environ.get("M4_AUTO_PLAN_ENABLED") == "1", "Automatic plans disabled or missing"
assert os.environ.get("M4_EMS_STATION2_TABLE_WRITES") == "1", "EMS table write override missing"
project = get_project()
assert project.id == "vifa", "These deployment commands are for VIFA only"
expected = load_project("/app/config/projects/vifa.json").station("station-2").m4
actual = project.station("station-2").m4
for key in ("telemetry_max_charge_kw", "telemetry_soc_upper_exclusive_pct", "ems_charge_kw"):
    assert actual[key] == expected[key], f"Project configuration mismatch: {key}; update external project.json"
    print(key, "=", actual[key])
print("Automatic plans and EMS table writes: enabled")
print("Project configuration:", os.environ.get("VIFA_PROJECT_CONFIG") or "built-in")
'
  m4_compose run --rm --no-deps -T --entrypoint python m4-api -c "\$m4_check_code"
  m4_compose up -d --no-build --force-recreate --wait --wait-timeout 120 m4-api
  m4_current_container="\$(m4_compose ps -q m4-api)"
  m4_current_image="\$(docker inspect --format '{{.Image}}' "\$m4_current_container")"
  [ "\$m4_current_image" = "\$m4_expected_image" ] || { echo "Running image mismatch; rollback image retained, no automatic rollback."; exit 1; }
  m4_compose exec -T m4-api python -c "\$m4_check_code"
  m4_compose images m4-api
  m4_compose ps
  m4_compose logs --since 5m --tail=80 m4-api
)

Frontend: replace the M4 Node-RED Template content with:
  $repo_root/m4/web/M4优化调度控制台-线上版.html
Use the source version matching release.json's html_sha256.
Then deploy the updated Template in Node-RED. Updating the backend image
does not update the frontend Template.

These commands do not clear EMS write holds or enable physical device execution.
For a first installation or a different project, follow the deployment manual.
EOF
