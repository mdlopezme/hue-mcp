# hue-mcp

An MCP server that lets Claude control Philips Hue lights through a Hue Bridge on your local
network. Ask in plain language: "dim the living room to 30% and make it warm", "candle effect
in the bedroom", "turn everything off in 30 minutes".

It talks to the bridge directly over your LAN (no Hue cloud account involved) using the bridge's
CLIP v2 API, plus the v1 schedules API for timers.

## Install

Needs Python 3.12 or newer and a Hue Bridge. Tested with the square Hue Bridge (v2); the
Bridge Pro speaks the same API.

```sh
git clone https://github.com/mdlopezme/hue-mcp.git && cd hue-mcp
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

On Debian/Ubuntu without the `python3-venv` package, create the venv with `--without-pip` and
bootstrap pip into it with the system pip (package `python3-pip`):
`python3 -m pip --python .venv/bin/python install pip`.

## Pair with the bridge

```sh
.venv/bin/hue-mcp setup              # or: setup --ip 192.168.1.20
```

Setup finds the bridge (mDNS, falling back to Signify's discovery service), asks you to press
the round link button on top of it, and saves an app key to `~/.config/hue-mcp/bridge.json`,
readable only by you, along with the streaming key light shows need (pairings from before
light shows lack it: run setup again to add it). Every connection verifies the bridge's certificate against Signify's root
CAs (bundled in `src/hue_mcp/hue_roots.pem`) and, from the first contact on, that it names the
bridge's id. (`setup --ip` learns that id from the bridge itself, over a connection whose
certificate must still chain to Signify's roots.)

## Use it from Claude Code

```sh
claude mcp add --scope user hue -- /absolute/path/to/hue-mcp/.venv/bin/hue-mcp
```

| Tool | Does |
|---|---|
| `get_home` | Rooms and zones, their lights' state, their scenes, and estimated watts |
| `set_lights` | On/off, brightness (absolute or relative), color, white tone, fades of up to 100 min |
| `set_power` | "Use 20 watts": one brightness for every light in the target, to fit the budget |
| `activate_scene` / `create_scene` | Recall a scene, or save a room's current look as a new one |
| `set_effect` | candle, fire, prism and other looping effects; sunrise/sunset over up to 6 h; `none` stops them |
| `set_timer` / `list_timers` / `cancel_timer` | "Turn the bedroom off in 30 minutes", run by the bridge |
| `start_pomodoro` / `stop_pomodoro` / `get_pomodoro` / `save_pomodoro_look` | Adaptive focus rounds whose looks follow the sun; see [Pomodoro](#pomodoro) |
| `start_light_show` / `stop_light_show` / `place_show_light` / `order_show_lights` | Lights following the music, the screen, the processor or a race kart, 25 times a second; see [Light shows](#light-shows) |

Good to know:

- **Fades and timers run on the bridge**, so they finish even after the Claude session ends.
- **Timers** use the bridge's v1 schedules (API v2 has none), so the Hue app doesn't show them.
  At most 10 can be pending at once; the bridge's schedule slots are shared with other apps.
  `list_timers` counts down with this computer's clock; the bridge fires them by its own.
- **Scenes** made with `create_scene` stay on the bridge; delete them in the Hue app.
- **Names** match exactly or by a unique part ("living" finds "Living room"). Misspellings are
  only suggested, never acted on, and `all` must be spelled out.
- **Partial success**: when a light in a group doesn't respond, the command still reaches the
  others and Claude is told which part may not have taken effect.
- **Hue bulbs sometimes switch themselves back on** right after being turned off, mostly after
  a fade or a room-wide off. It's a known quirk of the bulbs, not of this server (see
  [zigbee2mqtt #20336](https://github.com/Koenkk/zigbee2mqtt/issues/20336)); asking again turns
  them off.
- **Watts are estimates.** Hue bulbs don't report their draw, so `get_home` and `set_power`
  model it: about 0.5 W standby while off, rising linearly with brightness to the bulb's rating.
  Ratings for known models are in `src/hue_mcp/power.py`; other bulbs are assumed to be 9 W.

### Permissions

Claude Code asks before each tool call. You can allow `mcp__hue` in `/permissions` to skip the
prompts, but note that light, room and scene names come from the bridge and are shown to Claude
as-is: anyone who can rename your lights can put text in front of Claude.

## Pomodoro

`start_pomodoro` runs focus rounds (25 minutes by default) with breaks between them, told apart by
the room's lights:

- **Focus** looks follow the part of the day at your location: fresh blues in the morning,
  sunny yellows at midday, golden oranges in the afternoon, sunset rose in the evening, and a
  dim, blue-free red at night. A task light, such as a desk lamp, stays a white to read by,
  warming as the day goes.
- **Short breaks** are greens (5 minutes), and the **long break** after the last round is violet
  (30 minutes). At night both turn dimmer and warmer.
- **When a break ends and you're away from the computer**, the room keeps the break colors and
  breathes to dim red and back, over and over, until you're back: your first mouse move or key
  press starts the next round. If you keep working through a break, the lights breathe brighter to send you off, and
  a minute before a break ends they dip as a heads-up (breaks of two minutes or less skip both).
- **A session ends** when you ask Claude to stop it; when someone turns the room's lights off
  or changes their colors at the switch or in the Hue app while it waits for you (the lights
  stay as they set them; a change of brightness alone is taken for a bulb that missed a
  command, and put back); or after two hours away (the room fades off).
- `get_pomodoro` tells the current phase and counts the rounds completed each day. Desktop
  notifications mark each switch.

A background service, the watcher, runs the sessions: it notices when you're at the computer and
switches the lights. It needs a Wayland compositor that offers the input idle notifications of
`ext-idle-notify-v1` version 2 (tested on KDE Plasma 6), and it only ever learns *whether* you're
using the computer, never what you type. Set it up once:

```sh
.venv/bin/hue-mcp set-location "Lisbon"   # where the lights are, for the sun times
.venv/bin/hue-mcp install-watcher         # a systemd user service, started at every login;
                                          # run it again after an update to restart it
.venv/bin/hue-mcp watch --print-activity  # optional: check it sees you go idle and come back
```

`set-location` looks the city up once with [Open-Meteo](https://open-meteo.com/)'s free place
search and saves its coordinates to `~/.config/hue-mcp/location.json`; the sun times are then
computed locally. The first session in a room creates nine scenes there ("Pomodoro morning",
"Pomodoro short break", ...). Change one in the Hue app, or ask Claude to tweak the lights
mid-session: the change lasts until the next switch or nudge, unless `save_pomodoro_look` keeps
it in the look showing. Naming a task light later gives it its white in the existing looks, and
a light added to the room joins them. The watcher keeps its state and the
rounds log in `~/.local/state/hue-mcp/`; its log is `journalctl --user -u hue-mcp-watch`.

## Light shows

`start_light_show` streams colors to an entertainment area (make one in the Hue app: Settings,
Entertainment areas) through Hue's entertainment API, the one Hue Sync uses: every light
changes together, about 25 times a second. Five modes:

- **music**: colors spread across the lights, swinging with the bass and punching on the beats
  of whatever the computer plays.
- **screen**: each light mirrors its part of the screen. `place_show_light` picks the monitor
  (counting from 1 at the left) and the region (left, right, top, bottom, center or whole);
  `max_brightness` keeps a light that shines on you low.
- **both**: the screen's colors at the music's brightness.
- **cpu**: how loaded the computer is, per core: the 1-minute load average colors the lights,
  from blue when idle through cyan, green, yellow and orange to red when busy, and the 5-minute
  one sets their brightness, so a burst shifts the color and only sustained work brightens the
  room. The kernel already smooths both, so the lights drift rather than jump.
- **race**: a race kart's state, from its ROS 2 stack (see [The race show](#the-race-show)).

`order_show_lights` sets the lights' order, around the room say: music colors spread along it. The watcher plays the show, so it outlasts the
conversation; a running pomodoro pauses for it and carries on after. Music and screen shows end
after 15 minutes without you at the computer; the cpu and race shows, monitors, stay on. Either way,
the lights go back to how they were before the show. Flashes never come faster than three a
second, whatever the screen or the music does.

Streaming needs the system's OpenSSL (libssl 3), `parec` for the music modes, and for the
screen modes the system Python's PyGObject and GStreamer PipeWire plugin (Debian/Ubuntu:
`python3-gi gir1.2-gstreamer-1.0 gstreamer1.0-pipewire`). The first screen show asks you to share
the screen; on a multi-monitor desktop, share the full workspace and the watcher cuts it into
monitors. Only loudness and bass leave the sound, and a 64x20-pixel thumbnail the screen;
neither is kept.

### The race show

The race show is for a pit wall: the room shows how a race kart is doing, from the ROS 2 topics
its stack publishes. A breathing light means a problem with the kart, and beats any flag:

| Breathing | When |
|---|---|
| Blue | No fault status for 2 s: the link is down, or the stack isn't running |
| Amber / orange / red | The fault multiplexer asks for a safe / immediate / emergency stop |

Otherwise a steady light shows the race-control flag. When several are up, the first in this
list wins:

| Steady | Flags |
|---|---|
| Red | E-kill, stop, red |
| Dim white | Black |
| Purple | Purple |
| Orange | Orange |
| Yellow | Double yellow, yellow, full course yellow |
| Blue | Blue |
| White | Checkered |
| Green | Green, waved green, the speed-limited greens |
| Dim warm white | No flag, or none for 5 s |

It reads `/faults/status` (`race_msgs/FaultMultiplexerStatus`) and `/rc_to_ct`
(`race_msgs/VehicleCommand`) through a helper that runs in the stack's workspace, on the
Python ROS uses. Tell it where the workspace is once, from a shell set up for the stack: the
setup script doesn't set the middleware, so `set-ros` also saves this shell's
`RMW_IMPLEMENTATION`, `CYCLONEDDS_URI`, `ROS_DOMAIN_ID`, `ROS_AUTOMATIC_DISCOVERY_RANGE` and
`ROS_STATIC_PEERS` to `~/.config/hue-mcp/ros.json`:

```sh
.venv/bin/hue-mcp set-ros ~/ws/install/setup.bash
```

Run it again after changing any of those settings.

## Development

```sh
.venv/bin/python -m pip install -e '.[dev]'
make check      # ruff (lint + format), mypy --strict, pytest with branch coverage
make format     # apply ruff's fixes and formatting
```

CI runs `make check` on Python 3.12, 3.13 and 3.14 for pushes to `main` and for every pull
request, and weekly to catch breaking upstream releases. Dependabot proposes dependency and
action updates.

The tests use a fake bridge, so they can't prove the real bridge agrees. Before releasing a
change to what is sent to the bridge, run the hardware check. It drives every tool through the
installed server against the lights you pick, checks what each light actually does, and puts
them back afterwards (about eight minutes; the lights change, flicker and switch on and off). Its
pomodoro step runs the watcher's logic with second-long phases, so it needs a location set:

```sh
.venv/bin/python scripts/live_check.py --light "Desk"   # or --all, or --light repeated
```

A fade or timer failure that doesn't reproduce is most likely a bulb, not the code: bulbs with a
weak Zigbee link drop the odd command, and Hue bulbs sometimes switch back on after an off.

## License

MIT; see [LICENSE](LICENSE).
