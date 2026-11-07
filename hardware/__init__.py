"""Repository hardware toolchain; independent of the installed application."""

import importlib.machinery as _machinery
import py_compile as _py_compile
import sys as _sys

# The bootstrap loads before our finder; protect its next import with a hash.
if not _sys.dont_write_bytecode:
    try:
        _py_compile.compile(
            __file__, invalidation_mode=_py_compile.PycInvalidationMode.CHECKED_HASH
        )
    except OSError:
        pass  # Read-only checkouts can still execute their source modules.


class _SourceLoader(_machinery.SourceFileLoader):
    def get_code(self, fullname):
        # Execute the source bytes that CAD cache keys hash, ignoring stale .pyc.
        path = self.get_filename(fullname)
        return self.source_to_code(self.get_data(path), path)


class _SourceFinder:
    @staticmethod
    def find_spec(fullname, path=None, target=None):
        if not fullname.startswith("hardware."):
            return None
        spec = _machinery.PathFinder.find_spec(fullname, path, target)
        if spec is not None and isinstance(spec.loader, _machinery.SourceFileLoader):
            spec.loader = _SourceLoader(fullname, spec.loader.get_filename(fullname))
        return spec


# Install before hardware helpers load; third-party bytecode caches stay intact.
_sys.meta_path.insert(0, _SourceFinder())

from hardware.port.desktop import prepare_fonts as _prepare_fonts  # noqa: E402

_prepare_fonts()  # Must precede any build123d/OCCT import.
