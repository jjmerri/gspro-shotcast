"""A pytest plugin (pyproject's addopts: -p tests.shard): `--shard 2/3` runs the second third of the
tests. CI's Windows run, the slow one, is split this way into jobs that run side by side
(.github/workflows/tests.yml). Each test is in exactly one shard, by a hash of its id, so the split
stays even as tests are added and no list needs keeping."""
import zlib

import pytest


def pytest_addoption(parser):
    parser.addoption("--shard", default=None, metavar="I/N",
                     help="run only shard I of N (1-based): each test is in exactly one")


def split(value: str) -> tuple[int, int]:
    try:
        i, n = (int(x) for x in value.split("/"))
    except ValueError:
        raise pytest.UsageError(f"--shard {value}: use I/N, such as 2/3") from None
    if not 1 <= i <= n:
        raise pytest.UsageError(f"--shard {value}: I must be from 1 to N")
    return i, n


def shard_of(nodeid: str, n: int) -> int:
    return zlib.crc32(nodeid.encode()) % n + 1


def pytest_collection_modifyitems(config, items):
    value = config.getoption("--shard")
    if not value:
        return
    i, n = split(value)
    out = [it for it in items if shard_of(it.nodeid, n) != i]
    if out:
        config.hook.pytest_deselected(items=out)
        items[:] = [it for it in items if shard_of(it.nodeid, n) == i]
