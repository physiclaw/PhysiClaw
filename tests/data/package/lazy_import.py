"""Fresh-process probe: importing metadata must not load the hardware stack."""

import importlib.abc
import sys


class BlockHeavyImports(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "physiclaw.core" or fullname.split(".")[0] in {
            "cv2",
            "numpy",
            "serial",
            "onnxruntime",
        }:
            raise ImportError(f"heavy import attempted: {fullname}")
        return None


sys.meta_path.insert(0, BlockHeavyImports())
from physiclaw import __version__  # noqa: E402

print(__version__)
