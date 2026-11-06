"""Setup outcomes that need operator input rather than an automatic retry."""

from physiclaw.core.calibration._common import PreconditionError


class ManualPositionRequired(PreconditionError):
    """The operator must position the stylus before arm calibration can run."""
