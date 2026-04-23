"""PhysiClaw — gives AI agents a physical finger to operate any phone."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("physiclaw")
except PackageNotFoundError:  # running from a source tree without install
    __version__ = "0.0.0+local"

__all__ = ["PhysiClaw", "__version__"]


def __getattr__(name: str):
    # Defer the heavy import chain (cv2, numpy, pyserial, onnxruntime)
    # until someone actually touches the class. Lets `physiclaw --help`
    # and metadata-only imports stay cheap.
    if name == "PhysiClaw":
        from physiclaw.core import PhysiClaw as _P

        return _P
    raise AttributeError(f"module 'physiclaw' has no attribute {name!r}")
