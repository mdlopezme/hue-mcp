import struct
from pathlib import Path

import anyio
import pytest
from anyio.abc import SocketStream

from hue_mcp import wayland_idle
from hue_mcp.wayland_idle import WaylandError, idle_changes

pytestmark = pytest.mark.anyio


def string(text: str) -> bytes:
    data = text.encode() + b"\0"
    return struct.pack("=I", len(data)) + data + b"\0" * (-len(data) % 4)


def message(object_id: int, opcode: int, payload: bytes = b"") -> bytes:
    return struct.pack("=II", object_id, (8 + len(payload)) << 16 | opcode) + payload


def advertise(name: int, interface: str, version: int) -> bytes:
    payload = struct.pack("=I", name) + string(interface) + struct.pack("=I", version)
    return message(2, 0, payload)


DONE = message(3, 0, struct.pack("=I", 0))
GLOBALS = (
    advertise(1, "wl_compositor", 6)
    + advertise(7, "wl_seat", 9)
    + advertise(9, "ext_idle_notifier_v1", 2)
    + DONE
)
IDLED, RESUMED = message(6, 0), message(6, 1)


def parse(data: bytes) -> list[tuple[int, int, bytes]]:
    messages = []
    while data:
        object_id, word = struct.unpack_from("=II", data)
        size = word >> 16
        messages.append((object_id, word & 0xFFFF, data[8:size]))
        data = data[size:]
    return messages


class Compositor:
    """Plays a script: wait for some requests, send bytes, and so on; then hang up."""

    def __init__(self, tmp_path: Path, script: list[int | bytes]):
        self.path = tmp_path / "wayland-test"
        self.script = script
        self.received = b""

    async def serve(self, stream: SocketStream) -> None:
        async with stream:
            for step in self.script:
                if isinstance(step, bytes):
                    await stream.send(step)
                    continue
                while len(parse_complete(self.received)) < step:
                    self.received += await stream.receive()

    async def run(self, timeout_ms: int = 5000) -> tuple[list[bool], WaylandError]:
        """The changes reported, and the error that ended them: they always end in one."""
        changes: list[bool] = []
        listener = await anyio.create_unix_listener(self.path)
        async with listener, anyio.create_task_group() as tg:
            tg.start_soon(listener.serve, self.serve)
            try:
                async for idle in idle_changes(timeout_ms, self.path):
                    changes.append(idle)
            except WaylandError as error:
                ended = error
            tg.cancel_scope.cancel()
        return changes, ended


def parse_complete(data: bytes) -> list[tuple[int, int, bytes]]:
    complete = b""
    while len(data) >= 8:
        size = struct.unpack_from("=II", data)[1] >> 16
        if len(data) < size:
            break
        complete, data = complete + data[:size], data[size:]
    return parse(complete)


async def test_it_binds_the_seat_and_the_notifier_and_reports_idle_and_back(tmp_path):
    compositor = Compositor(tmp_path, [2, GLOBALS, 5, IDLED, RESUMED])
    changes, error = await compositor.run(timeout_ms=5000)
    assert changes == [False, True, False]
    assert "closed the connection" in str(error)
    requests = parse(compositor.received)
    assert requests == [
        (1, 1, struct.pack("=I", 2)),  # wl_display.get_registry
        (1, 0, struct.pack("=I", 3)),  # wl_display.sync
        (2, 0, struct.pack("=I", 7) + string("wl_seat") + struct.pack("=II", 1, 4)),
        (2, 0, struct.pack("=I", 9) + string("ext_idle_notifier_v1") + struct.pack("=II", 2, 5)),
        (5, 2, struct.pack("=III", 6, 5000, 4)),  # get_input_idle_notification
    ]


async def test_changes_start_active_then_follow_the_compositor(tmp_path):
    changes, _ = await Compositor(tmp_path, [2, GLOBALS, 5, IDLED, RESUMED, IDLED]).run()
    assert changes == [False, True, False, True]


async def test_events_split_across_reads_are_put_back_together(tmp_path):
    trickle: list[int | bytes] = [
        2,
        *(bytes([b]) for b in GLOBALS),
        5,
        *(bytes([b]) for b in IDLED),
    ]
    changes, _ = await Compositor(tmp_path, trickle).run()
    assert changes == [False, True]


async def test_other_events_are_skipped(tmp_path):
    seat_capabilities = message(4, 0, struct.pack("=I", 3))
    changes, _ = await Compositor(tmp_path, [2, GLOBALS, 5, seat_capabilities, IDLED]).run()
    assert changes == [False, True]


@pytest.mark.parametrize(
    ("globals_", "error"),
    [
        (advertise(7, "wl_seat", 9) + DONE, "doesn't offer ext_idle_notifier_v1 version 2"),
        (
            advertise(7, "wl_seat", 9) + advertise(9, "ext_idle_notifier_v1", 1) + DONE,
            "doesn't offer ext_idle_notifier_v1 version 2",
        ),
        (advertise(9, "ext_idle_notifier_v1", 2) + DONE, "no seat"),
        (advertise(7, "wl_seat", 9), "before listing its globals"),
    ],
)
async def test_a_compositor_without_what_it_needs_is_refused(tmp_path, globals_, error):
    changes, ended = await Compositor(tmp_path, [2, globals_]).run()
    assert changes == []
    assert error in str(ended)


async def test_a_protocol_error_is_raised_with_its_message(tmp_path):
    error = message(1, 0, struct.pack("=II", 5, 1) + string("invalid timeout"))
    _, ended = await Compositor(tmp_path, [2, GLOBALS, 5, error]).run()
    assert "error 1 on object 5: invalid timeout" in str(ended)


@pytest.mark.parametrize(
    "garbage",
    [
        struct.pack("=II", 2, 4 << 16),  # A size smaller than the header.
        message(2, 0, struct.pack("=I", 1)),  # A global with no interface.
        message(2, 0, struct.pack("=II", 1, 0)),  # An empty string.
        message(2, 0, struct.pack("=I", 1) + string("wl_seat")),  # No version.
    ],
)
async def test_malformed_messages_are_refused(tmp_path, garbage):
    _, ended = await Compositor(tmp_path, [2, garbage]).run()
    assert "malformed" in str(ended) or "too short" in str(ended)


def test_the_socket_is_found_from_the_session(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    assert wayland_idle.socket_path() == Path("/run/user/1000/wayland-1")
    monkeypatch.setenv("WAYLAND_DISPLAY", "/run/user/1000/elsewhere")
    assert wayland_idle.socket_path() == Path("/run/user/1000/elsewhere")


def test_without_a_session_it_says_so(monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    with pytest.raises(WaylandError, match="XDG_RUNTIME_DIR"):
        wayland_idle.socket_path()
