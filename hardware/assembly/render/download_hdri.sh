#!/usr/bin/env bash
# Fetch the studio HDRI used by render_tapz_20.py. Idempotent: skips
# the download if the file already exists at the expected size.
#
# Source: Poly Haven, https://polyhaven.com/a/studio_small_09 (CC0)
# Destination: hardware/output/render/ (alongside the rendered PNG/GLB)

set -euo pipefail

# Resolve script location → project root → output/render
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd "${here}/../../.." && pwd)"
out_dir="${project_root}/hardware/output/render"
url="https://dl.polyhaven.org/file/ph-assets/HDRIs/hdr/2k/studio_small_09_2k.hdr"
dest="${out_dir}/studio_small_09_2k.hdr"
expected_size_min=6000000   # full file is ~6.3 MB; refetch if smaller

mkdir -p "${out_dir}"

if [[ -f "${dest}" ]]; then
    actual_size=$(stat -f%z "${dest}" 2>/dev/null || stat -c%s "${dest}")
    if (( actual_size >= expected_size_min )); then
        echo "HDRI already present at ${dest} (${actual_size} bytes); skipping."
        exit 0
    fi
    echo "Existing HDRI is incomplete (${actual_size} bytes); refetching."
    rm -f "${dest}"
fi

echo "Downloading ${url} → ${dest}"
curl -L --retry 3 --retry-delay 2 --fail -o "${dest}" "${url}"

actual_size=$(stat -f%z "${dest}" 2>/dev/null || stat -c%s "${dest}")
if (( actual_size < expected_size_min )); then
    echo "Download finished but file size ${actual_size} < expected ${expected_size_min}" >&2
    exit 1
fi
echo "HDRI ready (${actual_size} bytes)."
