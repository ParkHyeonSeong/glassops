#!/bin/sh
set -eu

repo_root=${1:-.}
repo_root=$(cd "$repo_root" && pwd)

find_managed_dockerfiles() {
  find "$repo_root" \
    \( -type d \( -name .git -o -name node_modules -o -name .venv \) \
      -o -path "$repo_root/deploy/contracts" \) -prune -o \
    -type f \( -name Dockerfile -o -name 'Dockerfile.*' \) -print | LC_ALL=C sort
}

validate_external_froms() {
  dockerfile=$1
  relative_path=$2

  awk -v relative_path="$relative_path" '
    function is_pinned(ref, marker, digest) {
      marker = index(ref, "@sha256:")
      if (marker <= 1) {
        return 0
      }

      digest = substr(ref, marker + length("@sha256:"))
      return length(digest) == 64 && digest ~ /^[0-9a-f]+$/
    }

    function reject_unpinned(ref) {
      printf "%s: unpinned external FROM %s\\n", relative_path, ref
      invalid = 1
    }

    function process_instruction(instruction, count, i, ref, lower_ref) {
      sub(/^[[:space:]]+/, "", instruction)
      if (instruction == "" || instruction ~ /^#/) {
        return
      }

      count = split(instruction, fields, /[[:space:]]+/)
      if (toupper(fields[1]) != "FROM") {
        return
      }

      i = 2
      while (i <= count && fields[i] ~ /^--/) {
        i += 1
      }

      ref = fields[i]
      if (ref == "") {
        printf "%s: invalid FROM instruction\\n", relative_path
        invalid = 1
        return
      }

      lower_ref = tolower(ref)
      if (lower_ref != "scratch" && !(lower_ref in aliases) && !is_pinned(ref)) {
        reject_unpinned(ref)
      }

      if (toupper(fields[i + 1]) == "AS") {
        if (fields[i + 2] == "") {
          printf "%s: invalid FROM instruction\\n", relative_path
          invalid = 1
        } else {
          aliases[tolower(fields[i + 2])] = 1
        }
      }
    }

    {
      line = $0
      sub(/\\r$/, "", line)
      if (continued) {
        instruction = instruction " " line
      } else {
        instruction = line
      }

      if (instruction ~ /\\[[:space:]]*$/) {
        sub(/\\[[:space:]]*$/, "", instruction)
        continued = 1
        next
      }

      process_instruction(instruction)
      continued = 0
      instruction = ""
    }

    END {
      if (continued) {
        process_instruction(instruction)
      }
      exit invalid ? 1 : 0
    }
  ' "$dockerfile" >&2
}

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

find_managed_dockerfiles | while IFS= read -r dockerfile; do
  relative_path=${dockerfile#"$repo_root"/}
  validate_external_froms "$dockerfile" "$relative_path"
done

printf '%s\n' "all external Dockerfile base images are digest-pinned"
