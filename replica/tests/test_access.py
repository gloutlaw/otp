from replica.access import owner_of, parse_user_ids


def test_parse_keeps_order_and_drops_duplicates() -> None:
    assert parse_user_ids(" 30, 10 ,30,, 20 ") == (30, 10, 20)


def test_empty_list_has_no_owner() -> None:
    assert parse_user_ids("") == ()
    assert owner_of(()) is None


def test_owner_is_the_first_listed_id() -> None:
    assert owner_of((10, 20)) == 10
