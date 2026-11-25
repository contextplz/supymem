from tests.company_os_harness import load_company_os_app

load_company_os_app(reset=False)

from app.signals.gate import CandidateClaim, Signal, evaluate


def _signals(n: int, *, collectors: int = 2, windows: int = 2, effect: float = 0.2) -> list[Signal]:
    return [
        Signal(
            id=str(index),
            person_key=str(index),
            collector_id=f"collector-{index % collectors}",
            effect=effect,
            window=f"day-{index % windows}",
        )
        for index in range(n)
    ]


def test_gate_fails_at_twenty_nine_distinct_people() -> None:
    result = evaluate(CandidateClaim("test"), _signals(29))
    assert result.passed is False
    assert result.have_n == 29
    assert result.need_n == 30


def test_gate_passes_at_thirty_people_with_all_conditions() -> None:
    result = evaluate(CandidateClaim("test"), _signals(30))
    assert result.passed is True


def test_gate_rejects_one_collector() -> None:
    result = evaluate(CandidateClaim("test"), _signals(30, collectors=1))
    assert result.passed is False
    assert "collectors" in result.reason


def test_gate_deduplicates_persons_and_requires_stable_effect() -> None:
    duplicate = [Signal(str(index), "same-person", "a" if index % 2 else "b", 0.3, "day-1") for index in range(30)]
    assert evaluate(CandidateClaim("test"), duplicate).have_n == 1
    result = evaluate(CandidateClaim("test"), _signals(30, windows=1))
    assert result.passed is False
    assert "windows" in result.reason
