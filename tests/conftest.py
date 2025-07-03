# ──────────────────────────────────────────────────────────────────────────
# tests/conftest.py
# --------------------------------------------------------------------------
"""
Global pytest fixtures & CLI options for Metahash integration tests.
Run, e.g.:

    pytest -m integration tests/test_rewards_integration.py \
           --network finney --netuid 11
"""
import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    """Expose `--network` and `--netuid` on the pytest command line."""
    parser.addoption(
        "--network",
        action="store",
        default=None,
        help="Subtensor network name, e.g. `finney`, `local`, or `test`",
    )
    parser.addoption(
        "--netuid",
        action="store",
        default=1,
        type=int,
        help="UID of the subnet to interrogate (defaults to 1).",
    )
