"""`ledger-mix`: a small read/write ledger workload (added 2026-10-14).

Written as the "verification on something new" for the Linux profile: it is not
one of the repo's workloads (pgbench -S, the range count, the soak's four
statements). A ledger of accounts and append-only entries, driven as a fixed
60/20/10/10 mix of four operations:

| op               | kind   | statement                                            |
|------------------|--------|------------------------------------------------------|
| `balance_read`   | read   | one account's balance by primary key                 |
| `entry_append`   | write  | append one entry for the account                     |
| `balance_adjust` | write  | add 1 to the account's balance, stamp `updated_at`   |
| `entries_count`  | read   | count the account's entries through the index        |

The mix is the ops tuple itself -- a worker issues `ops[index % 10]` -- so it is
exact per worker, not sampled. That makes the write path checkable after the
run: the engine must hold exactly as many entries as `entry_append` operations
were issued, and the balances must have moved by exactly the number of
`balance_adjust` operations (when no operation failed). `expected_writes` is
that arithmetic.

Data is index-derived (`blake2b(seed|table|row|column)`), not a PRNG stream,
per the invariant.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import blake2b

NAME = "ledger-mix"
SEED = 20261014
ACCOUNTS_PER_SCALE = 100_000

OPS: tuple[str, ...] = (
    "balance_read",
    "entry_append",
    "balance_read",
    "balance_adjust",
    "balance_read",
    "entries_count",
    "balance_read",
    "entry_append",
    "balance_read",
    "balance_read",
)

STATEMENTS: tuple[tuple[str, str], ...] = (
    ("balance_read", "SELECT balance FROM ledger_accounts WHERE id = $1"),
    (
        "entry_append",
        "INSERT INTO ledger_entries (account_id, amount) VALUES ($1, ($1 % 97) - 48)",
    ),
    (
        "balance_adjust",
        "UPDATE ledger_accounts SET balance = balance + 1, updated_at = now() WHERE id = $1",
    ),
    ("entries_count", "SELECT count(*) FROM ledger_entries WHERE account_id = $1"),
)

DDL = """
CREATE TABLE ledger_accounts (
    id integer PRIMARY KEY,
    balance bigint NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE ledger_entries (
    id bigserial PRIMARY KEY,
    account_id integer NOT NULL REFERENCES ledger_accounts (id),
    amount integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ledger_entries_account ON ledger_entries (account_id);
"""


def opening_balance(account_id: int, seed: int = SEED) -> int:
    """Index-derived: the same row gets the same balance in any order, any process."""
    digest = blake2b(
        f"{seed}|ledger_accounts|{account_id}|balance".encode(),
        digest_size=8,
        usedforsecurity=False,
    ).digest()
    return int.from_bytes(digest, "big") % 1_000_000


def accounts_copy_text(scale: int = 1, seed: int = SEED) -> str:
    """`COPY ledger_accounts (id, balance) FROM STDIN` payload, tab-separated."""
    rows = scale * ACCOUNTS_PER_SCALE
    return "".join(f"{i}\t{opening_balance(i, seed)}\n" for i in range(1, rows + 1))


def opening_total(scale: int = 1, seed: int = SEED) -> int:
    return sum(opening_balance(i, seed) for i in range(1, scale * ACCOUNTS_PER_SCALE + 1))


def ops_issued(issued: int, op: str, ops: tuple[str, ...] = OPS) -> int:
    """How many of a worker's first `issued` operations were `op` (round-robin)."""
    per_cycle = ops.count(op)
    full, rest = divmod(issued, len(ops))
    return full * per_cycle + sum(1 for o in ops[:rest] if o == op)


@dataclass(frozen=True, slots=True)
class ExpectedWrites:
    entries: int
    balance_increment: int


def expected_writes(issued_per_worker: list[int]) -> ExpectedWrites:
    """What the engine must hold afterwards, if no operation failed."""
    return ExpectedWrites(
        entries=sum(ops_issued(n, "entry_append") for n in issued_per_worker),
        balance_increment=sum(ops_issued(n, "balance_adjust") for n in issued_per_worker),
    )
