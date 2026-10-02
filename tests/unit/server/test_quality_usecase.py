"""QualityUseCase 的窗口差分测试。"""

from wind_hub_server.application.usecase.quality import QualityUseCase


def test_quality_delta_never_negative() -> None:
    class C:
        points_total = 10
        points_bad = 2
        acquisition_runs = 10
        acquisition_failures = 1
        missed_cycles = 3
        poll_overruns = 1
        connect_failures = 2
        reconnects = 1

    class S:
        counters = C()
        points_dropped = 4

    first = S()
    last = S()
    last.counters = type(
        "C2",
        (),
        {
            "points_total": 8,
            "points_bad": 1,
            "acquisition_runs": 8,
            "acquisition_failures": 0,
            "missed_cycles": 1,
            "poll_overruns": 0,
            "connect_failures": 1,
            "reconnects": 0,
        },
    )()
    last.points_dropped = 2

    delta = QualityUseCase._delta(first, last)  # type: ignore[arg-type]
    assert all(value >= 0 for value in delta.values())
