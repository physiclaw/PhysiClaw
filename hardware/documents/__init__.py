"""Bilingual document builders — the assembly manual, the sourcing guide and
the extrusion tech drawing.

Standard-library only (no build123d). The manual consumes assembly SVGs;
the sourcing guide adds supplier data to the shared purchasing list;
the drawings join that list to the model's dimensions and profile constants.
The public CLI is ``python -m hardware manual|sourcing|drawing``.
"""


class BuildError(Exception):
    """A user-facing build failure: ``main()`` prints the message on its own,
    without a Python traceback."""
