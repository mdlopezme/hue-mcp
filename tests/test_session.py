import pytest

from hue_mcp.config import Location
from hue_mcp.errors import HueError
from hue_mcp.session import (
    AWAY_LIMIT_S,
    IDLE_TIMEOUT_S,
    Activity,
    ActivityTracker,
    Durations,
    Ended,
    PhaseChanged,
    RoundDone,
    Session,
)

PLACE = Location(latitude=38.72, longitude=-9.14, label="Lisbon, Portugal")
FOCUS, SHORT, LONG = 25 * 60, 5 * 60, 30 * 60
START = 1000.0


def session(rounds: int = 4, short_s: float = SHORT) -> Session:
    durations = Durations(focus_s=FOCUS, short_break_s=short_s, long_break_s=LONG, rounds=rounds)
    return Session.start("room-1", "Office", durations, PLACE, START, "midday")


def active(now: float) -> Activity:
    return Activity(known=True, idle=False, last_input=now)


def idle_since(last_input: float) -> Activity:
    return Activity(known=True, idle=True, last_input=last_input)


UNKNOWN = Activity(known=False, idle=False, last_input=0.0)


def test_a_session_starts_focused_in_the_look_for_the_part_of_the_day():
    s = session()
    assert (s.phase, s.round, s.look, s.band) == ("focus", 1, "Pomodoro midday", "midday")
    assert s.deadline() == START + FOCUS


def test_nothing_happens_before_the_focus_ends():
    s = session()
    assert s.advance(START + FOCUS - 1, active(START), "midday") == []
    assert s.phase == "focus"


def test_the_end_of_focus_logs_the_round_and_starts_a_short_break_on_schedule():
    s = session()
    events = s.advance(START + FOCUS + 3, active(START + FOCUS), "golden")
    assert events == [RoundDone(1, 25, "midday"), PhaseChanged("short_break", 1)]
    assert s.look == "Pomodoro short break"
    assert s.phase_started == START + FOCUS  # Not when the tick noticed.


def test_breaks_at_night_are_the_dimmer_night_versions():
    s = session()
    s.advance(START + FOCUS, active(START), "night")
    assert s.look == "Pomodoro short break night"


def test_the_last_round_ends_in_the_long_break():
    s = session(rounds=1)
    events = s.advance(START + FOCUS, active(START), "evening")
    assert events[-1] == PhaseChanged("long_break", 1)
    assert s.look == "Pomodoro long break"
    s = session(rounds=1)
    s.advance(START + FOCUS, active(START), "night")
    assert s.look == "Pomodoro long break night"


def test_a_break_ending_while_the_user_is_at_the_computer_goes_straight_to_focus():
    s = session()
    s.advance(START + FOCUS, active(START), "midday")
    now = START + FOCUS + SHORT + 0.5
    assert s.advance(now, active(now), "golden") == [PhaseChanged("focus", 2)]
    assert (s.look, s.band, s.phase_started) == ("Pomodoro golden", "golden", now)


def test_a_break_ending_while_the_user_is_away_waits_in_the_break_look():
    s = session()
    s.advance(START + FOCUS, active(START), "midday")
    break_ends = START + FOCUS + SHORT
    away = idle_since(break_ends - 120)
    assert s.advance(break_ends + 1, away, "midday") == [PhaseChanged("waiting", 1)]
    assert (s.look, s.phase_started, s.deadline()) == ("Pomodoro short break", break_ends, None)
    assert s.advance(break_ends + 600, away, "midday") == []


def waiting(rounds: int = 4) -> Session:
    s = session(rounds)
    s.advance(START + FOCUS, active(START), "midday")
    break_ends = s.deadline()
    assert break_ends is not None
    s.advance(break_ends, idle_since(START), "midday")
    assert s.phase == "waiting"
    return s


def test_coming_back_after_the_break_starts_the_next_round():
    s = waiting()
    now = s.phase_started + 300
    assert s.advance(now, idle_since(now - 2), "evening") == [PhaseChanged("focus", 2)]
    assert (s.look, s.phase_started) == ("Pomodoro evening", now)


def test_coming_back_after_the_long_break_starts_a_new_set():
    s = waiting(rounds=1)
    assert s.look == "Pomodoro long break"
    now = s.phase_started + 60
    assert s.advance(now, active(now), "midday") == [PhaseChanged("focus", 1)]


def test_input_from_before_the_break_ended_doesnt_count_as_coming_back():
    s = waiting()
    assert s.advance(s.phase_started + 30, idle_since(s.phase_started - 1), "midday") == []


def test_two_hours_away_ends_the_session():
    s = waiting()
    last_input = s.phase_started - 600
    assert s.advance(last_input + AWAY_LIMIT_S - 1, idle_since(last_input), "night") == []
    assert s.advance(last_input + AWAY_LIMIT_S, idle_since(last_input), "night") == [Ended()]


def test_unknown_activity_neither_starts_a_round_nor_ends_the_session():
    s = waiting()
    assert s.advance(s.phase_started + 3 * AWAY_LIMIT_S, UNKNOWN, "night") == []
    assert s.phase == "waiting"


def test_a_break_ending_with_unknown_activity_waits():
    s = session()
    s.advance(START + FOCUS, active(START), "midday")
    assert s.advance(START + FOCUS + SHORT, UNKNOWN, "midday") == [PhaseChanged("waiting", 1)]


def test_after_a_long_sleep_every_phase_that_passed_is_skipped_with_one_change():
    s = session()
    now = START + FOCUS + SHORT + 3600
    events = s.advance(now, idle_since(START + 60), "night")
    assert events == [RoundDone(1, 25, "midday"), PhaseChanged("waiting", 1)]
    assert s.phase_started == START + FOCUS + SHORT


def test_a_full_lap_in_one_step_still_reports_the_change():
    s = session(rounds=1)
    now = START + FOCUS + LONG + 1
    events = s.advance(now, active(now), "midday")
    assert events == [RoundDone(1, 25, "midday"), PhaseChanged("focus", 1)]


def test_focus_has_no_nudges():
    s = session()
    for now in (START, START + 100, START + FOCUS - 1):
        assert s.due_nudge(now, active(now)) is None


def test_red_pulses_start_ten_seconds_into_waiting_then_every_thirty():
    s = waiting()
    t0 = s.phase_started
    away = idle_since(t0 - 60)
    played = [now - t0 for now in range(int(t0), int(t0) + 75) if s.due_nudge(now, away)]
    assert played == [10, 40, 70]


def in_break(short_s: float = SHORT) -> Session:
    s = session(short_s=short_s)
    s.advance(START + FOCUS, active(START), "midday")
    return s


def test_working_through_a_break_gets_a_breath_after_a_minute_then_every_thirty_seconds():
    s = in_break()
    t0 = s.phase_started
    played = [
        (now - t0, nudge)
        for now in range(int(t0), int(t0) + 150)
        if (nudge := s.due_nudge(now, active(now)))
    ]
    assert played == [(60, "breath"), (90, "breath"), (120, "breath")]


def test_no_breath_for_a_user_who_stepped_away():
    s = in_break()
    t0 = s.phase_started
    away = idle_since(t0 + 20)  # Last touched the computer 20 s into the break.
    assert [s.due_nudge(now, away) for now in range(int(t0) + 60, int(t0) + 200)] == [None] * 140


def test_the_heads_up_dip_comes_once_a_minute_before_the_break_ends_and_replaces_the_breath():
    s = in_break()
    end = s.phase_started + SHORT
    assert s.due_nudge(end - 61, idle_since(START)) is None
    assert s.due_nudge(end - 60, active(end - 60)) == "dip"
    assert [s.due_nudge(end - t, active(end - t)) for t in (50, 30, 1)] == [None, None, None]


def test_short_breaks_get_no_breath_and_no_dip():
    s = in_break(short_s=120)
    t0 = s.phase_started
    assert [s.due_nudge(now, active(now)) for now in range(int(t0), int(t0) + 121)] == [None] * 121


def test_a_session_survives_a_restart_with_its_times_on_the_new_clock():
    s = waiting()
    data = s.to_json(now=s.phase_started + 100, wall_now=50_000.0)
    restored = Session.from_json(data, now=7.0, wall_now=50_030.0)  # Rebooted 30 s later.
    assert restored == Session(
        **{**s.__dict__, "phase_started": 7.0 - 130, "last_nudge": None, "dipped": False}
    )
    assert restored.location == PLACE


@pytest.mark.parametrize(
    "change",
    [
        {"phase": "napping"},
        {"band": "teatime"},
        {"round": "first"},
        {"durations": {"focus_s": 1}},
        {"location": None},
    ],
)
def test_an_unreadable_saved_session_is_refused(change):
    data = {**session().to_json(now=START, wall_now=START), **change}
    with pytest.raises(HueError, match="saved pomodoro"):
        Session.from_json(data, now=START, wall_now=START)


def test_a_new_connection_proves_nothing_until_the_idle_timeout_passes():
    tracker = ActivityTracker()
    assert not tracker.at(10).known
    tracker.connected(10)
    assert not tracker.at(10 + IDLE_TIMEOUT_S).known  # A restart isn't the user coming back.
    later = 10 + IDLE_TIMEOUT_S + 2
    assert tracker.at(later) == Activity(known=True, idle=False, last_input=later)


def test_an_idle_report_right_after_connecting_is_believed():
    tracker = ActivityTracker()
    tracker.connected(10)
    tracker.idled(15)
    assert tracker.at(16) == Activity(known=True, idle=True, last_input=15 - IDLE_TIMEOUT_S)


@pytest.mark.parametrize(
    "durations",
    [
        {"focus_s": 0, "short_break_s": 60, "long_break_s": 60, "rounds": 1},
        {"focus_s": 60, "short_break_s": -1, "long_break_s": 60, "rounds": 1},
        {"focus_s": 60, "short_break_s": 60, "long_break_s": float("inf"), "rounds": 1},
        {"focus_s": "25", "short_break_s": 60, "long_break_s": 60, "rounds": 1},
        {"focus_s": 60, "short_break_s": 60, "long_break_s": 60, "rounds": 0},
        {"focus_s": 60, "short_break_s": 60, "long_break_s": 60, "rounds": True},
    ],
)
def test_durations_that_could_never_advance_are_refused(durations):
    with pytest.raises(HueError):
        Durations(**durations)


def test_after_a_reconnect_the_first_idle_report_keeps_the_older_last_input():
    tracker = ActivityTracker()
    tracker.connected(10)
    tracker.resumed()
    tracker.idled(30)
    tracker.lost()
    tracker.connected(100)
    tracker.idled(105)
    assert tracker.at(106).last_input == 30 - IDLE_TIMEOUT_S


def test_idle_dates_the_last_input_to_before_the_timeout():
    tracker = ActivityTracker()
    tracker.connected(10)
    tracker.resumed()
    tracker.idled(30)
    assert tracker.at(100) == Activity(known=True, idle=True, last_input=30 - IDLE_TIMEOUT_S)
    tracker.resumed()
    assert tracker.at(101) == Activity(known=True, idle=False, last_input=101)
    tracker.lost()
    assert not tracker.at(102).known
