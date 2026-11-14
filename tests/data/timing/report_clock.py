"""Child pytest session for the repository's clock/reporting compatibility."""

from datetime import datetime

import pytest
from freezegun import freeze_time


@pytest.fixture
def application_clock():
    with freeze_time("2000-01-01") as clock:
        yield clock


def test_application_clock_still_moves(application_clock):
    application_clock.move_to("2100-01-01")

    assert datetime.now() == datetime(2100, 1, 1)
