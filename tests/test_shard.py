import pytest

from tests import shard


def test_every_test_is_in_exactly_one_shard_and_the_split_is_even():
    ids = [f"tests/test_x.py::test_{i}" for i in range(3000)]
    counts = [sum(shard.shard_of(i, 3) == k for i in ids) for k in (1, 2, 3)]
    assert sum(counts) == 3000 and min(counts) > 900
    assert shard.shard_of(ids[0], 3) == shard.shard_of(ids[0], 3)   # the same every run, on every machine


@pytest.mark.parametrize("bad", ["2", "0/3", "4/3", "a/b", "1/2/3"])
def test_a_shard_that_makes_no_sense_is_refused(bad):
    with pytest.raises(pytest.UsageError):
        shard.split(bad)
    assert shard.split("2/3") == (2, 3)
