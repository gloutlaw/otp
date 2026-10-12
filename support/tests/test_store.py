from support.store import CLOSED, DIRECT_SOURCE, OPEN, UNKNOWN_SOURCE, Store


def make_store(tmp_path) -> Store:
    return Store(tmp_path / "support.db")


def test_ticket_is_opened_and_closed(tmp_path) -> None:
    store = make_store(tmp_path)
    ticket = store.create_ticket(7, "replica", now=100)
    assert store.open_ticket_for(7).id == ticket.id
    closed = store.close_open_ticket(7, now=200)
    assert closed.status == CLOSED
    assert closed.closed_at == 200
    assert store.open_ticket_for(7) is None
    store.close()


def test_closed_ticket_is_followed_by_a_new_one(tmp_path) -> None:
    store = make_store(tmp_path)
    first = store.create_ticket(7, DIRECT_SOURCE, now=100)
    store.close_open_ticket(7, now=150)
    second = store.create_ticket(7, DIRECT_SOURCE, now=300)
    assert second.id != first.id
    assert store.open_ticket_for(7).id == second.id
    store.close()


def test_list_open_skips_closed_tickets(tmp_path) -> None:
    store = make_store(tmp_path)
    store.create_ticket(1, DIRECT_SOURCE, now=1)
    store.create_ticket(2, DIRECT_SOURCE, now=2)
    store.close_open_ticket(1, now=3)
    assert [t.user_id for t in store.list_open()] == [2]
    store.close()


def test_relay_routes_owner_message_to_ticket_user(tmp_path) -> None:
    store = make_store(tmp_path)
    ticket = store.create_ticket(7, DIRECT_SOURCE, now=1)
    store.add_relay(555, ticket.id, 7)
    assert store.route(555) == (ticket.id, 7)
    assert store.route(556) is None
    store.close()


def test_ban_and_unban(tmp_path) -> None:
    store = make_store(tmp_path)
    assert not store.is_banned(7)
    store.ban(7, now=1)
    assert store.is_banned(7)
    assert store.unban(7) is True
    assert not store.is_banned(7)
    assert store.unban(7) is False
    store.close()


def test_first_source_wins(tmp_path) -> None:
    store = make_store(tmp_path)
    store.remember_source(7, "replica", now=1)
    store.remember_source(7, DIRECT_SOURCE, now=2)
    assert store.source_of(7) == "replica"
    assert store.source_of(8) == UNKNOWN_SOURCE
    store.close()


def test_rate_limit_allows_limit_then_refuses(tmp_path) -> None:
    store = make_store(tmp_path)
    results = [store.take_rate_slot(7, now=100, limit=3, window_seconds=60) for _ in range(4)]
    assert results == [True, True, True, False]
    store.close()


def test_rate_limit_resets_after_the_window(tmp_path) -> None:
    store = make_store(tmp_path)
    for _ in range(3):
        store.take_rate_slot(7, now=100, limit=3, window_seconds=60)
    assert store.take_rate_slot(7, now=130, limit=3, window_seconds=60) is False
    assert store.take_rate_slot(7, now=161, limit=3, window_seconds=60) is True
    store.close()


def test_rate_limit_is_per_user(tmp_path) -> None:
    store = make_store(tmp_path)
    for _ in range(3):
        store.take_rate_slot(7, now=100, limit=3, window_seconds=60)
    assert store.take_rate_slot(8, now=100, limit=3, window_seconds=60) is True
    store.close()


def test_open_state_is_constant(tmp_path) -> None:
    store = make_store(tmp_path)
    ticket = store.create_ticket(7, DIRECT_SOURCE, now=1)
    assert ticket.status == OPEN
    store.close()
