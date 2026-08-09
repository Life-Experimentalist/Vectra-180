"""Desktop control panel.

An optional operator console for a bench or a development machine: it tunes
dewarping and depth parameters live, watches telemetry, and drives the same
recorder the headless service uses. The Pi in the car does not run this -- it
runs ``vectra180 run``.

DearPyGui is imported inside :func:`launch` rather than at module scope so that
importing :mod:`vectra180` on a headless Pi, where no GUI toolkit is installed,
costs nothing and fails nowhere.

Shutdown
--------

Closing this window must stop a recorder that may have a segment open. Four
paths lead out, and all of them converge on :meth:`DesktopApp.shutdown`:

* the title-bar close button, via DearPyGui's exit callback;
* ``Esc`` / ``Ctrl+Q`` / ``File > Exit``, which route through a confirmation
  when a recording is in flight;
* ``Ctrl+C`` in the terminal, via a SIGINT handler;
* an unhandled exception in the render loop, via ``finally``.

:meth:`~DesktopApp.shutdown` is idempotent and always finalises the open
segment, so a clip is never left as an unplayable stub.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from typing import Any

import cv2
import numpy as np

from vectra180 import __version__
from vectra180.config import EngineConfig
from vectra180.engine import Engine
from vectra180.errors import VectraError
from vectra180.imaging import HUDRenderer, split_stereo

__all__ = ["DesktopApp", "launch"]

log = logging.getLogger(__name__)

#: Size of the GPU texture. DearPyGui raw textures cannot be resized after
#: creation, so frames are letterboxed into this buffer instead.
TEXTURE_WIDTH = 1280
TEXTURE_HEIGHT = 400

#: Depth view refresh interval. SGBM runs on this thread, so recomputing it for
#: every rendered frame would peg a core without adding information.
DEPTH_INTERVAL_SECONDS = 0.25

#: Selectable with keys 1..5, in this order. "Raw" is the frame the recorder
#: actually writes; everything else is a viewing transform of it.
VIEW_MODES = ("Panorama", "Raw", "Left eye", "Right eye", "Depth")


class DesktopApp:
    """The DearPyGui window and its callbacks.

    ``dpg`` is injected rather than imported at module scope: this module has
    to stay importable without a GUI toolkit installed.
    """

    def __init__(self, engine: Engine, config: EngineConfig, dpg: Any) -> None:
        self.engine = engine
        self.config = config
        self.dpg = dpg
        self._texture = np.zeros((TEXTURE_HEIGHT, TEXTURE_WIDTH, 3), dtype=np.float32)
        self._closing = False
        self._view_mode = "Panorama"
        self._show_hud = True
        self._snapshot_dir = config.recording.directory / "snapshots"
        self._depth_image: np.ndarray | None = None
        self._depth_computed_at = 0.0

    # -- construction ------------------------------------------------------

    def build(self) -> None:
        dpg = self.dpg
        dpg.create_context()

        with dpg.texture_registry():
            dpg.add_raw_texture(
                TEXTURE_WIDTH,
                TEXTURE_HEIGHT,
                self._texture.ravel(),
                format=dpg.mvFormat_Float_rgb,
                tag="frame_texture",
            )

        self._build_theme()
        self._build_main_window()
        self._build_modals()
        self._build_menu()
        self._build_handlers()

        dpg.create_viewport(title=f"Vectra-180 {__version__}", width=1360, height=820, min_width=900, min_height=560)
        dpg.setup_dearpygui()
        dpg.set_viewport_resize_callback(self._on_resize)
        # Fires when the operating system closes the window -- the one exit
        # path that cannot be intercepted, so it must clean up unconditionally.
        dpg.set_exit_callback(self.shutdown)
        dpg.show_viewport()
        dpg.set_primary_window("main_window", True)

    def _build_theme(self) -> None:
        dpg = self.dpg
        with dpg.theme() as theme, dpg.theme_component(dpg.mvAll):
            dpg.add_theme_color(dpg.mvThemeCol_WindowBg, (11, 15, 25))
            dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (19, 26, 41))
            dpg.add_theme_color(dpg.mvThemeCol_FrameBg, (31, 42, 61))
            dpg.add_theme_color(dpg.mvThemeCol_Button, (31, 42, 61))
            dpg.add_theme_color(dpg.mvThemeCol_ButtonHovered, (0, 242, 254, 90))
            dpg.add_theme_color(dpg.mvThemeCol_Text, (232, 238, 252))
            dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 6)
            dpg.add_theme_style(dpg.mvStyleVar_WindowRounding, 8)
            dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 8, 6)
        dpg.bind_theme(theme)

    def _build_menu(self) -> None:
        dpg = self.dpg
        with dpg.viewport_menu_bar():
            with dpg.menu(label="File"):
                dpg.add_menu_item(label="Save snapshot          Space", callback=self.save_snapshot)
                dpg.add_menu_item(label="Open recordings folder", callback=self.open_recordings)
                dpg.add_separator()
                dpg.add_menu_item(label="Exit                   Ctrl+Q", callback=self.request_exit)
            with dpg.menu(label="Recording"):
                dpg.add_menu_item(label="Start / stop           R", callback=self.toggle_recording)
                dpg.add_menu_item(label="Lock current clip      L", callback=self.lock_clip)
            with dpg.menu(label="View"):
                dpg.add_menu_item(label="Toggle HUD             H", callback=self.toggle_hud)
                dpg.add_menu_item(label="Reset horizon          Z", callback=self.reset_horizon)
            with dpg.menu(label="Help"):
                dpg.add_menu_item(
                    label="Keyboard shortcuts", callback=lambda: dpg.configure_item("help_modal", show=True)
                )

    def _build_main_window(self) -> None:
        dpg = self.dpg
        with dpg.window(tag="main_window"), dpg.group(horizontal=True):
            with dpg.child_window(width=-330, border=False):
                dpg.add_image("frame_texture", tag="frame_image", width=TEXTURE_WIDTH, height=TEXTURE_HEIGHT)
                dpg.add_text("Waiting for the first frame...", tag="frame_status")
                with dpg.group(horizontal=True):
                    dpg.add_button(label="Record", tag="btn_record", callback=self.toggle_recording, width=120)
                    dpg.add_button(label="Lock clip", callback=self.lock_clip, width=120)
                    dpg.add_button(label="Snapshot", callback=self.save_snapshot, width=120)
                    dpg.add_button(label="Reset horizon", callback=self.reset_horizon, width=140)
                dpg.add_separator()
                dpg.add_text("", tag="telemetry_text")

            with dpg.child_window(width=320):
                dpg.add_text("VIEW", color=(0, 242, 254))
                dpg.add_combo(
                    VIEW_MODES,
                    default_value=self._view_mode,
                    tag="view_mode",
                    callback=self._on_view_mode,
                    width=-1,
                )
                dpg.add_checkbox(label="Show HUD", default_value=True, tag="show_hud", callback=self.toggle_hud)

                dpg.add_separator()
                dpg.add_text("OPTICS", color=(0, 242, 254))
                dpg.add_slider_float(
                    label="Focal",
                    tag="focal_scale",
                    default_value=self.config.depth.focal_scale,
                    min_value=0.2,
                    max_value=1.5,
                    format="%.2f",
                    callback=self._on_focal_scale,
                    width=-70,
                )

                dpg.add_separator()
                dpg.add_text("DEPTH", color=(0, 242, 254))
                dpg.add_slider_int(
                    label="Disparity",
                    tag="num_disparities",
                    default_value=self.config.depth.num_disparities,
                    min_value=16,
                    max_value=256,
                    callback=self._invalidate_depth,
                    width=-70,
                )
                dpg.add_slider_int(
                    label="Block",
                    tag="block_size",
                    default_value=self.config.depth.block_size,
                    min_value=3,
                    max_value=21,
                    callback=self._invalidate_depth,
                    width=-70,
                )
                dpg.add_slider_int(
                    label="Unique",
                    tag="uniqueness",
                    default_value=self.config.depth.uniqueness_ratio,
                    min_value=0,
                    max_value=30,
                    callback=self._invalidate_depth,
                    width=-70,
                )
                dpg.add_text("Depth is computed only while the Depth view is open.", wrap=300, color=(148, 163, 184))

                dpg.add_separator()
                dpg.add_text("STATUS", color=(0, 242, 254))
                dpg.add_text("", tag="status_text", wrap=300)
                dpg.add_text("", tag="message_text", wrap=300, color=(251, 191, 36))

    def _build_modals(self) -> None:
        dpg = self.dpg
        with dpg.window(
            tag="help_modal",
            label="Keyboard shortcuts",
            modal=True,
            show=False,
            width=380,
            height=280,
            pos=(420, 240),
        ):
            for key, action in (
                ("Space", "Save a snapshot"),
                ("R", "Start or stop recording"),
                ("L", "Lock the current clip"),
                ("H", "Toggle the HUD"),
                ("Z", "Reset the horizon"),
                ("1 - 5", "Switch view mode"),
                ("Esc / Ctrl+Q", "Exit"),
            ):
                dpg.add_text(f"{key:<14}{action}")
            dpg.add_separator()
            dpg.add_button(label="Close", width=-1, callback=lambda: dpg.configure_item("help_modal", show=False))

        with dpg.window(
            tag="exit_modal",
            label="Exit while recording?",
            modal=True,
            show=False,
            width=430,
            height=180,
            pos=(420, 300),
            no_close=True,
        ):
            dpg.add_text(
                "A recording is in progress. Exiting finalises the current clip first, "
                "so nothing already captured is lost.",
                wrap=400,
            )
            dpg.add_spacer(height=10)
            with dpg.group(horizontal=True):
                dpg.add_button(label="Stop and exit", width=195, callback=self.shutdown_and_stop)
                dpg.add_button(
                    label="Keep recording",
                    width=195,
                    callback=lambda: dpg.configure_item("exit_modal", show=False),
                )

    def _build_handlers(self) -> None:
        dpg = self.dpg
        with dpg.handler_registry():
            dpg.add_key_press_handler(dpg.mvKey_Escape, callback=self.request_exit)
            dpg.add_key_press_handler(dpg.mvKey_Q, callback=self._on_q)
            dpg.add_key_press_handler(dpg.mvKey_Spacebar, callback=self.save_snapshot)
            dpg.add_key_press_handler(dpg.mvKey_R, callback=self.toggle_recording)
            dpg.add_key_press_handler(dpg.mvKey_L, callback=self.lock_clip)
            dpg.add_key_press_handler(dpg.mvKey_H, callback=self.toggle_hud)
            dpg.add_key_press_handler(dpg.mvKey_Z, callback=self.reset_horizon)
            for offset, mode in enumerate(VIEW_MODES):
                dpg.add_key_press_handler(dpg.mvKey_1 + offset, callback=lambda _s, _d, m=mode: self._set_view_mode(m))

    # -- callbacks ---------------------------------------------------------

    def _message(self, text: str) -> None:
        self.dpg.set_value("message_text", text)

    def _on_q(self) -> None:
        dpg = self.dpg
        if dpg.is_key_down(dpg.mvKey_LControl) or dpg.is_key_down(dpg.mvKey_RControl):
            self.request_exit()

    def _on_resize(self) -> None:
        """Keep the preview as wide as the window without distorting it."""
        width = max(320, self.dpg.get_viewport_client_width() - 360)
        aspect = TEXTURE_HEIGHT / TEXTURE_WIDTH
        self.dpg.configure_item("frame_image", width=width, height=int(width * aspect))

    def _on_view_mode(self, _sender: Any, value: str) -> None:
        self._view_mode = value
        self._invalidate_depth()

    def _set_view_mode(self, mode: str) -> None:
        self._view_mode = mode
        self.dpg.set_value("view_mode", mode)
        self._invalidate_depth()

    def _invalidate_depth(self, *_args: Any) -> None:
        """Force the next Depth frame to recompute instead of reusing the cache."""
        self._depth_computed_at = 0.0

    def _on_focal_scale(self, _sender: Any, value: float) -> None:
        # Rounded because the dewarper caches a remap table per focal length;
        # otherwise every distinct value a drag produces would allocate one.
        self.engine.dewarper.focal_scale = round(value, 2)
        self.engine.dewarper.invalidate_cache()
        self._invalidate_depth()

    def toggle_hud(self) -> None:
        self._show_hud = not self._show_hud
        self.dpg.set_value("show_hud", self._show_hud)

    def reset_horizon(self) -> None:
        self.engine.orientation_filter.reset()
        self._message("Horizon reset")

    def toggle_recording(self) -> None:
        try:
            if self.engine.recorder.running:
                self.engine.recorder.stop()
                self._message("Recording stopped")
            else:
                self.engine.begin_recording()
                self._message("Recording started")
        except VectraError as exc:
            log.error("recording toggle failed: %s", exc)
            self._message(str(exc))

    def lock_clip(self) -> None:
        if not self.engine.recorder.running:
            self._message("Nothing to lock -- not recording")
            return
        self.engine.lock_incident()
        self._message("Current clip locked")

    def save_snapshot(self) -> None:
        image = self._render_frame()
        if image is None:
            self._message("No frame to save yet")
            return
        path = self._snapshot_dir / f"snap_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
        try:
            self._snapshot_dir.mkdir(parents=True, exist_ok=True)
            if not cv2.imwrite(str(path), image):
                raise OSError("OpenCV refused to encode the image")
        except OSError as exc:
            log.error("snapshot failed: %s", exc)
            self._message(f"Snapshot failed: {exc}")
            return
        log.info("snapshot saved to %s", path)
        self._message(f"Saved {path.name}")

    def open_recordings(self) -> None:
        """Reveal the recordings folder in the platform's file manager."""
        path = self.config.recording.directory
        try:
            path.mkdir(parents=True, exist_ok=True)
            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                opener = "open" if sys.platform == "darwin" else "xdg-open"
                subprocess.Popen([opener, str(path)])
        except OSError as exc:
            log.warning("could not open %s: %s", path, exc)
            self._message(f"Could not open {path}")

    # -- exit --------------------------------------------------------------

    def request_exit(self) -> None:
        """Ask to exit, confirming first if a recording is in flight."""
        if self.engine.recorder.running:
            self.dpg.configure_item("exit_modal", show=True)
            return
        self.shutdown_and_stop()

    def shutdown_and_stop(self) -> None:
        self.shutdown()
        self.dpg.stop_dearpygui()

    def shutdown(self) -> None:
        """Release the camera and finalise any open segment. Idempotent."""
        if self._closing:
            return
        self._closing = True
        log.info("shutting down the desktop UI")
        try:
            self.engine.stop()
        except Exception:
            log.exception("error while stopping the engine")

    # -- frame loop --------------------------------------------------------

    def _depth_frame(self) -> np.ndarray | None:
        """Return the cached disparity map, recomputing it when it is stale."""
        now = time.monotonic()
        if self._depth_image is None or now - self._depth_computed_at >= DEPTH_INTERVAL_SECONDS:
            self._depth_image = self.engine.compute_depth(
                num_disparities=self.dpg.get_value("num_disparities"),
                block_size=self.dpg.get_value("block_size"),
                uniqueness_ratio=self.dpg.get_value("uniqueness"),
            )
            self._depth_computed_at = now
        return self._depth_image

    def _render_frame(self) -> np.ndarray | None:
        """Build the image for the current view mode, HUD included."""
        snapshot = self.engine.snapshot()
        if snapshot is None:
            return None

        if self._view_mode == "Depth":
            depth = self._depth_frame()
            if depth is None:
                return None
            image = depth.copy()
        elif self._view_mode == "Panorama":
            image = self.engine.render_panorama(snapshot, TEXTURE_WIDTH)
        elif self._view_mode in ("Left eye", "Right eye"):
            left, right = split_stereo(snapshot.image)
            image = self.engine.dewarper.dewarp(left if self._view_mode == "Left eye" else right)
        else:
            image = snapshot.image.copy()

        if self._show_hud:
            HUDRenderer.draw_telemetry_overlay(
                image, snapshot.sample, snapshot.orientation, snapshot.fps, self.engine.hud_status()
            )
        return image

    def _update_texture(self, image: np.ndarray) -> None:
        """Letterbox an image into the fixed texture buffer.

        DearPyGui wants contiguous float RGB in 0..1; OpenCV supplies uint8
        BGR, so the conversion and the scale happen here, once per rendered
        frame.
        """
        height, width = image.shape[:2]
        scale = min(TEXTURE_WIDTH / width, TEXTURE_HEIGHT / height)
        new_w = max(1, int(width * scale))
        new_h = max(1, int(height * scale))
        resized = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)

        self._texture.fill(0.0)
        top = (TEXTURE_HEIGHT - new_h) // 2
        left = (TEXTURE_WIDTH - new_w) // 2
        self._texture[top : top + new_h, left : left + new_w] = rgb.astype(np.float32) / 255.0
        self.dpg.set_value("frame_texture", self._texture.ravel())

    def _update_panels(self) -> None:
        dpg = self.dpg
        snapshot = self.engine.snapshot()
        stats = self.engine.recorder.stats

        dpg.set_item_label("btn_record", "Stop" if self.engine.recorder.running else "Record")

        if snapshot is None:
            dpg.set_value("frame_status", "Waiting for the first frame...")
            dpg.set_value("telemetry_text", "")
        else:
            height, width = snapshot.image.shape[:2]
            dpg.set_value("frame_status", f"{self._view_mode}   {width}x{height}   {snapshot.fps:.1f} fps")

            sample = snapshot.sample
            roll, pitch, yaw = snapshot.orientation.as_tuple()
            if sample is None:
                telemetry = "No telemetry strip decoded from this camera."
            else:
                telemetry = (
                    f"GYRO  {sample.gyro_x:+8.3f} {sample.gyro_y:+8.3f} {sample.gyro_z:+8.3f}  rad/s\n"
                    f"ACCEL {sample.accel_x:+8.3f} {sample.accel_y:+8.3f} {sample.accel_z:+8.3f}  m/s2\n"
                    f"ATT   roll {roll:+7.2f}   pitch {pitch:+7.2f}   yaw {yaw:+7.2f}  deg"
                )
            dpg.set_value("telemetry_text", telemetry)

        dpg.set_value(
            "status_text",
            f"Recorder: {'running' if self.engine.recorder.running else 'stopped'}\n"
            f"Clip: {stats.current_clip or '-'}\n"
            f"Segments: {stats.segments_written}\n"
            f"Written: {stats.written_frames}  Dropped: {stats.dropped_frames}\n"
            f"Incidents: {self.engine.incidents.trigger_count}\n"
            f"Peak: {self.engine.incidents.peak_magnitude_g:.2f} g",
        )

    def run(self) -> None:
        """Render until the window closes."""
        dpg = self.dpg
        self._on_resize()
        while dpg.is_dearpygui_running():
            image = self._render_frame()
            if image is not None:
                self._update_texture(image)
            self._update_panels()
            dpg.render_dearpygui_frame()


def launch(config: EngineConfig) -> int:
    """Open the desktop UI. Returns a process exit code."""
    try:
        import dearpygui.dearpygui as dpg
    except ImportError as exc:  # pragma: no cover - depends on the install extra
        raise VectraError(
            "the desktop UI needs DearPyGui: install it with  uv pip install 'vectra-180[desktop]'"
        ) from exc

    engine = Engine(config)
    engine.start()

    app = DesktopApp(engine, config, dpg)

    # Ctrl+C in the terminal must take the same exit path as the window's close
    # button, or a segment is left unfinalised.
    def _on_sigint(_signum: int, _frame: Any) -> None:
        app.shutdown()
        dpg.stop_dearpygui()

    previous = signal.signal(signal.SIGINT, _on_sigint)
    try:
        app.build()
        app.run()
    finally:
        signal.signal(signal.SIGINT, previous)
        app.shutdown()
        dpg.destroy_context()
    return 0
