"""A thumbnail of the screen, for hue-mcp's light shows: only its colors, 30 times a second.

Run with the system Python, which has PyGObject and GStreamer's PipeWire plugin:

    /usr/bin/python3 screen_capture.py <restore-token file>

It asks for the screen through the desktop's ScreenCast portal. The first time, the desktop asks
the user which screen to share; the portal's restore token, saved in the given file, lets it
skip the question afterwards. Frames go to stdout: see Capture.stream. It never imports
hue_mcp, and it must keep working on the system Python, outside any virtual environment.
"""

import json
import os
import secrets
import sys
import tempfile
from typing import Any

# Shared with hue_mcp.screen, and kept equal by a test: this script can't import the package.
WIDTH, HEIGHT = 64, 20  # Rows of 192 bytes: no padding.
EXIT_CANCELLED = 3  # The user declined to share the screen.
EXIT_FAILED = 4
EXIT_CLOSED = 5  # The desktop ended the screencast, or the stream ended.
EXIT_MISSING = 6  # PyGObject or GStreamer's PipeWire plugin isn't installed.

try:
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gio, GLib, Gst
except (ImportError, ValueError):
    sys.exit(EXIT_MISSING)

PORTAL = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SOURCE_MONITOR = 1
CURSOR_HIDDEN = 1
PERSIST_UNTIL_REVOKED = 2


class Capture:
    def __init__(self, token_path: str):
        self.token_path = token_path
        self.loop = GLib.MainLoop()
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.portal = Gio.DBusProxy.new_sync(
            self.bus,
            Gio.DBusProxyFlags.NONE,
            None,
            PORTAL,
            PORTAL_PATH,
            "org.freedesktop.portal.ScreenCast",
            None,
        )
        self.sender = self.bus.get_unique_name()[1:].replace(".", "_")
        self.exit_code = 0

    def request(
        self, method: str, arguments: list[tuple[str, Any]], options: dict[str, Any]
    ) -> dict[str, Any]:
        """Call a portal method and wait for its Request's Response signal, subscribed to
        before the call so it can't be missed."""
        token = f"huemcp{secrets.token_hex(8)}"
        path = f"{PORTAL_PATH}/request/{self.sender}/{token}"
        answer: dict[str, Any] = {}

        def on_response(*signal: Any) -> None:
            parameters = signal[5]
            answer["code"], answer["results"] = parameters.unpack()
            self.loop.quit()

        subscription = self.bus.signal_subscribe(
            PORTAL,
            "org.freedesktop.portal.Request",
            "Response",
            path,
            None,
            Gio.DBusSignalFlags.NONE,
            on_response,
            None,
        )
        options = {**options, "handle_token": GLib.Variant("s", token)}
        signature = "(" + "".join(kind for kind, _ in arguments) + "a{sv})"
        values = (*(value for _, value in arguments), options)
        self.portal.call_sync(method, GLib.Variant(signature, values), 0, -1, None)
        self.loop.run()
        self.bus.signal_unsubscribe(subscription)
        if answer.get("code") == 1:
            sys.exit(EXIT_CANCELLED)
        if answer.get("code") != 0:
            sys.exit(EXIT_FAILED)
        results: dict[str, Any] = answer["results"]
        return results

    def open(self) -> tuple[int, list[tuple[int, dict[str, Any]]]]:
        """The PipeWire fd, and each shared monitor's node and properties."""
        session_token = GLib.Variant("s", f"huemcp{secrets.token_hex(8)}")
        session = self.request("CreateSession", [], {"session_handle_token": session_token})
        handle = session["session_handle"]
        self.bus.signal_subscribe(
            PORTAL,
            "org.freedesktop.portal.Session",
            "Closed",
            handle,
            None,
            Gio.DBusSignalFlags.NONE,
            lambda *_: self.stop(EXIT_CLOSED),
            None,
        )
        options = {
            "types": GLib.Variant("u", SOURCE_MONITOR),
            "multiple": GLib.Variant("b", True),  # Lights can follow different monitors.
            "cursor_mode": GLib.Variant("u", CURSOR_HIDDEN),
            "persist_mode": GLib.Variant("u", PERSIST_UNTIL_REVOKED),
        }
        if token := self.saved_token():
            options["restore_token"] = GLib.Variant("s", token)
        self.request("SelectSources", [("o", handle)], options)
        started = self.request("Start", [("o", handle), ("s", "")], {})
        if "restore_token" in started:
            self.save_token(started["restore_token"])  # Each token works only once.
        streams = [(int(node), dict(properties)) for node, properties in started["streams"]]
        result, fds = self.portal.call_with_unix_fd_list_sync(
            "OpenPipeWireRemote",
            GLib.Variant("(oa{sv})", (handle, {})),
            0,
            -1,
            None,
            None,
        )
        fd: int = fds.get(result.unpack()[0])
        return fd, streams

    def saved_token(self) -> str | None:
        try:
            with open(self.token_path) as file:
                return file.read().strip() or None
        except OSError:
            return None

    def save_token(self, token: str) -> None:
        directory = os.path.dirname(self.token_path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=directory)
        with os.fdopen(fd, "w") as file:
            file.write(token)
        os.replace(temporary, self.token_path)

    def stream(self, fd: int, streams: list[tuple[int, dict[str, Any]]]) -> None:
        """A header line naming each monitor's position and size, then frames, each led by
        its monitor's index in that list."""
        Gst.init(None)
        if Gst.ElementFactory.find("pipewiresrc") is None:
            sys.exit(EXIT_MISSING)
        screens = [
            [*properties.get("position", (None, None)), *properties.get("size", (None, None))]
            for _, properties in streams
        ]
        monitors, workspace = monitor_layout()
        header = {"screens": screens, "monitors": monitors, "workspace": workspace}
        sys.stdout.buffer.write(json.dumps(header).encode() + b"\n")
        sys.stdout.buffer.flush()
        pipelines = []
        for index, (node, _) in enumerate(streams):
            # path takes the portal's node id; target-object wants a serial or name, and
            # waits forever for a node id.
            pipeline = Gst.parse_launch(
                f"pipewiresrc fd={fd} path={node} always-copy=true "
                "! video/x-raw,max-framerate=30/1 ! videoconvertscale "
                f"! video/x-raw,format=RGB,width={WIDTH},height={HEIGHT} "
                "! appsink name=sink emit-signals=true drop=true max-buffers=1 sync=false"
            )
            pipeline.get_by_name("sink").connect("new-sample", self.on_sample, index)
            bus = pipeline.get_bus()
            bus.add_signal_watch()
            bus.connect("message::error", lambda *_: self.stop(EXIT_FAILED))
            bus.connect("message::eos", lambda *_: self.stop(EXIT_CLOSED))
            pipeline.set_state(Gst.State.PLAYING)
            pipelines.append(pipeline)
        self.loop.run()
        for pipeline in pipelines:
            pipeline.set_state(Gst.State.NULL)

    def on_sample(self, sink: Any, index: int) -> Any:
        sample = sink.emit("pull-sample")
        buffer = sample.get_buffer()
        ok, info = buffer.map(Gst.MapFlags.READ)
        if ok:
            try:
                frame = bytes(info.data)
                if len(frame) >= WIDTH * HEIGHT * 3:  # Rows can be padded; ours aren't.
                    sys.stdout.buffer.write(bytes([index]) + frame[: WIDTH * HEIGHT * 3])
                    sys.stdout.buffer.flush()
            except BrokenPipeError:
                self.stop(0)
            finally:
                buffer.unmap(info)
        return Gst.FlowReturn.OK

    def stop(self, code: int) -> None:
        self.exit_code = code
        self.loop.quit()


def monitor_layout() -> tuple[list[list[float]], list[int] | None]:
    """Each monitor's box as fractions (left, top, right, bottom) of the whole workspace, so a
    full-workspace share can be cut into monitors, and the workspace's size; empty when GDK
    can't tell."""
    try:
        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk

        Gdk.init([])
        display = Gdk.Display.get_default()
        boxes = [display.get_monitor(i).get_geometry() for i in range(display.get_n_monitors())]
    except (ValueError, ImportError, AttributeError, GLib.Error):
        return [], None
    if not boxes:
        return [], None
    left, top = min(b.x for b in boxes), min(b.y for b in boxes)
    width = max(b.x + b.width for b in boxes) - left
    height = max(b.y + b.height for b in boxes) - top
    fractions = [
        [
            (b.x - left) / width,
            (b.y - top) / height,
            (b.x + b.width - left) / width,
            (b.y + b.height - top) / height,
        ]
        for b in boxes
    ]
    return fractions, [width, height]


def main() -> None:
    capture = Capture(sys.argv[1])
    try:
        fd, streams = capture.open()
    except GLib.Error:
        sys.exit(EXIT_FAILED)
    capture.stream(fd, streams)
    sys.exit(capture.exit_code)


if __name__ == "__main__":
    main()
