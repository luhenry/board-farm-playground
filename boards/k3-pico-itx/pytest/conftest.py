import pathlib

import pytest


def pytest_configure(config):
    if not config.getoption("--lg-env", default=None):
        env = (pathlib.Path(__file__).parent.parent / "client.yaml").resolve()
        config.option.lg_env = str(env)


@pytest.fixture(scope="module")
def emmc(strategy, target):
    try:
        strategy.transition("emmc")
    except Exception as e:
        import traceback
        traceback.print_exc()
        pytest.exit(f"Transition to emmc shell failed: {e}", returncode=3)
    return target.get_driver("ShellDriver")
