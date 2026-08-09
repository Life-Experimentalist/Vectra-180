"""The desktop control panel.

:class:`~vectra180.ui.desktop.DesktopApp` takes the DearPyGui module as a
constructor argument rather than importing it, which is what makes it testable
without a display. :class:`FakeDPG` stands in for that module: it records the
widgets the app creates, holds their values, and hands back the callbacks so a
test can press a key or click a button by invoking exactly what the toolkit
would have invoked.

The exit paths get the most attention here. Closing this window while a
recording is open has to finalise the segment -- a half-written clip is an
unplayable one -- and there are four ways out (window close, keyboard, SIGINT,
an exception in the render loop), so each is driven separately.

The engine underneath is real, with :class:`~tests.conftest.FakeCameraSource`
in place of the camera and frames pushed through by hand. That keeps the
snapshots, telemetry and depth maps genuine while leaving no thread running.
"""

from __future__ import annotations

import signal
import sys
import types
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NoReturn

import numpy as np
import pytest

from tests.conftest import FIRST_TIMESTAMP_US, FRAME_INTERVAL_US, FakeCameraSource, encode_payload, make_frame
from vectra180.capture import Frame
from vectra180.config import EngineConfig
from vectra180.engine import Engine
from vectra180.errors import RecorderError, VectraError
from vectra180.ui import desktop as desktop_module
from vectra180.ui.desktop import DEPTH_INTERVAL_SECONDS, TEXTURE_HEIGHT, TEXTURE_WIDTH, DesktopApp, launch

#: Capture time of the first hand-built frame, and the step between them.
START = 1_000.0
STEP = 1.0 / 30.0

#: Widget factories used as ``with`` blocks rather than called for a value.
SECTIONS = frozenset(
    {
        "texture_registry",
        "theme",
        "theme_component",
        "viewport_menu_bar",
        "menu",
        "window",
        "group",
        "child_window",
        "handler_registry",
    }
)


class FakeDPG:
    """A stand-in for ``dearpygui.dearpygui``.

    Only the calls :mod:`~vectra180.ui.desktop` actually makes are implemented.
    Everything else -- the ``mv*`` enum constants, the styling calls -- is
    answered by :meth:`__getattr__`, so adding a theme colour to the app does
    not mean editing this class.
    """

    def __init__(self, *, frame_budget: int = 0) -> None:
        self.values: dict[str, Any] = {}
        self.labels: dict[str, str] = {}
        self.config: dict[str, dict[str, Any]] = {}
        self.tags: list[str] = []
        self.buttons: dict[str, Callable[..., Any]] = {}
        self.menu_items: dict[str, Callable[..., Any]] = {}
        self.key_handlers: dict[int, Callable[..., Any]] = {}
        self.exit_callback: Callable[..., Any] | None = None
        self.resize_callback: Callable[..., Any] | None = None
        self.viewport_width = 1360
        self.held_keys: set[int] = set()
        self.frames = 0
        self.frame_budget = frame_budget
        self.stopped = False
        self.destroyed = False
        self._constants: dict[str, int] = {}

    # -- the parts with behaviour -----------------------------------------

    def get_value(self, tag: str) -> Any:
        return self.values.get(tag)

    def set_value(self, tag: str, value: Any) -> None:
        self.values[tag] = value

    def set_item_label(self, tag: str, label: str) -> None:
        self.labels[tag] = label

    def configure_item(self, tag: str, **kwargs: Any) -> None:
        self.config.setdefault(tag, {}).update(kwargs)

    def is_key_down(self, key: int) -> bool:
        return key in self.held_keys

    def get_viewport_client_width(self) -> int:
        return self.viewport_width

    def set_exit_callback(self, callback: Callable[..., Any]) -> None:
        self.exit_callback = callback

    def set_viewport_resize_callback(self, callback: Callable[..., Any]) -> None:
        self.resize_callback = callback

    def is_dearpygui_running(self) -> bool:
        return not self.stopped and self.frames < self.frame_budget

    def render_dearpygui_frame(self) -> None:
        self.frames += 1

    def stop_dearpygui(self) -> None:
        self.stopped = True

    def destroy_context(self) -> None:
        self.destroyed = True

    # -- the generic remainder --------------------------------------------

    def __getattr__(self, name: str) -> Any:
        if name.startswith("mv"):
            return self._constants.setdefault(name, 1000 + len(self._constants) * 10)
        if name in SECTIONS:
            return lambda *args, **kwargs: self._section(name, *args, **kwargs)
        if name.startswith("add_"):
            return lambda *args, **kwargs: self._add(name, *args, **kwargs)
        return lambda *args, **kwargs: None

    @contextmanager
    def _section(self, name: str, *args: Any, **kwargs: Any) -> Iterator[str]:
        del args
        yield self._register(name, kwargs)

    def _add(self, name: str, *args: Any, **kwargs: Any) -> str:
        tag = self._register(name, kwargs)
        if "default_value" in kwargs:
            self.values[tag] = kwargs["default_value"]
        elif name == "add_text":
            self.values[tag] = args[0] if args else ""

        label = kwargs.get("label", "")
        callback = kwargs.get("callback")
        if callback is not None:
            if name == "add_button":
                self.buttons[label] = callback
            elif name == "add_menu_item":
                self.menu_items[label] = callback
            elif name == "add_key_press_handler":
                self.key_handlers[args[0]] = callback
        return tag

    def _register(self, name: str, kwargs: dict[str, Any]) -> str:
        tag = kwargs.get("tag") or f"{name}#{len(self.tags)}"
        self.tags.append(tag)
        return str(tag)


def click(dpg: FakeDPG, prefix: str) -> None:
    """Invoke the button or menu item whose label starts with ``prefix``.

    Matched on a prefix because the menu labels carry trailing padding that
    lines their shortcut hints up.
    """
    for source in (dpg.buttons, dpg.menu_items):
        for label, callback in source.items():
            if label.startswith(prefix):
                callback()
                return
    raise AssertionError(f"no control labelled {prefix!r}; have {sorted(dpg.buttons) + sorted(dpg.menu_items)}")


def feed(engine: Engine, count: int = 3, *, telemetry: bool = True) -> None:
    """Push frames through the engine so it has something to display.

    ``_process`` is called directly rather than started as a thread: the sensor
    and capture clocks then advance by exact amounts, which is what makes the
    frame rate and orientation shown in the panels predictable.
    """
    for index in range(count):
        payload = encode_payload(timestamp_us=FIRST_TIMESTAMP_US + index * FRAME_INTERVAL_US)
        image = make_frame(payload if telemetry else None)
        engine._process(
            Frame(image=image, index=index, monotonic=START + index * STEP, wall_time=1_786_000_000.0 + index * STEP)
        )


@pytest.fixture
def engine(config: EngineConfig) -> Iterator[Engine]:
    """A real engine with a fake camera and no capture thread."""
    instance = Engine(config)
    instance.source = FakeCameraSource()  # type: ignore[assignment]
    yield instance
    # The recorder and the camera are released directly rather than through
    # Engine.stop(), which a test about a wedged shutdown will have replaced
    # with something that raises. No capture thread is ever started here.
    instance.recorder.stop()
    instance.source.close()


@pytest.fixture
def dpg() -> FakeDPG:
    return FakeDPG()


@pytest.fixture
def app(engine: Engine, config: EngineConfig, dpg: FakeDPG) -> DesktopApp:
    """A built app with frames already flowing."""
    feed(engine)
    instance = DesktopApp(engine, config, dpg)
    instance.build()
    return instance


# --------------------------------------------------------------------------
# construction
# --------------------------------------------------------------------------


def test_building_creates_the_widgets_the_loop_writes_to(app: DesktopApp, dpg: FakeDPG) -> None:
    """Every tag the render loop sets has to exist before the first frame."""
    del app

    for tag in ("frame_texture", "frame_image", "frame_status", "telemetry_text", "status_text", "message_text"):
        assert tag in dpg.tags, tag


def test_the_window_close_button_is_wired_to_shutdown(app: DesktopApp, dpg: FakeDPG) -> None:
    """The one exit path that cannot be intercepted, so it must clean up."""
    assert dpg.exit_callback is not None

    dpg.exit_callback()

    assert not app.engine.running
    assert app.engine.source.closed  # type: ignore[attr-defined]


def test_the_sliders_start_at_the_configured_values(app: DesktopApp, dpg: FakeDPG) -> None:
    assert dpg.get_value("num_disparities") == app.config.depth.num_disparities
    assert dpg.get_value("block_size") == app.config.depth.block_size
    assert dpg.get_value("uniqueness") == app.config.depth.uniqueness_ratio
    assert dpg.get_value("focal_scale") == pytest.approx(app.config.depth.focal_scale)


def test_the_preview_widens_with_the_window_without_distorting(app: DesktopApp, dpg: FakeDPG) -> None:
    dpg.viewport_width = 1760

    app._on_resize()

    sized = dpg.config["frame_image"]
    assert sized["width"] == 1400
    assert sized["height"] == int(1400 * TEXTURE_HEIGHT / TEXTURE_WIDTH)


def test_the_preview_never_shrinks_past_a_usable_width(app: DesktopApp, dpg: FakeDPG) -> None:
    """A narrow window would otherwise ask for a negative image width."""
    dpg.viewport_width = 200

    app._on_resize()

    assert dpg.config["frame_image"]["width"] == 320


# --------------------------------------------------------------------------
# view modes
# --------------------------------------------------------------------------


def test_the_panorama_view_joins_the_two_dewarped_eyes(app: DesktopApp) -> None:
    image = app._render_frame()

    assert image is not None
    snapshot = app.engine.snapshot()
    assert snapshot is not None
    # Wider than one eye, narrower than both, because the seam overlaps them.
    assert snapshot.image.shape[1] // 2 < image.shape[1] < snapshot.image.shape[1]


def test_the_raw_view_is_the_frame_without_its_telemetry_strip(app: DesktopApp) -> None:
    """What the recorder writes has to be inspectable, not just its prettier cousin."""
    app._set_view_mode("Raw")

    image = app._render_frame()

    assert image is not None
    snapshot = app.engine.snapshot()
    assert snapshot is not None
    assert image.shape == snapshot.image.shape


@pytest.mark.parametrize("mode", ["Left eye", "Right eye"])
def test_each_eye_renders_dewarped(app: DesktopApp, mode: str) -> None:
    app._set_view_mode(mode)

    image = app._render_frame()

    assert image is not None
    snapshot = app.engine.snapshot()
    assert snapshot is not None
    assert image.shape[1] == snapshot.image.shape[1] // 2


def test_the_depth_view_renders_a_disparity_map(app: DesktopApp) -> None:
    app._set_view_mode("Depth")

    image = app._render_frame()

    assert image is not None
    assert image.ndim == 3


def test_a_number_key_switches_the_view_and_the_combo_follows(app: DesktopApp, dpg: FakeDPG) -> None:
    """The keyboard and the dropdown must not disagree about what is shown."""
    dpg.key_handlers[dpg.mvKey_1 + 4](None, None)

    assert app._view_mode == "Depth"
    assert dpg.get_value("view_mode") == "Depth"


def test_the_dropdown_switches_the_view(app: DesktopApp) -> None:
    app._on_view_mode(None, "Right eye")

    assert app._view_mode == "Right eye"


def test_nothing_renders_before_the_first_frame(engine: Engine, config: EngineConfig, dpg: FakeDPG) -> None:
    app = DesktopApp(engine, config, dpg)
    app.build()

    assert app._render_frame() is None


def test_the_depth_view_renders_nothing_before_the_first_frame(
    engine: Engine, config: EngineConfig, dpg: FakeDPG
) -> None:
    app = DesktopApp(engine, config, dpg)
    app.build()
    app._set_view_mode("Depth")

    assert app._render_frame() is None


def test_the_depth_view_shows_nothing_rather_than_a_stale_map(app: DesktopApp, monkeypatch: pytest.MonkeyPatch) -> None:
    """The matcher races the capture thread and can find the frame gone."""
    monkeypatch.setattr(app.engine, "compute_depth", lambda **_kwargs: None)
    app._set_view_mode("Depth")

    assert app._render_frame() is None


def test_the_hud_can_be_taken_off_the_preview(app: DesktopApp) -> None:
    """Judging the raw image means seeing it without text drawn over it."""
    with_hud = app._render_frame()
    app.toggle_hud()
    without_hud = app._render_frame()

    assert with_hud is not None and without_hud is not None
    assert not np.array_equal(with_hud, without_hud)


# --------------------------------------------------------------------------
# depth caching
# --------------------------------------------------------------------------


class DepthProbe:
    """Stands in for stereo matching, counting calls and keeping the arguments."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> np.ndarray:
        self.calls.append(kwargs)
        return np.zeros((8, 8, 3), dtype=np.uint8)


@pytest.fixture
def depth_probe(app: DesktopApp, monkeypatch: pytest.MonkeyPatch) -> DepthProbe:
    """Make these tests about *when* the matcher runs, not what it finds."""
    probe = DepthProbe()
    monkeypatch.setattr(app.engine, "compute_depth", probe)
    return probe


def test_depth_is_reused_between_refreshes(app: DesktopApp, depth_probe: DepthProbe) -> None:
    """SGBM runs on the render thread, so recomputing it per frame pegs a core."""
    app._set_view_mode("Depth")

    app._render_frame()
    app._render_frame()

    assert len(depth_probe.calls) == 1


def test_depth_is_recomputed_once_the_cache_goes_stale(app: DesktopApp, depth_probe: DepthProbe) -> None:
    app._set_view_mode("Depth")

    app._render_frame()
    app._depth_computed_at -= DEPTH_INTERVAL_SECONDS
    app._render_frame()

    assert len(depth_probe.calls) == 2


def test_moving_a_depth_slider_takes_effect_immediately(app: DesktopApp, dpg: FakeDPG) -> None:
    """Waiting a quarter second to see a slider move would read as a broken UI."""
    app._set_view_mode("Depth")
    app._render_frame()
    assert app._depth_computed_at > 0.0

    dpg.set_value("block_size", 9)
    app._invalidate_depth()

    assert app._depth_computed_at == 0.0


def test_the_sliders_are_what_the_matcher_is_given(app: DesktopApp, dpg: FakeDPG, depth_probe: DepthProbe) -> None:
    dpg.set_value("num_disparities", 64)
    dpg.set_value("block_size", 7)
    dpg.set_value("uniqueness", 12)
    app._set_view_mode("Depth")

    app._render_frame()

    assert depth_probe.calls == [{"num_disparities": 64, "block_size": 7, "uniqueness_ratio": 12}]


def test_the_focal_slider_rounds_before_rebuilding_the_remap_table(app: DesktopApp) -> None:
    """The dewarper caches one table per focal length; a drag would fill memory."""
    app._on_focal_scale(None, 0.8123456)

    assert app.engine.dewarper.focal_scale == pytest.approx(0.81)
    assert app._depth_computed_at == 0.0


# --------------------------------------------------------------------------
# the texture
# --------------------------------------------------------------------------


def test_a_frame_is_letterboxed_into_the_fixed_texture(app: DesktopApp) -> None:
    """DearPyGui raw textures cannot be resized, so the image is fitted to them."""
    app._update_texture(np.full((64, 320, 3), 255, dtype=np.uint8))

    assert app._texture.shape == (TEXTURE_HEIGHT, TEXTURE_WIDTH, 3)
    assert app._texture.max() == pytest.approx(1.0)
    # 320x64 scaled to the 1280 width is 1280x256, leaving bars top and bottom.
    assert app._texture[0, TEXTURE_WIDTH // 2].max() == pytest.approx(0.0)
    assert app._texture[TEXTURE_HEIGHT // 2, TEXTURE_WIDTH // 2].max() == pytest.approx(1.0)


def test_the_texture_is_rgb_not_bgr(app: DesktopApp) -> None:
    """OpenCV hands over BGR; sending it unconverted swaps red and blue."""
    blue = np.zeros((64, 320, 3), dtype=np.uint8)
    blue[:, :, 0] = 255

    app._update_texture(blue)

    red, green, channel_blue = app._texture[TEXTURE_HEIGHT // 2, TEXTURE_WIDTH // 2]
    assert (red, green) == pytest.approx((0.0, 0.0))
    assert channel_blue == pytest.approx(1.0)


def test_a_frame_smaller_than_one_pixel_of_texture_still_fits(app: DesktopApp) -> None:
    """Guards the max(1, ...) floor: a zero-sized resize is a hard OpenCV error."""
    app._update_texture(np.zeros((1, 4000, 3), dtype=np.uint8))

    assert app._texture.shape == (TEXTURE_HEIGHT, TEXTURE_WIDTH, 3)


# --------------------------------------------------------------------------
# panels
# --------------------------------------------------------------------------


def test_the_panels_report_the_frame_and_the_telemetry(app: DesktopApp, dpg: FakeDPG) -> None:
    app._update_panels()

    assert "Panorama" in dpg.get_value("frame_status")
    assert "312x64" in dpg.get_value("frame_status")
    assert "GYRO" in dpg.get_value("telemetry_text")
    assert "Recorder: stopped" in dpg.get_value("status_text")


def test_a_camera_without_a_telemetry_strip_says_so(engine: Engine, config: EngineConfig, dpg: FakeDPG) -> None:
    """Blank numbers would look like a level, stationary car."""
    feed(engine, telemetry=False)
    app = DesktopApp(engine, config, dpg)
    app.build()

    app._update_panels()

    assert "No telemetry" in dpg.get_value("telemetry_text")


def test_the_panels_wait_rather_than_show_a_stale_frame(engine: Engine, config: EngineConfig, dpg: FakeDPG) -> None:
    app = DesktopApp(engine, config, dpg)
    app.build()

    app._update_panels()

    assert "Waiting" in dpg.get_value("frame_status")
    assert dpg.get_value("telemetry_text") == ""


def test_the_record_button_says_what_it_will_do(app: DesktopApp, dpg: FakeDPG) -> None:
    app._update_panels()
    assert dpg.labels["btn_record"] == "Record"

    app.toggle_recording()
    app._update_panels()

    assert dpg.labels["btn_record"] == "Stop"


# --------------------------------------------------------------------------
# recording controls
# --------------------------------------------------------------------------


def test_r_starts_and_stops_recording(app: DesktopApp, dpg: FakeDPG) -> None:
    dpg.key_handlers[dpg.mvKey_R]()
    assert app.engine.recorder.running
    assert dpg.get_value("message_text") == "Recording started"

    dpg.key_handlers[dpg.mvKey_R]()

    assert not app.engine.recorder.running
    assert dpg.get_value("message_text") == "Recording stopped"


def test_a_recorder_that_will_not_start_reports_why(
    app: DesktopApp, dpg: FakeDPG, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unwritable directory has to reach the operator, not just the log."""

    def refuse() -> NoReturn:
        raise RecorderError("cannot create /clips: read-only file system")

    monkeypatch.setattr(app.engine, "begin_recording", refuse)

    app.toggle_recording()

    assert "read-only file system" in dpg.get_value("message_text")


def test_locking_a_clip_needs_a_clip(app: DesktopApp, dpg: FakeDPG) -> None:
    click(dpg, "Lock clip")

    assert "Nothing to lock" in dpg.get_value("message_text")
    assert app.engine.incidents.trigger_count == 0


def test_locking_while_recording_protects_the_segment(app: DesktopApp, dpg: FakeDPG) -> None:
    app.toggle_recording()

    dpg.key_handlers[dpg.mvKey_L]()

    assert app.engine.incidents.trigger_count == 1
    assert "locked" in dpg.get_value("message_text")


def test_resetting_the_horizon_clears_the_filter(app: DesktopApp, dpg: FakeDPG) -> None:
    app.engine.orientation_filter.update(None, STEP)

    dpg.key_handlers[dpg.mvKey_Z]()

    assert app.engine.orientation_filter.orientation.as_tuple() == (0.0, 0.0, 0.0)
    assert dpg.get_value("message_text") == "Horizon reset"


def test_h_toggles_the_hud(app: DesktopApp, dpg: FakeDPG) -> None:
    assert app._show_hud

    dpg.key_handlers[dpg.mvKey_H]()

    assert not app._show_hud
    assert dpg.get_value("show_hud") is False


# --------------------------------------------------------------------------
# snapshots and the recordings folder
# --------------------------------------------------------------------------


def test_space_saves_a_snapshot(app: DesktopApp, dpg: FakeDPG, config: EngineConfig) -> None:
    dpg.key_handlers[dpg.mvKey_Spacebar]()

    saved = list((config.recording.directory / "snapshots").glob("snap_*.png"))
    assert len(saved) == 1
    assert saved[0].name in dpg.get_value("message_text")


def test_a_snapshot_before_the_first_frame_says_so(engine: Engine, config: EngineConfig, dpg: FakeDPG) -> None:
    app = DesktopApp(engine, config, dpg)
    app.build()

    app.save_snapshot()

    assert "No frame to save" in dpg.get_value("message_text")


def test_a_snapshot_that_cannot_be_written_reports_the_reason(
    app: DesktopApp, dpg: FakeDPG, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full card is the realistic case, and it must not take the window down."""
    monkeypatch.setattr(desktop_module.cv2, "imwrite", lambda *_args, **_kwargs: False)

    app.save_snapshot()

    assert "Snapshot failed" in dpg.get_value("message_text")


def test_a_snapshot_directory_that_cannot_be_made_reports_the_reason(
    app: DesktopApp, dpg: FakeDPG, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> NoReturn:
        raise PermissionError("read-only file system")

    monkeypatch.setattr(Path, "mkdir", refuse)

    app.save_snapshot()

    assert "read-only file system" in dpg.get_value("message_text")


def test_windows_reveals_the_recordings_folder_with_the_shell(
    app: DesktopApp, monkeypatch: pytest.MonkeyPatch, config: EngineConfig
) -> None:
    opened: list[Path] = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(desktop_module.os, "startfile", opened.append, raising=False)

    app.open_recordings()

    assert opened == [config.recording.directory]


@pytest.mark.parametrize(("platform", "opener"), [("darwin", "open"), ("linux", "xdg-open")])
def test_unix_reveals_the_recordings_folder_with_the_desktop_opener(
    app: DesktopApp, monkeypatch: pytest.MonkeyPatch, config: EngineConfig, platform: str, opener: str
) -> None:
    launched: list[list[str]] = []
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(desktop_module.subprocess, "Popen", lambda argv: launched.append(argv))

    app.open_recordings()

    assert launched == [[opener, str(config.recording.directory)]]


def test_a_folder_that_will_not_open_is_reported_not_raised(
    app: DesktopApp, dpg: FakeDPG, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> NoReturn:
        raise OSError("no file manager")

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(desktop_module.subprocess, "Popen", refuse)

    app.open_recordings()

    assert "Could not open" in dpg.get_value("message_text")


# --------------------------------------------------------------------------
# exit
# --------------------------------------------------------------------------


def test_escape_exits_straight_away_when_idle(app: DesktopApp, dpg: FakeDPG) -> None:
    dpg.key_handlers[dpg.mvKey_Escape]()

    assert dpg.stopped
    assert not app.engine.running


def test_escape_asks_first_when_a_recording_is_open(app: DesktopApp, dpg: FakeDPG) -> None:
    """Losing footage to a stray keypress is not recoverable."""
    app.toggle_recording()

    dpg.key_handlers[dpg.mvKey_Escape]()

    assert dpg.config["exit_modal"]["show"] is True
    assert not dpg.stopped
    assert app.engine.recorder.running


def test_confirming_the_exit_finalises_the_clip(app: DesktopApp, dpg: FakeDPG) -> None:
    app.toggle_recording()
    app.request_exit()

    click(dpg, "Stop and exit")

    assert dpg.stopped
    assert not app.engine.recorder.running
    assert not app.engine.running


def test_declining_the_exit_keeps_recording(app: DesktopApp, dpg: FakeDPG) -> None:
    app.toggle_recording()
    app.request_exit()

    click(dpg, "Keep recording")

    assert dpg.config["exit_modal"]["show"] is False
    assert not dpg.stopped
    assert app.engine.recorder.running


def test_ctrl_q_exits_but_q_alone_does_not(app: DesktopApp, dpg: FakeDPG) -> None:
    dpg.key_handlers[dpg.mvKey_Q]()
    assert not dpg.stopped

    dpg.held_keys.add(dpg.mvKey_LControl)
    dpg.key_handlers[dpg.mvKey_Q]()

    assert dpg.stopped


def test_the_right_control_key_works_too(app: DesktopApp, dpg: FakeDPG) -> None:
    del app
    dpg.held_keys.add(dpg.mvKey_RControl)

    dpg.key_handlers[dpg.mvKey_Q]()

    assert dpg.stopped


def test_the_file_menu_exit_takes_the_same_path(app: DesktopApp, dpg: FakeDPG) -> None:
    app.toggle_recording()

    click(dpg, "Exit")

    assert dpg.config["exit_modal"]["show"] is True


def test_shutdown_runs_once_however_many_times_it_is_called(app: DesktopApp) -> None:
    """Four exit paths converge here, and two of them can fire together."""
    stops: list[int] = []
    app.engine.stop = lambda *args, **kwargs: stops.append(1)  # type: ignore[method-assign]

    app.shutdown()
    app.shutdown()
    app.shutdown()

    assert stops == [1]


def test_an_engine_that_fails_to_stop_does_not_block_the_exit(
    app: DesktopApp, dpg: FakeDPG, caplog: pytest.LogCaptureFixture
) -> None:
    """Whatever the engine does, the window has to close."""

    def explode(*_args: Any, **_kwargs: Any) -> NoReturn:
        raise RuntimeError("the encoder wedged")

    app.engine.stop = explode  # type: ignore[method-assign]

    with caplog.at_level("ERROR", logger="vectra180.ui.desktop"):
        app.shutdown_and_stop()

    assert dpg.stopped
    assert "the encoder wedged" in caplog.text


# --------------------------------------------------------------------------
# the render loop
# --------------------------------------------------------------------------


def test_the_loop_renders_until_the_window_closes(app: DesktopApp, dpg: FakeDPG) -> None:
    dpg.frame_budget = 4

    app.run()

    assert dpg.frames == 4
    assert dpg.get_value("frame_texture") is not None


def test_the_loop_keeps_the_panels_current_before_any_frame_arrives(
    engine: Engine, config: EngineConfig, dpg: FakeDPG
) -> None:
    """The window must draw and stay responsive while the camera warms up."""
    app = DesktopApp(engine, config, dpg)
    app.build()
    dpg.frame_budget = 2

    app.run()

    assert dpg.frames == 2
    assert "Waiting" in dpg.get_value("frame_status")


# --------------------------------------------------------------------------
# launch
# --------------------------------------------------------------------------


@pytest.fixture
def dearpygui(monkeypatch: pytest.MonkeyPatch, dpg: FakeDPG) -> FakeDPG:
    """Install the fake toolkit where ``launch`` imports it from."""
    package = types.ModuleType("dearpygui")
    package.dearpygui = dpg  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "dearpygui", package)
    monkeypatch.setitem(sys.modules, "dearpygui.dearpygui", dpg)
    return dpg


@pytest.fixture
def fake_camera(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vectra180.engine.CameraSource", lambda _config: FakeCameraSource())


@pytest.mark.usefixtures("fake_camera")
def test_launch_opens_the_window_and_returns_a_clean_exit_code(dearpygui: FakeDPG, config: EngineConfig) -> None:
    assert launch(config) == 0

    assert dearpygui.destroyed


@pytest.mark.usefixtures("fake_camera")
def test_launch_restores_the_interrupt_handler_it_replaced(dearpygui: FakeDPG, config: EngineConfig) -> None:
    """Leaving a handler installed would break Ctrl+C for whatever runs next."""
    del dearpygui
    before = signal.getsignal(signal.SIGINT)

    launch(config)

    assert signal.getsignal(signal.SIGINT) is before


@pytest.mark.usefixtures("fake_camera")
def test_ctrl_c_in_the_terminal_finalises_the_clip(dearpygui: FakeDPG, config: EngineConfig) -> None:
    """The terminal and the window must not have different shutdown behaviour."""
    interrupt: list[Any] = []

    def capture(number: int, handler: Any) -> Any:
        if number == signal.SIGINT:
            interrupt.append(handler)
        return signal.SIG_DFL

    original = signal.signal
    try:
        signal.signal = capture  # type: ignore[assignment]
        dearpygui.frame_budget = 1
        launch(config)
    finally:
        signal.signal = original  # type: ignore[assignment]

    interrupt[0](int(signal.SIGINT), None)
    assert dearpygui.stopped


@pytest.mark.usefixtures("fake_camera")
def test_an_exception_in_the_render_loop_still_releases_the_camera(
    dearpygui: FakeDPG, config: EngineConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped: list[int] = []
    monkeypatch.setattr(DesktopApp, "run", lambda _self: (_ for _ in ()).throw(RuntimeError("render failed")))
    monkeypatch.setattr(DesktopApp, "shutdown", lambda _self: stopped.append(1))

    with pytest.raises(RuntimeError, match="render failed"):
        launch(config)

    assert stopped == [1]
    assert dearpygui.destroyed


def test_a_missing_toolkit_says_how_to_install_it(monkeypatch: pytest.MonkeyPatch, config: EngineConfig) -> None:
    """The desktop extra is optional, so this is a first-run message, not a bug."""
    monkeypatch.setitem(sys.modules, "dearpygui.dearpygui", None)

    with pytest.raises(VectraError, match=r"vectra-180\[desktop\]"):
        launch(config)
