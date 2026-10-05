"""When the user stops and starts using the computer, from the Wayland compositor.

A minimal client of the wire protocol for ext-idle-notify-v1's input idle notification: only
real input (keyboard, mouse, touch) counts, so a playing video doesn't look like the user.
"""

import os
import struct
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path

import anyio
from anyio.abc import ByteStream

# Object ids this client allocates; the display is always 1.
DISPLAY = 1
REGISTRY = 2
SYNC_CALLBACK = 3
SEAT = 4
NOTIFIER = 5
NOTIFICATION = 6

# Requests.
DISPLAY_SYNC = 0
DISPLAY_GET_REGISTRY = 1
REGISTRY_BIND = 0
NOTIFIER_GET_INPUT_IDLE_NOTIFICATION = 2

# Events.
DISPLAY_ERROR = 0
REGISTRY_GLOBAL = 0
CALLBACK_DONE = 0
NOTIFICATION_IDLED = 0
NOTIFICATION_RESUMED = 1

SEAT_INTERFACE = "wl_seat"
NOTIFIER_INTERFACE = "ext_idle_notifier_v1"
NOTIFIER_VERSION = 2  # The first version with input idle notifications.

HEADER = struct.Struct("=II")  # Object id, then size << 16 | opcode, in the host's byte order.
UINT = struct.Struct("=I")


class WaylandError(Exception):
    pass


def socket_path() -> Path:
    display = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
    if os.path.isabs(display):
        return Path(display)
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        raise WaylandError("XDG_RUNTIME_DIR isn't set, so there's no Wayland session to watch.")
    return Path(runtime_dir) / display


async def idle_changes(timeout_ms: int, path: Path | None = None) -> AsyncGenerator[bool]:
    """False once watching starts, then True after `timeout_ms` without input and False again
    at the next input. Raises WaylandError, or OSError, when the compositor can't be reached."""
    stream = await anyio.connect_unix(path or socket_path())
    async with stream:
        connection = _Connection(stream)
        await connection.send(DISPLAY, DISPLAY_GET_REGISTRY, _uint(REGISTRY))
        await connection.send(DISPLAY, DISPLAY_SYNC, _uint(SYNC_CALLBACK))
        globals_: dict[str, tuple[int, int]] = {}
        async for object_id, opcode, payload in connection.events():
            if object_id == REGISTRY and opcode == REGISTRY_GLOBAL:
                name, interface, version = _parse_global(payload)
                globals_.setdefault(interface, (name, version))
            elif object_id == SYNC_CALLBACK and opcode == CALLBACK_DONE:
                break
        else:
            raise WaylandError("The compositor closed the connection before listing its globals.")
        seat = globals_.get(SEAT_INTERFACE)
        notifier = globals_.get(NOTIFIER_INTERFACE)
        if seat is None:
            raise WaylandError("The compositor has no seat, so no keyboard or mouse to watch.")
        if notifier is None or notifier[1] < NOTIFIER_VERSION:
            raise WaylandError(
                f"The compositor doesn't offer {NOTIFIER_INTERFACE} version {NOTIFIER_VERSION}, "
                "which reports input idle time."
            )
        await connection.send(REGISTRY, REGISTRY_BIND, _bind(seat[0], SEAT_INTERFACE, 1, SEAT))
        await connection.send(
            REGISTRY,
            REGISTRY_BIND,
            _bind(notifier[0], NOTIFIER_INTERFACE, NOTIFIER_VERSION, NOTIFIER),
        )
        await connection.send(
            NOTIFIER,
            NOTIFIER_GET_INPUT_IDLE_NOTIFICATION,
            _uint(NOTIFICATION) + _uint(timeout_ms) + _uint(SEAT),
        )
        yield False
        async for object_id, opcode, _ in connection.events():
            if object_id == NOTIFICATION and opcode == NOTIFICATION_IDLED:
                yield True
            elif object_id == NOTIFICATION and opcode == NOTIFICATION_RESUMED:
                yield False
    raise WaylandError("The compositor closed the connection.")


class _Connection:
    def __init__(self, stream: ByteStream):
        self._stream = stream
        self._buffer = b""

    async def send(self, object_id: int, opcode: int, payload: bytes = b"") -> None:
        size = HEADER.size + len(payload)
        await self._stream.send(HEADER.pack(object_id, size << 16 | opcode) + payload)

    async def events(self) -> AsyncIterator[tuple[int, int, bytes]]:
        """Every event until the compositor hangs up; a protocol error raises."""
        while True:
            while len(self._buffer) >= HEADER.size:
                object_id, size_and_opcode = HEADER.unpack_from(self._buffer)
                size, opcode = size_and_opcode >> 16, size_and_opcode & 0xFFFF
                if size < HEADER.size:
                    raise WaylandError(f"The compositor sent a malformed message ({size} bytes).")
                if len(self._buffer) < size:
                    break
                payload = self._buffer[HEADER.size : size]
                self._buffer = self._buffer[size:]
                if object_id == DISPLAY and opcode == DISPLAY_ERROR:
                    raise WaylandError(_describe_error(payload))
                yield object_id, opcode, payload
            try:
                self._buffer += await self._stream.receive()
            except (anyio.EndOfStream, anyio.BrokenResourceError):
                return


def _uint(value: int) -> bytes:
    return UINT.pack(value)


def _string(text: str) -> bytes:
    """Length (counting the NUL), then the text and NUL, padded to 32 bits."""
    data = text.encode() + b"\0"
    return _uint(len(data)) + data + b"\0" * (-len(data) % 4)


def _bind(name: int, interface: str, version: int, new_id: int) -> bytes:
    """wl_registry.bind's new_id has no fixed interface, so the interface and version come
    with it."""
    return _uint(name) + _string(interface) + _uint(version) + _uint(new_id)


def _read_uint(payload: bytes, offset: int) -> int:
    if offset + UINT.size > len(payload):
        raise WaylandError("The compositor sent a message too short for its arguments.")
    value: int = UINT.unpack_from(payload, offset)[0]
    return value


def _read_string(payload: bytes, offset: int) -> tuple[str, int]:
    length = _read_uint(payload, offset)
    start = offset + UINT.size
    if length == 0 or start + length > len(payload):
        raise WaylandError("The compositor sent a malformed string.")
    text = payload[start : start + length - 1].decode(errors="replace")
    return text, start + length + (-length % 4)


def _parse_global(payload: bytes) -> tuple[int, str, int]:
    name = _read_uint(payload, 0)
    interface, offset = _read_string(payload, UINT.size)
    return name, interface, _read_uint(payload, offset)


def _describe_error(payload: bytes) -> str:
    object_id, code = _read_uint(payload, 0), _read_uint(payload, UINT.size)
    message, _ = _read_string(payload, 2 * UINT.size)
    return f"Wayland protocol error {code} on object {object_id}: {message}"
