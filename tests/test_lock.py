import pytest

from agent.lock import AlreadyRunning, SingleInstance


def test_second_instance_is_refused(tmp_path):
    p = tmp_path / "agent.lock"
    with SingleInstance(p):
        with pytest.raises(AlreadyRunning):
            with SingleInstance(p):
                pass
    # released after exit
    with SingleInstance(p):
        pass
