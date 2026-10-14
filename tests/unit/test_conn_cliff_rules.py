"""The curve rules, on curves whose answers are known (PLAN.md S15-S18).

The live ramps exercise these against real engines, where the right answer is
whatever the engine does. Here the curves are constructed, so the rules can be
held to answers that are known in advance -- including the awkward ones: a
noisy plateau must not be read as a collapse, and a recovery must not be read
as one either.
"""

from __future__ import annotations

from dsel.phenomena.conn_cliff import (
    CONNECTION_AXIS,
    Curve,
    Point,
    collapse,
    knee,
    max_sustainable,
)


def point(x: float, achieved: float, p99: float = 1000.0, errors: int = 0) -> Point:
    return Point(
        x=x,
        achieved_rate_per_s=achieved,
        completed=10_000,
        errors=errors,
        p99_us=p99,
        offered_rate_per_s=achieved * 1.005,
    )


def test_a_sustained_fall_is_a_collapse() -> None:
    points = [point(8, 1000), point(16, 1010), point(32, 1005), point(64, 700), point(128, 500)]
    assert collapse(points) == 64


def test_a_single_dip_that_recovers_is_not() -> None:
    """Measured on a real connection ramp: 3513, 3479, 3279, 3370, 3449, 3441
    per second across 8 to 256 connections. The dip at 32 is 6.7% below the
    peak and the next three rungs contradict it. Reporting that as a collapse
    would have a reader cap a pool at a number that means nothing.
    """
    points = [
        point(8, 3513),
        point(16, 3479),
        point(32, 3279),
        point(64, 3370),
        point(128, 3449),
        point(256, 3441),
    ]
    assert collapse(points) is None


def test_errors_fire_immediately_without_waiting_to_be_sustained() -> None:
    """Errors are not noise. A rung that started failing has answered the
    question, and requiring it to keep failing would delay the answer past the
    point where it mattered."""
    points = [point(8, 1000), point(16, 990, errors=500), point(32, 1000)]
    assert collapse(points) == 16


def test_the_knee_is_the_first_doubling_of_the_baseline() -> None:
    points = [point(8, 1000, p99=1000), point(16, 1000, p99=1900), point(32, 1000, p99=2100)]
    assert knee(points) == 32


def test_a_flat_curve_has_no_knee_and_no_collapse() -> None:
    """The rules must be able to say "not reached". One that always finds a
    landmark is not a measurement."""
    points = [point(x, 1000) for x in (8, 16, 32, 64)]
    assert knee(points) is None
    assert collapse(points) is None
    assert max_sustainable(points) == 64


def test_the_landmarks_are_on_whichever_axis_was_ramped() -> None:
    """`x` is the connection count on a connection ramp, and the rules return
    a point on that axis rather than a rate they never saw vary."""
    curve = Curve(
        points=(point(8, 1000), point(64, 1010), point(256, 400)),
        axis=CONNECTION_AXIS,
    )
    assert curve.collapse_rate_per_s == 256
    assert curve.landmarks()["axis"] == CONNECTION_AXIS


def test_the_rung_order_is_shuffled_and_reproducible() -> None:
    """Ascending order makes rung position a proxy for elapsed time, and on
    this host elapsed time is a proxy for temperature. Measured: five
    consecutive ascending ramps reported a 15% fall from 8 connections to 32,
    and a sixth -- after forty minutes of continuous load -- came out flat at
    a level 15% below all of them. The fall was the machine warming up.
    """
    from pathlib import Path

    from dsel.driver.connections import ConnectionRampPlan

    counts = (8, 16, 32, 64, 128, 256)

    def plan(repeat: int, shuffle: bool = True) -> ConnectionRampPlan:
        return ConnectionRampPlan(
            run_dir=Path("/tmp"),
            dsn="postgresql://x",
            connection_counts=counts,
            rate_per_s=100.0,
            repeat=repeat,
            shuffle_rungs=shuffle,
        )

    orders = [plan(repeat).rung_order() for repeat in (1, 2, 3)]
    assert all(sorted(order) == list(range(len(counts))) for order in orders), (
        "every rung must still run exactly once"
    )
    assert len({tuple(order) for orders_ in [orders] for order in orders_}) == 3, (
        "each repeat must take a different order, or drift aligns with the axis "
        "in every one of them the same way"
    )
    assert any(order != list(range(len(counts))) for order in orders)
    # Reproducible from the seed, so the order is in the bundle by construction.
    assert plan(2).rung_order() == plan(2).rung_order()
    # And the failure mode is still reachable deliberately.
    assert plan(1, shuffle=False).rung_order() == list(range(len(counts)))


def test_a_multi_op_cell_re_derives_the_sum_of_its_ops_not_their_mean() -> None:
    """One writer, one-second windows, four ops at 30/10/5/5 per second: the
    cell ran at 50/s. Adding up every record's width counted each second four
    times and re-derived 12.5/s (found 2026-10-14 with `ledger-mix`)."""
    from dsel.live.schema import LatencyWindowRecord
    from dsel.phenomena.conn_cliff import summarise_cells

    cell = "ledger/postgres/ledger-mix/r50/rep1/step0"
    records = []
    seq = 0
    for second in range(10):
        for op, count in (("read", 30), ("append", 10), ("adjust", 5), ("count", 5)):
            records.append(
                LatencyWindowRecord(
                    t_ms=1_000 * (second + 1), w="driver-0", seq=seq, cell=cell,
                    window_ms=1000, op=op, count=count, rate_per_s=float(count), p99_us=900.0,
                )
            )  # fmt: skip
            seq += 1
    (summary,) = summarise_cells(records)
    assert summary.completed == 500
    assert summary.achieved_rate_per_s == 50.0
