# Firmware

Build the FluidNC bundle used by `physiclaw flash` for the MKS DLC32 V2.1 board.
`firmware.py` combines upstream release binaries with the adjacent `config.yaml`.
For installation, see the [setup guide](../docs/set-up/firmware.mdx)
([中文](../docs/set-up/firmware.zh.mdx)).

## Build

From the repository root, with uv and Python 3.12 or newer:

```bash
make firmware
make firmware ARGS="--help"
make firmware ARGS="--tag v4.1.0"
```

`DEFAULT_TAG` in `firmware.py` selects the default FluidNC release. The standalone
script declares its image-building dependencies through PEP 723; uv installs
them without requiring the PhysiClaw package. To run without Make or choose a
custom destination:

```bash
uv run firmware/firmware.py --out /tmp/fluidnc.zip
```

## Output and cache

Paths are relative to this script, regardless of the working directory:

```text
firmware/output/
├── fluidnc.zip          # completed flash bundle
└── .cache/             # preserved between builds
    ├── .build.lock     # prevents overlapping builds; unlocked automatically on exit
    └── v4.0.3/         # release metadata and downloaded ZIP/ELF
```

Every build clears generated files from `output/`, **preserving `.cache/`**.
Images use a fresh temporary directory under `output/`; only the seven named
entries below enter the ZIP. A failed build leaves no old managed bundle.
`--help` changes nothing. A custom output outside `output/` is replaced only
when the new ZIP is complete; its packaging temporary file sits beside it for
atomic replacement.

Online builds check current GitHub release metadata and verify cached assets
against GitHub's SHA-256 and size. Corrupt, partial, or replaced assets are
redownloaded. Releases without checksums are downloaded fresh. Temporary
failures retry up to eight times, resuming only the current download. Missing
releases or assets fail immediately with a link to available releases.

For an offline build, supply both inputs from the same release. Keep them
outside the cleaned output folder or inside `.cache/`:

```bash
uv run firmware/firmware.py --tag v4.0.3 \
  --posix-zip /path/to/fluidnc-v4.0.3-posix.zip \
  --elf /path/to/esp32-noradio-firmware.elf
```

Successful cached builds print five lines: release and cleanup status, two
verified inputs, the bundle path/size/file count, and its SHA-256. Downloads and
failures add details as needed; image-converter diagnostics appear on failure.
Colors are disabled for redirected output and when `NO_COLOR` is set.

## Bundle contents

| File | Source | Flash offset |
| --- | --- | --- |
| `bootloader.bin` | `wifi/bootloader.bin` in the upstream POSIX ZIP | `0x1000` |
| `partitions.bin` | `wifi/partitions.bin` in the same ZIP | `0x8000` |
| `boot_app0.bin` | `common/boot_app0.bin` in the same ZIP | `0xe000` |
| `firmware.bin` | Converted from `esp32-noradio-firmware.elf` | `0x10000` |
| `littlefs.bin` | Generated filesystem containing only `/config.yaml` | `0x3d0000` |
| `config.yaml` | This directory's machine configuration | Reference only |
| `fluidnc_version.txt` | FluidNC release tag followed by a newline | Reference only |

The POSIX ZIP lacks the `noradio` variant, so the builder converts its ELF with
ESP32, DIO flash mode, 80 MHz flash frequency, and 4 MB flash size. LittleFS uses
4096-byte blocks and the library's default disk version. Its size comes from
the partition table; a moved offset or invalid size stops the build.

`main()` parses options, locks and cleans the workspace, resolves release
assets, then calls `build_bundle()`. That function accepts local inputs and
never downloads. Image tools are imported lazily, so imports and `--help` need
only the standard library. ZIP entry order, timestamps, and permissions are
fixed for reproducible packaging.

## Change and verify

Keep config comments on their own lines: FluidNC's YAML subset does not support
inline comments. Update the YAML examples in both setup guides when changing
`config.yaml`. Keep flash offsets aligned with
[`physiclaw.cli.flash.FLASH_LAYOUT`](../src/physiclaw/cli/flash.py).

```bash
uv run pytest tests/firmware tests/cli/test_flash.py tests/test_site_contract.py
uv run ruff check firmware/ tests/firmware/
uv run ruff format --check firmware/ tests/firmware/
```

Tests cover cache integrity, cleanup, concurrency, download failures, image
layout, deterministic packaging, and config consistency without flashing a
board. Site contract checks require a sibling `PhysiClaw-site` checkout.

## Publish

After building and validating:

```bash
gh release upload firmware_fluidNC firmware/output/fluidnc.zip \
  --clobber -R physiclaw/PhysiClaw
```

Then redeploy `PhysiClaw-site`. Its `scripts/fetch-downloads.mjs` copies the asset
to [the firmware download URL](https://physiclaw.ai/downloads/firmware/fluidnc.zip)
used by `physiclaw flash`. The URL stays stable; `fluidnc_version.txt` records
the bundled version.
