"""Event bus: isolation, coalescing, gap-free seq."""

from __future__ import annotations

import asyncio

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.core.eventbus import COALESCE_TOPICS, InProcessEventBus


@pytest.mark.asyncio
async def test_slow_subscriber_does_not_block_fast() -> None:
    bus = InProcessEventBus(queue_size=8)
    fast_got: list[int] = []
    slow_got: list[int] = []

    async def fast() -> None:
        async for ev in bus.subscribe(["job.progress"]):
            fast_got.append(ev.seq)
            if len(fast_got) >= 20:
                return

    async def slow() -> None:
        async for ev in bus.subscribe(["job.progress"]):
            slow_got.append(ev.seq)
            await asyncio.sleep(0.05)  # deliberately slow
            if len(slow_got) >= 5:
                return

    t_fast = asyncio.create_task(fast())
    t_slow = asyncio.create_task(slow())
    await asyncio.sleep(0.02)

    for i in range(30):
        await bus.publish("job.progress", {"n": i})
        await asyncio.sleep(0)  # yield

    # Fast must finish promptly even while slow is lagging.
    await asyncio.wait_for(t_fast, timeout=2.0)
    assert len(fast_got) >= 20
    # Slow eventually completes without starving the publisher.
    await asyncio.wait_for(t_slow, timeout=5.0)


@pytest.mark.asyncio
async def test_coalescing_collapses_under_load() -> None:
    assert "coding.stream" in COALESCE_TOPICS
    bus = InProcessEventBus(queue_size=256, coalesce_hz=10)
    received: list[int] = []

    async def collector() -> None:
        async for ev in bus.subscribe(["coding.stream"]):
            received.append(ev.seq)

    task = asyncio.create_task(collector())
    await asyncio.sleep(0.02)
    published = 50
    for i in range(published):
        await bus.publish("coding.stream", {"chunk": i})
    # Allow coalesce interval (100ms) to flush the latest pending frame.
    await asyncio.sleep(0.25)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(received) < published
    assert len(received) >= 1


@pytest.mark.asyncio
async def test_seq_gap_free_per_topic() -> None:
    bus = InProcessEventBus()
    seqs_a: list[int] = []
    seqs_b: list[int] = []
    for _ in range(40):
        ea = await bus.publish("venture.created", {})
        seqs_a.append(ea.seq)
    for _ in range(15):
        eb = await bus.publish("agent.step", {"x": 1})
        seqs_b.append(eb.seq)
    assert seqs_a == list(range(1, 41))
    assert seqs_b == list(range(1, 16))
    # Topics are independent
    assert bus.topic_seq("venture.created") == 40
    assert bus.topic_seq("agent.step") == 15
