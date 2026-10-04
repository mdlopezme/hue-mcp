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
readable only by you. Every connection verifies the bridge's certificate against Signify's root
CAs (bundled in `src/hue_mcp/hue_roots.pem`) and, from the first contact on, that it names the
bridge's id. (`setup --ip` learns that id from the bridge itself, over a connection whose
certificate must still chain to Signify's roots.)

## Use it from Claude Code

```sh
claude mcp add --scope user hue -- /absolute/path/to/hue-mcp/.venv/bin/hue-mcp
```

| Tool | Does |
|---|---|
| `get_home` | Rooms and zones, their lights' state, and their scenes |
| `set_lights` | On/off, brightness (absolute or relative), color, white tone, fades of up to 100 min |
| `activate_scene` / `create_scene` | Recall a scene, or save a room's current look as a new one |
| `set_effect` | candle, fire, prism and other looping effects; sunrise/sunset over up to 6 h; `none` stops them |
| `set_timer` / `list_timers` / `cancel_timer` | "Turn the bedroom off in 30 minutes", run by the bridge |

Good to know:

- **Fades and timers run on the bridge**, so they finish even after the Claude session ends.
- **Timers** use the bridge's v1 schedules (API v2 has none), so the Hue app doesn't show them.
  At most 10 can be pending at once; the bridge's schedule slots are shared with other apps.
- **Scenes** made with `create_scene` stay on the bridge; delete them in the Hue app.
- **Names** match exactly or by a unique part ("living" finds "Living room"). Misspellings are
  only suggested, never acted on, and `all` must be spelled out.
- **Partial success**: when a light in a group doesn't respond, the command still reaches the
  others and Claude is told which part may not have taken effect.

### Permissions

Claude Code asks before each tool call. You can allow `mcp__hue` in `/permissions` to skip the
prompts, but note that light, room and scene names come from the bridge and are shown to Claude
as-is: anyone who can rename your lights can put text in front of Claude.

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
change to what is sent to the bridge, run the hardware check, which drives every tool against
one light through the installed server and restores the light afterwards (about two minutes):

```sh
.venv/bin/python scripts/live_check.py --light "Desk"
```

A failure in its fade or timer step that doesn't reproduce is most likely the light itself:
Zigbee bulbs with a weak link occasionally drop a command, whichever way it is sent.

## License

MIT; see [LICENSE](LICENSE).
