import pytest

from ai_usage_monitor import sampler


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch):
    """Keep the sampler's wake-blip retry delay out of test wall-clock time.

    Tests that care about the delay override this by patching
    sampler.time.sleep themselves.
    """
    monkeypatch.setattr(sampler.time, "sleep", lambda seconds: None)
