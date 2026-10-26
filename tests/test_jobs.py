"""DynamoDB value handling: floats go in as Decimals, numbers come back plain."""

from lib import jobs


def test_floats_are_stored_as_decimals_and_read_back_as_numbers():
    from decimal import Decimal

    stored = jobs.ddb_safe({"a": 0.25, "b": [1, 0.5, {"c": 1.5}], "d": "x", "e": 3})
    assert stored["a"] == Decimal("0.25") and isinstance(stored["b"][2]["c"], Decimal)
    assert stored["e"] == 3 and isinstance(stored["e"], int)
    back = jobs.plain(stored)
    assert back == {"a": 0.25, "b": [1, 0.5, {"c": 1.5}], "d": "x", "e": 3}
    assert isinstance(back["e"], int) and isinstance(back["a"], float)


def test_the_fake_table_refuses_a_float_like_the_real_service(monkeypatch):
    import pytest

    from tests.fakes import FakeTable

    table = FakeTable()
    with pytest.raises(TypeError, match="Float types"):
        table.update_item(Key={"pk": "JOB#x"}, UpdateExpression="SET m = :v",
                          ExpressionAttributeValues={":v": {"ratio": 0.5}})
