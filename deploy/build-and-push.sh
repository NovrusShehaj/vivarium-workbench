#!/usr/bin/env bash
#
# Build + push the vivarium-workbench server image.
#
# The cluster nodes are x86_64, so this ALWAYS builds linux/amd64. Since #932
# the image contains only the workbench and its own locked dependencies -- no
# workspace package, no science stack -- so a cross-build from an ARM Mac works
# fine (~2 min, ~744 MB). It previously could not: the build imported v2ecoli ->
# polars, which needs AVX/AVX2/FMA/BMI, and QEMU does not emulate those, so
# every Apple Silicon build died with "Illegal instruction".
#
# Usage:
#   deploy/build-and-push.sh [version] [org]
#     version  image tag (default: short git sha)
#     org      ghcr org   (default: vivarium-collective)
#
# Optional:
#   PBG_PTOOLS_REF   git ref for the pbg-ptools Omics-Viewer plugin (default: main)
#
# Requires: docker buildx and a ghcr login (`docker login ghcr.io`) to push.
#
# NO LONGER REQUIRED (#932): WORKSPACE_IMAGE and an ECR login. The image no
# longer copies a venv out of a per-commit sms-ecoli/v2ecoli image, so nothing
# is pulled from GovCloud ECR. The workspace supplies the science environment at
# RUNTIME via <workspace>/.venv, which is where EnvironmentResolver already
# sends every env worker. If you have WORKSPACE_IMAGE exported from an older
# workflow, it is simply ignored.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${1:-$(git -C "${ROOT_DIR}" rev-parse --short HEAD)}"
ORG="${2:-vivarium-collective}"
IMAGE="ghcr.io/${ORG}/vivarium-workbench:${VERSION}"

# Image provenance (#1114), part 2: a semver-shaped VERSION run from a laptop
# is refused unless it could only have come from deploy/bump-and-release.sh --
# clean tree, on main, and a v<version> tag already pointing at HEAD. This is
# what makes a hand-built semver image actually IMPOSSIBLE rather than merely
# traceable via the -dirty label below: the CI gate in build-and-push.yml only
# binds `gh workflow run`, not this script run directly, and a bare-string
# VERSION run by hand from a laptop is exactly how the 9 unrecoverable images
# in #1114 were published. A non-semver VERSION (the short-sha default, or any
# other ad-hoc debug tag) is unaffected -- it was always self-describing.
if [[ "${VERSION}" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]]; then
  fail() { echo "refusing: $*" >&2; exit 1; }
  if [[ -n "$(git -C "${ROOT_DIR}" status --porcelain)" ]]; then
    fail "'${VERSION}' is semver but the tree is not clean -- use deploy/bump-and-release.sh, or pass a non-semver tag for an ad-hoc build"
  fi
  if [[ "$(git -C "${ROOT_DIR}" rev-parse --abbrev-ref HEAD)" != "main" ]]; then
    fail "'${VERSION}' is semver but HEAD is not on main -- use deploy/bump-and-release.sh, or pass a non-semver tag for an ad-hoc build"
  fi
  TAG_SHA="$(git -C "${ROOT_DIR}" rev-list -n1 "v${VERSION}" 2>/dev/null || true)"
  if [[ -z "${TAG_SHA}" || "${TAG_SHA}" != "$(git -C "${ROOT_DIR}" rev-parse HEAD)" ]]; then
    fail "'${VERSION}' is semver but tag v${VERSION} doesn't exist or doesn't point at HEAD -- use deploy/bump-and-release.sh, which creates it before building"
  fi
fi

# Image provenance (#1114), part 1: stamp the exact commit + build time into the image
# itself (OCI labels + /app/BUILD_INFO.json), so a published tag is no longer
# the only record of what it was built from. `-dirty` matters: a hand-build
# from an uncommitted tree should say so in its own label, since that is
# exactly how past unrecoverable images came to exist.
VCS_REF="$(git -C "${ROOT_DIR}" rev-parse HEAD)"
git -C "${ROOT_DIR}" diff --quiet HEAD || VCS_REF="${VCS_REF}-dirty"
BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# ${arr[@]+"${arr[@]}"} below (not bare "${arr[@]}") guards against `set -u`'s
# unbound-variable check on bash < 4.4 (this Mac's stock /bin/bash is 3.2.57)
# if BUILD_ARGS is ever empty again in the future -- kept defensive even though
# the three provenance args below now always populate it.
BUILD_ARGS=(
  --build-arg "VERSION=${VERSION}"
  --build-arg "VCS_REF=${VCS_REF}"
  --build-arg "BUILD_DATE=${BUILD_DATE}"
)
if [[ -n "${PBG_PTOOLS_REF:-}" ]]; then
  BUILD_ARGS+=(--build-arg "PBG_PTOOLS_REF=${PBG_PTOOLS_REF}")
fi

echo "building + pushing ${IMAGE} (linux/amd64)"
docker buildx build \
  --platform=linux/amd64 \
  -f "${ROOT_DIR}/Dockerfile" \
  -t "${IMAGE}" \
  "${BUILD_ARGS[@]+"${BUILD_ARGS[@]}"}" \
  "${ROOT_DIR}" \
  --push

echo "pushed ${IMAGE}"
echo "pin it in deploy/kustomize/overlays/<env>/kustomization.yaml (images: newTag)"
