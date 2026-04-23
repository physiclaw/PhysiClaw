---
name: setup
description: Connect the robotic arm and camera, then calibrate. Required before using any PhysiClaw MCP tools.
allowed-tools: Bash
---

# Setup

```bash
physiclaw setup hardware            # interactive, default
physiclaw setup hardware -y         # auto mode, skip prompts
physiclaw setup hardware --trace    # add edge-trace visual check at end
```

Fails with non-zero exit and prints which step failed. Fix the physical setup and rerun.
