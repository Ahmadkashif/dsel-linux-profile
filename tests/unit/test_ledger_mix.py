"""The `ledger-mix` workload's definition and its write-path arithmetic."""

from __future__ import annotations

from dsel.driver.transport import PostgresFactory, PostgresTransport
from dsel.workloads import ledger_mix as lm


def test_the_mix_is_sixty_twenty_ten_ten() -> None:
    assert len(lm.OPS) == 10
    counts = {op: lm.OPS.count(op) for op in set(lm.OPS)}
    assert counts == {
        "balance_read": 6,
        "entry_append": 2,
        "balance_adjust": 1,
        "entries_count": 1,
    }


def test_every_op_has_a_statement_taking_one_parameter() -> None:
    statements = dict(lm.STATEMENTS)
    assert set(statements) == set(lm.OPS)
    for sql in statements.values():
        assert "$1" in sql and "$2" not in sql


def test_it_is_not_one_of_the_repos_workloads() -> None:
    from dsel.driver.calibrate import COUNT_A_RANGE, SELECT_ONE_ROW
    from dsel.driver.transport import PGBENCH_SELECT

    existing = {PGBENCH_SELECT, SELECT_ONE_ROW[1], COUNT_A_RANGE[1]}
    assert not existing & set(dict(lm.STATEMENTS).values())
    assert "pgbench" not in lm.DDL


def test_opening_balances_are_index_derived() -> None:
    assert lm.opening_balance(42) == lm.opening_balance(42)
    assert lm.opening_balance(42) != lm.opening_balance(43)
    text = lm.accounts_copy_text(scale=1)
    lines = text.splitlines()
    assert len(lines) == lm.ACCOUNTS_PER_SCALE
    assert lines[41] == f"42\t{lm.opening_balance(42)}"


def test_round_robin_counts_are_exact() -> None:
    assert lm.ops_issued(0, "entry_append") == 0
    assert lm.ops_issued(2, "entry_append") == 1  # index 1
    assert lm.ops_issued(10, "entry_append") == 2
    assert lm.ops_issued(23, "entry_append") == 5  # 4 + indices 21
    assert lm.ops_issued(23, "balance_adjust") == 2  # 2 + none of 20..22
    assert lm.ops_issued(24, "balance_adjust") == 3  # index 23
    expected = lm.expected_writes([23, 24])
    assert expected.entries == 10 and expected.balance_increment == 5


def test_the_transport_picks_the_statement_for_the_op() -> None:
    factory = PostgresFactory(dsn="postgresql://x", scale=1, statements=lm.STATEMENTS)
    transport = factory(object())
    assert isinstance(transport, PostgresTransport)
    assert dict(transport.statements)["entry_append"].startswith("INSERT")
    assert transport.rows == lm.ACCOUNTS_PER_SCALE
