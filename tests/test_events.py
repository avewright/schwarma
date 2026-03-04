"""Tests for the EventBus — subscribe, publish, unsubscribe, publish_and_wait."""

import asyncio
from uuid import uuid4

import pytest

from schwarma.events import Event, EventBus, EventKind


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _make_event(kind: EventKind = EventKind.PROBLEM_POSTED, **kw) -> Event:
    return Event(kind=kind, **kw)


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------

class TestSubscribeAndPublish:
    async def test_handler_receives_event(self):
        bus = EventBus()
        received = []

        async def handler(event: Event):
            received.append(event)

        bus.subscribe(EventKind.PROBLEM_POSTED, handler)
        evt = _make_event()
        await bus.publish(evt)

        assert len(received) == 1
        assert received[0] is evt

    async def test_only_matching_kind_fires(self):
        bus = EventBus()
        received = []

        async def handler(event: Event):
            received.append(event)

        bus.subscribe(EventKind.PROBLEM_POSTED, handler)
        await bus.publish(_make_event(EventKind.SOLUTION_SUBMITTED))

        assert received == []

    async def test_multiple_handlers(self):
        bus = EventBus()
        a_received, b_received = [], []

        async def handler_a(event):
            a_received.append(event)

        async def handler_b(event):
            b_received.append(event)

        bus.subscribe(EventKind.REVIEW_SUBMITTED, handler_a)
        bus.subscribe(EventKind.REVIEW_SUBMITTED, handler_b)

        await bus.publish(_make_event(EventKind.REVIEW_SUBMITTED))

        assert len(a_received) == 1
        assert len(b_received) == 1


class TestSubscribeAll:
    async def test_global_handler_gets_everything(self):
        bus = EventBus()
        received = []

        async def handler(event):
            received.append(event.kind)

        bus.subscribe_all(handler)

        await bus.publish(_make_event(EventKind.PROBLEM_POSTED))
        await bus.publish(_make_event(EventKind.SOLUTION_SUBMITTED))
        await bus.publish(_make_event(EventKind.SWAP_COMPLETED))

        assert received == [
            EventKind.PROBLEM_POSTED,
            EventKind.SOLUTION_SUBMITTED,
            EventKind.SWAP_COMPLETED,
        ]


class TestUnsubscribe:
    async def test_unsubscribed_handler_stops_receiving(self):
        bus = EventBus()
        received = []

        async def handler(event):
            received.append(event)

        bus.subscribe(EventKind.PROBLEM_POSTED, handler)
        await bus.publish(_make_event())
        assert len(received) == 1

        bus.unsubscribe(EventKind.PROBLEM_POSTED, handler)
        await bus.publish(_make_event())
        assert len(received) == 1  # no new events

    async def test_unsubscribe_nonexistent_is_safe(self):
        bus = EventBus()

        async def handler(event):
            pass

        # Should not raise
        bus.unsubscribe(EventKind.PROBLEM_POSTED, handler)


class TestPublishAndWait:
    async def test_gathers_results(self):
        bus = EventBus()

        async def handler(event):
            return f"handled-{event.kind.name}"

        bus.subscribe(EventKind.PROBLEM_POSTED, handler)
        results = await bus.publish_and_wait(_make_event())

        assert results == ["handled-PROBLEM_POSTED"]


class TestHandlerError:
    async def test_failing_handler_does_not_crash_bus(self):
        bus = EventBus()
        received = []

        async def bad_handler(event):
            raise RuntimeError("boom")

        async def good_handler(event):
            received.append(event)

        bus.subscribe(EventKind.PROBLEM_POSTED, bad_handler)
        bus.subscribe(EventKind.PROBLEM_POSTED, good_handler)

        # Should not raise — bad handler is caught internally
        await bus.publish(_make_event())

        assert len(received) == 1


class TestEventFields:
    def test_event_str(self):
        evt = Event(
            kind=EventKind.PROBLEM_POSTED,
            problem_id=uuid4(),
            source_agent_id=uuid4(),
        )
        s = str(evt)
        assert "PROBLEM_POSTED" in s

    def test_event_is_frozen(self):
        evt = _make_event()
        with pytest.raises(AttributeError):
            evt.kind = EventKind.SWAP_COMPLETED  # type: ignore[misc]


class TestFilteredSubscriptions:
    """Tests for subscribe_filtered."""

    @pytest.mark.asyncio
    async def test_filtered_handler_receives_matching_events(self):
        bus = EventBus()
        received = []

        async def handler(event: Event):
            received.append(event)

        # Only listen to problem-related events
        bus.subscribe_filtered(
            lambda e: e.problem_id is not None,
            handler,
        )

        pid = uuid4()
        await bus.publish(Event(kind=EventKind.PROBLEM_POSTED, problem_id=pid))
        await bus.publish(Event(kind=EventKind.AGENT_REGISTERED))  # no problem_id

        assert len(received) == 1
        assert received[0].problem_id == pid

    @pytest.mark.asyncio
    async def test_filtered_handler_skips_non_matching(self):
        bus = EventBus()
        received = []

        async def handler(event: Event):
            received.append(event)

        # Only listen to solution events
        bus.subscribe_filtered(
            lambda e: e.kind in (EventKind.SOLUTION_SUBMITTED, EventKind.SOLUTION_ACCEPTED),
            handler,
        )

        await bus.publish(Event(kind=EventKind.PROBLEM_POSTED))
        await bus.publish(Event(kind=EventKind.REVIEW_SUBMITTED))
        assert len(received) == 0

        await bus.publish(Event(kind=EventKind.SOLUTION_SUBMITTED))
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_unsubscribe_filtered(self):
        bus = EventBus()
        received = []

        async def handler(event: Event):
            received.append(event)

        bus.subscribe_filtered(lambda _: True, handler)
        await bus.publish(_make_event())
        assert len(received) == 1

        bus.unsubscribe_filtered(handler)
        await bus.publish(_make_event())
        assert len(received) == 1  # no new delivery

    @pytest.mark.asyncio
    async def test_agent_specific_subscription(self):
        bus = EventBus()
        agent_id = uuid4()
        received = []

        async def handler(event: Event):
            received.append(event)

        # Subscribe only to events targeting this agent
        bus.subscribe_filtered(
            lambda e: e.target_agent_id == agent_id,
            handler,
        )

        await bus.publish(Event(kind=EventKind.TRIAGE_ASSIGNED, target_agent_id=uuid4()))
        assert len(received) == 0

        await bus.publish(Event(kind=EventKind.TRIAGE_ASSIGNED, target_agent_id=agent_id))
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_filtered_works_with_publish_and_wait(self):
        bus = EventBus()
        received = []

        async def handler(event: Event):
            received.append(event)

        bus.subscribe_filtered(lambda _: True, handler)
        results = await bus.publish_and_wait(_make_event())
        assert len(received) == 1
        assert len(results) == 1


class TestEventSerialization:
    """Tests for Event.to_dict()."""

    def test_to_dict_basic(self):
        e = Event(kind=EventKind.PROBLEM_POSTED, source_agent_id=uuid4())
        d = e.to_dict()
        assert d["kind"] == "PROBLEM_POSTED"
        assert isinstance(d["timestamp"], str)

    def test_to_dict_none_fields(self):
        e = Event(kind=EventKind.AGENT_REGISTERED)
        d = e.to_dict()
        assert d["problem_id"] is None
        assert d["source_agent_id"] is None


class TestEventRecording:
    """Tests for EventBus recording / replay capabilities."""

    async def test_recording_disabled_by_default(self):
        bus = EventBus()
        await bus.publish(_make_event())
        assert bus.recorded_events == []

    async def test_enable_recording_captures_events(self):
        bus = EventBus()
        bus.enable_recording()
        e1 = _make_event(EventKind.PROBLEM_POSTED)
        e2 = _make_event(EventKind.SOLUTION_SUBMITTED)
        await bus.publish(e1)
        await bus.publish(e2)
        assert len(bus.recorded_events) == 2
        assert bus.recorded_events[0] is e1
        assert bus.recorded_events[1] is e2

    async def test_recording_via_publish_and_wait(self):
        bus = EventBus()
        bus.enable_recording()
        e = _make_event()
        await bus.publish_and_wait(e)
        assert len(bus.recorded_events) == 1
        assert bus.recorded_events[0] is e

    async def test_disable_recording_stops_capture(self):
        bus = EventBus()
        bus.enable_recording()
        await bus.publish(_make_event())
        assert len(bus.recorded_events) == 1

        bus.enable_recording(enabled=False)
        await bus.publish(_make_event())
        assert len(bus.recorded_events) == 1  # no new capture

    async def test_clear_recording(self):
        bus = EventBus()
        bus.enable_recording()
        await bus.publish(_make_event())
        assert len(bus.recorded_events) == 1
        bus.clear_recording()
        assert bus.recorded_events == []

    async def test_recorded_events_returns_copy(self):
        bus = EventBus()
        bus.enable_recording()
        await bus.publish(_make_event())
        snapshot = bus.recorded_events
        await bus.publish(_make_event())
        # snapshot should not grow
        assert len(snapshot) == 1
        assert len(bus.recorded_events) == 2


class TestEventReplay:
    """Tests for EventBus.replay()."""

    async def test_replay_recorded_events(self):
        bus = EventBus()
        received: list[Event] = []

        async def handler(event: Event):
            received.append(event)

        bus.subscribe_all(handler)
        bus.enable_recording()

        e1 = _make_event(EventKind.PROBLEM_POSTED)
        e2 = _make_event(EventKind.SOLUTION_SUBMITTED)
        await bus.publish(e1)
        await bus.publish(e2)

        received.clear()
        count = await bus.replay()
        assert count == 2
        assert received[0] is e1
        assert received[1] is e2

    async def test_replay_with_filter_kinds(self):
        bus = EventBus()
        received: list[Event] = []

        async def handler(event: Event):
            received.append(event)

        bus.subscribe_all(handler)
        bus.enable_recording()

        await bus.publish(_make_event(EventKind.PROBLEM_POSTED))
        await bus.publish(_make_event(EventKind.SOLUTION_SUBMITTED))
        await bus.publish(_make_event(EventKind.REVIEW_SUBMITTED))

        received.clear()
        count = await bus.replay(filter_kinds={EventKind.SOLUTION_SUBMITTED})
        assert count == 1
        assert received[0].kind == EventKind.SOLUTION_SUBMITTED

    async def test_replay_explicit_event_list(self):
        bus = EventBus()
        received: list[Event] = []

        async def handler(event: Event):
            received.append(event)

        bus.subscribe_all(handler)

        custom = [
            _make_event(EventKind.AGENT_REGISTERED),
            _make_event(EventKind.SWAP_COMPLETED),
        ]
        count = await bus.replay(custom)
        assert count == 2
        assert received[0].kind == EventKind.AGENT_REGISTERED
        assert received[1].kind == EventKind.SWAP_COMPLETED

    async def test_replay_empty_returns_zero(self):
        bus = EventBus()
        count = await bus.replay()
        assert count == 0
