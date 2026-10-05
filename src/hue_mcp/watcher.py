"""The pomodoro watcher: a background service that runs adaptive pomodoros.

It follows the user's activity through the Wayland compositor and switches the room's looks
through the bridge. The MCP server talks to it over a Unix socket, one JSON line each way.
"""

import fcntl
import json
import logging
import os
import subprocess
import time
from collections.abc import Awaitable, Callable
from contextlib import aclosing, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import anyio
from anyio.abc import ByteStream, TaskGroup
from anyio.streams.buffered import BufferedByteReceiveStream

from hue_mcp import looks
from hue_mcp.bridge import HueBridge
from hue_mcp.config import atomic_write_json, load_location, state_dir
from hue_mcp.daylight import Band, band_at
from hue_mcp.errors import HueError
from hue_mcp.home import Group, Home, Light, Scene
from hue_mcp.session import (
    AWAY_LIMIT_S,
    IDLE_TIMEOUT_S,
    ActivityTracker,
    Durations,
    Ended,
    Nudge,
    PhaseChanged,
    RoundDone,
    Session,
    is_positive_number,
)
from hue_mcp.wayland_idle import WaylandError, idle_changes

logger = logging.getLogger(__name__)

TICK_S = 1.0
SLEEP_SEEN_S = 1.0  # More time asleep than this since the last tick: the computer was suspended.
RESUME_HOLD_S = 15.0  # After a sleep, wait for activity and the network before acting.
SWITCH_FADE_MS = 3000
VERIFY_AFTER_S = 6.0  # After a switch: its fade, plus a margin for the bridge to report.
RETRY_AFTER_S = 5.0
QUIET_AFTER_CHANGE_S = 60.0  # Time for Claude to save a change before a nudge restores the look.
SAVE_EVERY_S = 60.0  # So a restart can tell how recent a saved session is.
RESTORE_FADE_MS = 400
PULSE_HOLD_S = looks.PULSE_FADE_MS / 1000 + 0.5  # The fade to the nudge, and a moment there.
DIP_HOLD_S = looks.DIP_HOLD_S
RECONNECT_FIRST_S = 1.0
RECONNECT_MAX_S = 60.0
OFF_FADE_MS = 10_000
SETTLE_TRIES = 5  # Reads, SETTLE_INTERVAL_S apart, to wait out a fade before saving a look.
SETTLE_INTERVAL_S = 1.0
REQUEST_LIMIT_BYTES = 64 * 1024
CLIENT_TIMEOUT_S = 60.0  # Starting a session may create nine scenes.
NOTIFY_TIMEOUT_S = 5.0
MOST_MINUTES = {"focus_minutes": 120, "short_break_minutes": 60, "long_break_minutes": 120}
MOST_ROUNDS = 8
MOST_DAYS = 366

NOT_RUNNING = (
    "The pomodoro watcher isn't running. Ask the user to run `hue-mcp install-watcher` in a "
    "terminal; it starts the watcher now and at every login."
)

Notify = Callable[[str, str], Awaitable[None]]
_Resend = tuple[Light, dict[str, Any]]  # A light that missed a command, and what to resend it.


class WatcherNotRunning(HueError):
    pass


def boot_clock() -> float:
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def time_asleep() -> float:
    """Grows only while the computer is suspended: the boot clock counts that time, the
    monotonic clock doesn't."""
    return time.clock_gettime(time.CLOCK_BOOTTIME) - time.clock_gettime(time.CLOCK_MONOTONIC)


def runtime_dir() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        raise HueError("XDG_RUNTIME_DIR isn't set, so the watcher has nowhere to listen.")
    return Path(runtime) / "hue-mcp"


def socket_path() -> Path:
    return runtime_dir() / "watcher.sock"


async def ask_watcher(
    command: str, *, timeout_s: float = CLIENT_TIMEOUT_S, **arguments: Any
) -> dict[str, Any]:
    """Send one request to the watcher and return its result; its refusals raise HueError."""
    try:
        with anyio.fail_after(timeout_s):
            stream = await anyio.connect_unix(socket_path())
            async with stream:
                request = {"command": command, **arguments}
                await stream.send(json.dumps(request).encode() + b"\n")
                reply = await BufferedByteReceiveStream(stream).receive_until(
                    b"\n", REQUEST_LIMIT_BYTES
                )
    except (FileNotFoundError, ConnectionRefusedError) as error:
        raise WatcherNotRunning(NOT_RUNNING) from error
    except TimeoutError as error:
        raise HueError("The pomodoro watcher didn't answer in time.") from error
    except (OSError, anyio.BrokenResourceError, anyio.EndOfStream, anyio.IncompleteRead) as error:
        raise HueError(f"Couldn't talk to the pomodoro watcher: {error!r}") from error
    try:
        answer = json.loads(reply)
        if not answer["ok"]:
            raise HueError(str(answer["error"]))
        result: dict[str, Any] = answer["result"]
    except (ValueError, KeyError, TypeError) as error:
        raise HueError(f"The pomodoro watcher answered nonsense: {error}") from error
    return result


class Watcher:
    def __init__(
        self,
        get_bridge: Callable[[], HueBridge],
        clock: Callable[[], float] = boot_clock,
        wall_clock: Callable[[], float] = time.time,
        notify: Notify | None = None,
        asleep: Callable[[], float] = time_asleep,
    ):
        self._get_bridge = get_bridge
        self._clock = clock
        self._wall_clock = wall_clock
        self._notify = notify or notify_desktop
        self._asleep = asleep
        self.tracker = ActivityTracker()
        self.session: Session | None = None
        self._lock = anyio.Lock()  # Serializes ticks and requests, which change the session.
        self._room_lock = anyio.Lock()  # Serializes writes to the room, pulses included.
        self._pulse: anyio.CancelScope | None = None
        self._pulse_done = anyio.Event()
        self._pulse_done.set()
        self._pulse_left_look = False  # A pulse was sent, and its way back to the look wasn't.
        self._outside_change = False
        self._switch_due: float | None = None
        self._verify_due: float | None = None
        self._previous_look: str | None = None  # Lights still showing it missed a switch.
        self._changed_by_claude: set[str] = set()  # Lights to leave be until the next switch.
        self._quiet_until = 0.0
        self._hold_until = 0.0
        self._last_asleep: float | None = None
        self._saved_at = 0.0

    async def run(self) -> None:
        directory = runtime_dir()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        with (directory / "watcher.lock").open("w") as lock_file:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise HueError("Another pomodoro watcher is already running.") from None
            socket_path().unlink(missing_ok=True)  # Left by a watcher that didn't exit cleanly.
            listener = await anyio.create_unix_listener(socket_path())
            self.restore()
            async with listener, anyio.create_task_group() as task_group:
                task_group.start_soon(self.watch_activity)
                task_group.start_soon(self._keep_ticking, task_group)
                await listener.serve(self.serve, task_group)

    def restore(self) -> None:
        """Pick up the session a restart interrupted, unless it's too old to still matter."""
        path = _state_path()
        try:
            data = json.loads(path.read_text())
            age_s = self._wall_clock() - float(data["saved_at_wall"])
            session = Session.from_json(data, self._clock(), self._wall_clock())
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError, HueError) as error:
            logger.warning("Ignoring the saved pomodoro: %s", error)
            path.unlink(missing_ok=True)
            return
        if age_s > AWAY_LIMIT_S:  # A reboot overnight must not turn the lights off at login.
            path.unlink(missing_ok=True)
            return
        self.session = session
        self._verify_due = self._clock()  # A restart mid-pulse can leave the room red.

    async def tick(self, task_group: TaskGroup) -> None:
        async with self._lock:
            now = self._clock()
            asleep = self._asleep()
            if self._last_asleep is not None and asleep - self._last_asleep > SLEEP_SEEN_S:
                self._hold_until = now + RESUME_HOLD_S
            self._last_asleep = asleep
            session = self.session
            if session is None or now < self._hold_until:
                return
            if self._outside_change:
                reason = "The lights were changed from the switch or the Hue app."
                await self._end(session, reason)
                return
            look_before = session.look
            events = session.advance(now, self.tracker.at(now), self._band(session))
            changes: list[PhaseChanged] = []
            for event in events:
                if isinstance(event, RoundDone):
                    _log_round(event, session.room_name)
                elif isinstance(event, Ended):
                    hours = AWAY_LIMIT_S // 3600
                    reason = f"You were away for {hours} hours."
                    await self._end(session, reason, lights_off=True)
                    return
                else:
                    changes.append(event)
            if changes and session.look != look_before:
                await self._cancel_pulse(restore=False)
                self._previous_look = look_before
                self._changed_by_claude.clear()
                self._switch_due, self._verify_due = now, None
            # Waiting keeps the break's look: no switch, so a change at the switch survives.
            if events or now - self._saved_at >= SAVE_EVERY_S:
                self._save()
            if self._switch_due is not None and now >= self._switch_due:
                await self._switch(session)
            elif self._verify_due is not None and now >= self._verify_due:
                await self._verify(session)
            elif now >= self._quiet_until and self._pulse is None:
                nudge = session.due_nudge(now, self.tracker.at(now))
                if nudge is not None:
                    self._start_pulse(task_group, session, nudge)
            for change in changes:  # After the switch, so the lights don't wait on the desktop.
                await self._announce(session, change)

    async def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        command = request.get("command")
        async with self._lock:
            if command == "start":
                return await self._start(request)
            if command == "stop":
                return await self._stop()
            if command == "status":
                days = request.get("days", 7)
                if (
                    not isinstance(days, int)
                    or isinstance(days, bool)
                    or not 1 <= days <= MOST_DAYS
                ):
                    raise HueError(f"days must be a whole number from 1 to {MOST_DAYS}.")
                return self._status(days)
            if command == "save_look":
                return await self._save_look()
            if command == "touched":
                lights = request.get("lights")
                if not isinstance(lights, list) or not all(isinstance(i, str) for i in lights):
                    raise HueError("touched needs a list of light ids.")
                return await self._touched(lights)
        raise HueError(f"The watcher doesn't know the command {command!r}.")

    async def serve(self, stream: ByteStream) -> None:
        async with stream:
            try:
                with anyio.fail_after(CLIENT_TIMEOUT_S):
                    line = await BufferedByteReceiveStream(stream).receive_until(
                        b"\n", REQUEST_LIMIT_BYTES
                    )
                    request = json.loads(line)
                    if not isinstance(request, dict):
                        raise HueError("A request must be a JSON object.")
                    reply = {"ok": True, "result": await self.handle(request)}
            except HueError as error:
                reply = {"ok": False, "error": str(error)}
            except TimeoutError:
                reply = {"ok": False, "error": "The watcher took too long; try again."}
            except (ValueError, anyio.IncompleteRead, anyio.DelimiterNotFound) as error:
                reply = {"ok": False, "error": f"Bad request: {error!r}"}
            except Exception as error:  # One bad request must not take the watcher down.
                logger.exception("A request failed")
                reply = {"ok": False, "error": f"The watcher failed: {error!r}"}
            # The client may have given up waiting.
            with suppress(anyio.BrokenResourceError, anyio.ClosedResourceError, OSError):
                await stream.send(json.dumps(reply).encode() + b"\n")

    async def watch_activity(self) -> None:
        """Follow the user's activity, reconnecting whenever the compositor goes away."""
        delay_s = RECONNECT_FIRST_S
        while True:
            try:
                async with aclosing(idle_changes(round(IDLE_TIMEOUT_S * 1000))) as changes:
                    connected = False
                    async for idle in changes:
                        if not connected:  # The first change only says watching has started.
                            connected = True
                            self.tracker.connected(self._clock())
                            delay_s = RECONNECT_FIRST_S
                        elif idle:
                            self.tracker.idled(self._clock())
                        else:
                            self.tracker.resumed()
            except (OSError, WaylandError) as error:
                logger.warning("Can't watch the user's activity: %s", error)
            self.tracker.lost()
            await anyio.sleep(delay_s)
            delay_s = min(delay_s * 2, RECONNECT_MAX_S)

    async def _keep_ticking(self, task_group: TaskGroup) -> None:
        while True:
            try:
                await self.tick(task_group)
            except HueError as error:
                logger.warning("A pomodoro tick failed: %s", error)
            except Exception:  # The next tick may well succeed; dying here ends every session.
                logger.exception("A pomodoro tick failed")
            await anyio.sleep(TICK_S)

    async def _start(self, request: dict[str, Any]) -> dict[str, Any]:
        room, task_light_name, durations = _parse_start(request)
        location = load_location()
        if location is None:
            raise HueError(
                "The pomodoro follows the sun, but no location is set. Ask the user to run "
                '`hue-mcp set-location "<their city>"` in a terminal.'
            )
        bridge = self._get_bridge()
        home = Home(await bridge.get_resources())
        group = home.find_group(room)
        task_light = _task_light(home, group, task_light_name)
        band = band_at(datetime.now(UTC), location)
        notes: list[str] = []
        replaced = self.session
        if replaced is not None:
            await self._end_quietly()
            if replaced.room_id != group.id:
                notes += await self._recall(replaced.room_id, self._focus_scene(replaced))
        await self._ensure_scenes(bridge, home, group, task_light.id if task_light else None)
        session = Session.start(group.id, group.name, durations, location, self._clock(), band)
        self.session, self._previous_look = session, None
        self._changed_by_claude.clear()
        self._switch_due = self._clock()  # Should the switch below be cut short, ticks retry it.
        self._save()
        await self._switch(session)
        await self._announce(session, PhaseChanged(session.phase, session.round))
        result = self._status(days=0)
        if replaced is not None:
            result["replaced"] = f"the pomodoro that was running in {replaced.room_name}"
        if notes:
            result["warnings"] = notes
        return result

    async def _stop(self) -> dict[str, Any]:
        session = self._require_session()
        await self._end_quietly()
        notes = await self._recall(session.room_id, self._focus_scene(session))
        result: dict[str, Any] = {"stopped": f"the pomodoro in {session.room_name}"}
        if notes:
            result["warnings"] = notes
        return result

    async def _save_look(self) -> dict[str, Any]:
        session = self._require_session()
        await self._hold_off_for_a_change()
        bridge = self._get_bridge()
        async with self._room_lock:
            home, group, settled = await _settled_room(bridge, session.room_id)
            scene = _find_scene(home, group, session.look)
            if scene is None:
                raise HueError(f"The scene {session.look!r} is gone; stop and start the pomodoro.")
            actions = [
                {
                    "target": {"rid": light.id, "rtype": "light"},
                    "action": looks.current_look(light),
                }
                for light in group.lights
            ]
            await bridge.update("scene", scene.id, {"actions": actions})
        self._changed_by_claude.clear()  # The look now has the change.
        result: dict[str, Any] = {"saved_into": f"the {_look_label(session.look)} look"}
        if not settled:
            result["warnings"] = ["The lights were still changing; saved them mid-fade."]
        return result

    async def _touched(self, light_ids: list[str]) -> dict[str, Any]:
        """Claude is about to change these lights: hold off whatever would undo that, and don't
        take it for a change from the switch or the Hue app."""
        session = self.session
        if session is None:
            return {}
        home = Home(await self._get_bridge().get_resources())
        group = _room(home, session.room_id)
        in_room = set(light_ids) & {light.id for light in group.lights} if group else set()
        if not in_room:
            return {}
        await self._hold_off_for_a_change()
        self._changed_by_claude |= in_room
        return {"room": session.room_name, "look": _look_label(session.look)}

    async def _hold_off_for_a_change(self) -> None:
        await self._cancel_pulse(restore=True)
        self._quiet_until = self._clock() + QUIET_AFTER_CHANGE_S
        self._verify_due = None

    def _status(self, days: int) -> dict[str, Any]:
        result: dict[str, Any] = {}
        session = self.session
        if session is None:
            result["running"] = False
        else:
            result.update(running=True, room=session.room_name, phase=session.phase)
            result["round"] = f"{session.round} of {session.durations.rounds}"
            result["look"] = _look_label(session.look)
            deadline = session.deadline()
            if deadline is None:
                result["note"] = "The break is over; the next round starts when the user is back."
            else:
                result["ends_at"] = self._local_time(deadline)
        if days > 0:
            result["rounds_by_day"] = rounds_by_day(days)
        return result

    async def _switch(self, session: Session) -> None:
        """Recall the session's look; a failure is retried after a pause."""
        try:
            notes = await self._recall(session.room_id, session.look, SWITCH_FADE_MS)
        except HueError as error:
            logger.warning("Couldn't switch %s to %s: %s", session.room_name, session.look, error)
            self._switch_due = self._clock() + RETRY_AFTER_S
            return
        for note in notes:
            logger.warning("Switching %s to %s: %s", session.room_name, session.look, note)
        self._switch_due = None
        self._pulse_left_look = False
        self._verify_due = self._clock() + VERIFY_AFTER_S

    async def _verify(self, session: Session) -> None:
        """Resend the look to lights that missed the switch."""
        self._verify_due = None
        bridge = self._get_bridge()
        try:
            async with self._room_lock:
                home = Home(await bridge.get_resources())
                group = _room(home, session.room_id)
                if group is not None:
                    missed, _ = self._compare(home, group, session.look)
                    await _resend(bridge, missed)
        except HueError as error:
            logger.warning("Couldn't check the lights in %s: %s", session.room_name, error)
            self._verify_due = self._clock() + RETRY_AFTER_S

    def _compare(self, home: Home, group: Group, look: str) -> tuple[list[_Resend], bool]:
        """The lights that missed a command (still on the previous look, or a red pulse), with
        what to resend them; and whether someone else changed a light."""
        wanted = _scene_actions(_find_scene(home, group, look))
        previous = _scene_actions(_find_scene(home, group, self._previous_look))
        missed: list[_Resend] = []
        changed = False
        for light in group.lights:
            action = wanted.get(light.id)
            if action is None or not light.reachable or light.id in self._changed_by_claude:
                continue
            if looks.shows(light.resource, action):
                continue
            missed_switch = light.id in previous and looks.shows(
                light.resource, previous[light.id]
            )
            if missed_switch or looks.shows_red_pulse(light):
                missed.append((light, action))
            else:
                changed = True
        return missed, changed

    def _start_pulse(self, task_group: TaskGroup, session: Session, nudge: Nudge) -> None:
        # The scope exists before the task starts, so a cancel can't miss it.
        scope = anyio.CancelScope()
        self._pulse, self._pulse_done = scope, anyio.Event()
        task_group.start_soon(self._play, session, nudge, scope, self._pulse_done)

    async def _play(
        self, session: Session, nudge: Nudge, scope: anyio.CancelScope, done: anyio.Event
    ) -> None:
        try:
            with scope:
                async with self._room_lock:
                    await self._pulse_and_back(session, nudge)
        except Exception as error:  # Logged, and the look restored; never the watcher's end.
            logger.warning("A %s pulse in %s failed: %r", nudge, session.room_name, error)
            if self._pulse_left_look:
                self._switch_due = self._clock() + RETRY_AFTER_S
        finally:
            if self._pulse is scope:
                self._pulse = None
            done.set()

    async def _pulse_and_back(self, session: Session, nudge: Nudge) -> None:
        """Fade to the nudge and back to the look. A red pulse first checks that nobody changed
        the lights (that ends the session, at the next tick), and resends missed commands."""
        bridge = self._get_bridge()
        home = Home(await bridge.get_resources())
        group = _room(home, session.room_id)
        scene = _find_scene(home, group, session.look)
        if group is None or scene is None:
            return
        if nudge == "red":
            missed, changed = self._compare(home, group, session.look)
            if changed:
                self._outside_change = True
                return
            await _resend(bridge, missed)
        body, hold_s, back_ms = _nudge_command(nudge)
        self._pulse_left_look = True
        await bridge.update("grouped_light", group.grouped_light["id"], body)
        await anyio.sleep(hold_s)
        if nudge == "red" and self._switched_off_meanwhile(
            Home(await bridge.get_resources()), session
        ):
            self._outside_change = True
            return
        recall = {"recall": {"action": "active", "duration": back_ms}}
        await bridge.update("scene", scene.id, recall)
        self._pulse_left_look = False

    def _switched_off_meanwhile(self, home: Home, session: Session) -> bool:
        """Mid-pulse, lights are anywhere between the look and red, so only on and off tell."""
        group = _room(home, session.room_id)
        if group is None:
            return True
        wanted = _scene_actions(_find_scene(home, group, session.look))
        return any(
            light.resource["on"]["on"] != wanted[light.id].get("on", {}).get("on", True)
            for light in group.lights
            if light.id in wanted and light.reachable and light.id not in self._changed_by_claude
        )

    async def _cancel_pulse(self, restore: bool) -> None:
        """Stop a pulse in flight; with `restore`, bring the look back if the pulse had left it."""
        if self._pulse is not None:
            self._pulse.cancel()
        await self._pulse_done.wait()
        left_look, self._pulse_left_look = self._pulse_left_look, False
        if restore and left_look and self.session is not None:
            await self._recall(self.session.room_id, self.session.look, RESTORE_FADE_MS)

    async def _recall(
        self, room_id: str, scene_name: str, fade_ms: int = SWITCH_FADE_MS
    ) -> list[str]:
        bridge = self._get_bridge()
        async with self._room_lock:
            home = Home(await bridge.get_resources())
            scene = _find_scene(home, _room(home, room_id), scene_name)
            if scene is None:
                return [f"The scene {scene_name!r} is gone, so the lights stay as they are."]
            recall = {"recall": {"action": "active", "duration": fade_ms}}
            return await bridge.update("scene", scene.id, recall)

    async def _ensure_scenes(
        self, bridge: HueBridge, home: Home, group: Group, task_light_id: str | None
    ) -> None:
        """Create the looks the room is missing. Existing ones keep the user's changes, but
        gain any light added to the room since, and the task light's white."""
        for look in looks.ALL_LOOKS:
            defaults = {
                action["target"]["rid"]: action
                for action in looks.scene_actions(look, group.lights, task_light_id)
            }
            scene = _find_scene(home, group, look.scene_name)
            if scene is None:
                await bridge.create(
                    "scene",
                    {
                        "type": "scene",
                        "metadata": {"name": look.scene_name},
                        "group": {"rid": group.id, "rtype": group.kind},
                        "actions": list(defaults.values()),
                    },
                )
                continue
            actions = list(scene.resource.get("actions", []))
            index = {action["target"]["rid"]: i for i, action in enumerate(actions)}
            changed = False
            for light_id, default in defaults.items():
                if light_id not in index:
                    actions.append(default)
                    changed = True
                elif (
                    light_id == task_light_id
                    and "color_temperature" in default["action"]
                    and "color_temperature" not in actions[index[light_id]]["action"]
                ):
                    actions[index[light_id]] = default
                    changed = True
            if changed:
                await bridge.update("scene", scene.id, {"actions": actions})

    async def _end(self, session: Session, reason: str, lights_off: bool = False) -> None:
        await self._end_quietly()
        if lights_off:
            try:
                await self._turn_off(session.room_id)
            except HueError as error:
                logger.warning("Couldn't turn %s off: %s", session.room_name, error)
        await self._notify("Pomodoro ended", reason)

    async def _end_quietly(self) -> None:
        await self._cancel_pulse(restore=False)
        self.session = None
        self._outside_change = False
        self._switch_due = self._verify_due = None
        self._changed_by_claude.clear()
        _state_path().unlink(missing_ok=True)

    async def _turn_off(self, room_id: str) -> None:
        bridge = self._get_bridge()
        async with self._room_lock:
            group = _room(Home(await bridge.get_resources()), room_id)
            if group is not None:
                body = {"on": {"on": False}, "dynamics": {"duration": OFF_FADE_MS}}
                await bridge.update("grouped_light", group.grouped_light["id"], body)

    async def _announce(self, session: Session, change: PhaseChanged) -> None:
        rounds = session.durations.rounds
        deadline = session.deadline()
        if change.phase == "focus" and deadline is not None:
            title = f"Focus {change.round} of {rounds}"
            body = f"Until {self._local_time(deadline)}, in the {session.band} light."
        elif change.phase == "short_break":
            minutes = round(session.durations.short_break_s / 60)
            title, body = "Break time", f"{minutes} minutes. Step away from the screen."
        elif change.phase == "long_break":
            minutes = round(session.durations.long_break_s / 60)
            title, body = "Long break", f"{minutes} minutes. That was {rounds} rounds."
        else:
            title = "Break's over"
            body = "The next round starts when you're back at the computer."
        await self._notify(title, body)

    def _band(self, session: Session) -> Band:
        return band_at(datetime.now(UTC), session.location)

    def _focus_scene(self, session: Session) -> str:
        return looks.focus_look(self._band(session)).scene_name

    def _local_time(self, clock_time: float) -> str:
        moment = datetime.now(UTC) + timedelta(seconds=clock_time - self._clock())
        return moment.astimezone().strftime("%H:%M")

    def _require_session(self) -> Session:
        if self.session is None:
            raise HueError("No pomodoro is running.")
        return self.session

    def _save(self) -> None:
        if self.session is not None:
            state = self.session.to_json(self._clock(), self._wall_clock())
            atomic_write_json(_state_path(), state)
            self._saved_at = self._clock()


async def notify_desktop(title: str, body: str) -> None:
    command = ["notify-send", "--app-name=hue-mcp", "--expire-time=8000", title, body]
    try:
        with anyio.fail_after(NOTIFY_TIMEOUT_S):
            await anyio.run_process(command)
    except FileNotFoundError:
        logger.warning("notify-send isn't installed, so there are no desktop notifications.")
    except (TimeoutError, OSError, subprocess.CalledProcessError) as error:
        logger.warning("Couldn't show a desktop notification: %s", error)


def rounds_by_day(days: int) -> dict[str, int]:
    """Completed focus rounds per local date, for the last `days` days including today."""
    today = datetime.now().astimezone().date()
    counts = {str(today - timedelta(days=offset)): 0 for offset in range(days)}
    try:
        lines = _log_path().read_text().splitlines()
    except FileNotFoundError:
        return counts
    for line in lines:
        try:
            date = json.loads(line)["date"]
        except (ValueError, KeyError, TypeError):
            continue  # A line cut short by a crash.
        if date in counts:
            counts[date] += 1
    return counts


def _parse_start(request: dict[str, Any]) -> tuple[str, str | None, Durations]:
    """The start request's room, task light and durations, checked: any local program can
    write to the socket."""
    room, task_light = request.get("room"), request.get("task_light")
    if not isinstance(room, str):
        raise HueError("start needs the name of a room or zone.")
    if task_light is not None and not isinstance(task_light, str):
        raise HueError("task_light must be the name of a light.")
    for key, most in MOST_MINUTES.items():
        value = request.get(key)
        if not is_positive_number(value) or value > most:
            raise HueError(f"{key} must be more than 0 and at most {most}.")
    rounds = request.get("rounds")
    if not isinstance(rounds, int) or isinstance(rounds, bool) or not 1 <= rounds <= MOST_ROUNDS:
        raise HueError(f"rounds must be a whole number from 1 to {MOST_ROUNDS}.")
    durations = Durations(
        focus_s=request["focus_minutes"] * 60,
        short_break_s=request["short_break_minutes"] * 60,
        long_break_s=request["long_break_minutes"] * 60,
        rounds=rounds,
    )
    return room, task_light, durations


def _log_round(event: RoundDone, room_name: str) -> None:
    now = datetime.now().astimezone()
    entry = {
        "date": str(now.date()),
        "ended_at": now.isoformat(timespec="seconds"),
        "minutes": event.minutes,
        "band": event.band,
        "room": room_name,
    }
    path = _log_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("a") as log:
        log.write(json.dumps(entry) + "\n")


def _state_path() -> Path:
    return state_dir() / "pomodoro.json"


def _log_path() -> Path:
    return state_dir() / "focus_log.jsonl"


async def _settled_room(bridge: HueBridge, room_id: str) -> tuple[Home, Group, bool]:
    """The room once its lights hold still (mid-fade, the bridge reports passing values), and
    whether they did within SETTLE_TRIES reads."""
    home = Home(await bridge.get_resources())
    group = _existing_room(home, room_id)
    for _ in range(SETTLE_TRIES):
        await anyio.sleep(SETTLE_INTERVAL_S)
        again = Home(await bridge.get_resources())
        regrouped = _existing_room(again, room_id)
        if _states(regrouped) == _states(group):
            return again, regrouped, True
        home, group = again, regrouped
    return home, group, False


async def _resend(bridge: HueBridge, missed: list[_Resend]) -> None:
    for light, action in missed:
        body = {**action, "dynamics": {"duration": RESTORE_FADE_MS}}
        await bridge.update("light", light.id, body)


def _states(group: Group) -> list[Any]:
    keys = ("on", "dimming", "color", "color_temperature")
    return [(light.id, *(light.resource.get(key) for key in keys)) for light in group.lights]


def _room(home: Home, room_id: str) -> Group | None:
    return next((group for group in home.groups if group.id == room_id), None)


def _existing_room(home: Home, room_id: str) -> Group:
    group = _room(home, room_id)
    if group is None:
        raise HueError("The pomodoro's room no longer exists.")
    return group


def _find_scene(home: Home, group: Group | None, name: str | None) -> Scene | None:
    """The first by id, should the room have several with the name."""
    matches = [s for s in home.scenes if group is not None and s.group is group and s.name == name]
    return min(matches, key=lambda scene: scene.id, default=None)


def _scene_actions(scene: Scene | None) -> dict[str, dict[str, Any]]:
    if scene is None:
        return {}
    actions = scene.resource.get("actions", [])
    return {action["target"]["rid"]: action["action"] for action in actions}


def _task_light(home: Home, group: Group, name: str | None) -> Light | None:
    if name is None:
        return None
    light = home.find_target(name, "light")
    if not isinstance(light, Light) or light.id not in {member.id for member in group.lights}:
        raise HueError(f"{light.name} isn't in {group.name}, so it can't be its task light.")
    return light


def _nudge_command(nudge: Nudge) -> tuple[dict[str, Any], float, int]:
    """The grouped_light command, how long to hold it, and the fade back to the look."""
    if nudge == "red":
        return looks.red_pulse_body(), PULSE_HOLD_S, looks.PULSE_FADE_MS
    if nudge == "breath":
        return _brightness_step("up", looks.PULSE_FADE_MS), PULSE_HOLD_S, looks.PULSE_FADE_MS
    return _brightness_step("down", looks.DIP_FADE_MS), DIP_HOLD_S, looks.DIP_FADE_MS


def _brightness_step(direction: Literal["up", "down"], fade_ms: int) -> dict[str, Any]:
    step = {"action": direction, "brightness_delta": looks.BREATH_DELTA}
    return {"dimming_delta": step, "dynamics": {"duration": fade_ms}}


def _look_label(scene_name: str) -> str:
    return scene_name.removeprefix("Pomodoro ")
