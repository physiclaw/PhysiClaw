# Third-Party Notices

PhysiClaw is [MIT](LICENSE)-licensed. It redistributes two components under
their own copyleft terms (full texts in [`licenses/`](licenses/)):

| Component | Version | License | Copyright | Source |
|---|---|---|---|---|
| **FluidNC** firmware (arm controller) | v4.0.3 `noradio` | [GPL-3.0](licenses/GPL-3.0.txt) | © the FluidNC project | <https://github.com/bdring/FluidNC> |
| **OmniParser `icon_detect`** model (icon detection) | v2.0 | [AGPL-3.0](licenses/AGPL-3.0.txt) | © Microsoft; YOLOv8 © Ultralytics | <https://huggingface.co/microsoft/OmniParser-v2.0> |

Both are redistributed **unmodified**: FluidNC as the upstream flash images
(`physiclaw flash`), `icon_detect` as the same weights converted `.pt`→`.onnx`
(`physiclaw setup local-vision-model`). Corresponding source is the upstream
release above — for FluidNC v4.0.3, our flash recipe is in
`src/physiclaw/cli/flash.py`; PhysiClaw's own source is public, satisfying
AGPL-3.0 §13. We do **not** use OmniParser's separate (MIT) `icon_caption` model.
