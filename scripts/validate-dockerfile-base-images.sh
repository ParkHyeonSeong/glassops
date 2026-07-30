#!/bin/sh
set -eu

repo_root=${1:-.}

extract_pinned_ref() {
  image=$1
  dockerfile=$2

  awk -v image="$image" '
    $1 == "FROM" && ($2 == image || index($2, image "@") == 1) {
      count += 1
      ref = $2
    }
    END {
      prefix = image "@sha256:"
      digest = substr(ref, length(prefix) + 1)
      if (count != 1 ||
          substr(ref, 1, length(prefix)) != prefix ||
          length(digest) != 64 ||
          digest !~ /^[0-9a-f]+$/) {
        exit 1
      }
      print ref
    }
  ' "$dockerfile"
}

validate_lockstep() {
  image=$1
  shift
  expected_ref=
  expected_path=

  for relative_path in "$@"; do
    dockerfile="$repo_root/$relative_path"
    if [ ! -f "$dockerfile" ]; then
      printf '%s\n' "$relative_path does not exist" >&2
      return 1
    fi

    if ! ref=$(extract_pinned_ref "$image" "$dockerfile"); then
      printf '%s\n' \
        "$relative_path must contain exactly one pinned FROM $image@sha256:<64 hex> reference" \
        >&2
      return 1
    fi

    if [ -z "$expected_ref" ]; then
      expected_ref=$ref
      expected_path=$relative_path
    elif [ "$ref" != "$expected_ref" ]; then
      printf '%s\n' \
        "$image digest mismatch: $relative_path uses $ref; $expected_path uses $expected_ref" \
        >&2
      return 1
    fi
  done

  printf '%s\n' "$image references are pinned and in lockstep: $expected_ref"
}

validate_lockstep \
  "node:22-alpine" \
  "Dockerfile" \
  "frontend/Dockerfile" \
  "frontend/Dockerfile.dev"

validate_lockstep \
  "python:3.12-slim" \
  "Dockerfile" \
  "backend/Dockerfile" \
  "agent/Dockerfile"
