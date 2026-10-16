"""Run a delegated CLI module with clean cancellation during its imports."""

import runpy
import sys


def main() -> int:
    module, *args = sys.argv[1:]
    sys.argv = [module, *args]
    try:
        runpy.run_module(module, run_name="__main__", alter_sys=True)
    except KeyboardInterrupt:
        print("CANCELLED", file=sys.stderr, flush=True)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
