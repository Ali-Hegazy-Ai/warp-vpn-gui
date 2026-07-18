#!/usr/bin/env python3
"""
Cloudflare WARP VPN GUI — single-file desktop controller.

Architecture
------------
WarpCLI    — subprocess wrapper around warp-cli (JSON mode).
State      — explicit state machine via WarpState enum.
GUI        — Tkinter main loop with thread-safe queue for background ops.
Lock       — fcntl-based single-instance lock (user-specific).
Logging    — Python logging module writes to ~/.local/state/warp-vpn/warp-gui.log.

Threading model
---------------
All warp-cli calls run in daemon threads so the UI never blocks.
Results are pushed into a queue.Queue which the GUI polls every 100 ms.
"""

from __future__ import annotations

import enum
import json
import logging
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WARP_CLI = "/usr/bin/warp-cli"

STATUS_TIMEOUT = 10
ACTION_TIMEOUT = 30
POST_ACTION_POLL_INTERVAL = 1.5
POST_ACTION_MAX_POLLS = 12

REFRESH_INTERVAL_MS = 15_000
QUEUE_POLL_MS = 100

_LOCK_FILE: str | None = None
_LOCK_DIR: str | None = None


def _get_lock_dir() -> str:
    global _LOCK_DIR
    if _LOCK_DIR is None:
        _LOCK_DIR = os.environ.get("XDG_RUNTIME_DIR", "")
        if not _LOCK_DIR or not os.access(_LOCK_DIR, os.W_OK):
            _LOCK_DIR = tempfile.gettempdir()
    return _LOCK_DIR


def _get_lock_file() -> str:
    global _LOCK_FILE
    if _LOCK_FILE is None:
        uid = os.getuid()
        _LOCK_FILE = f"{_get_lock_dir()}/warp-vpn-gui-{uid}.lock"
    return _LOCK_FILE


# ---------------------------------------------------------------------------
# Single-instance lock
# ---------------------------------------------------------------------------


def _acquire_lock() -> int:
    import fcntl

    path = _get_lock_file()
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        print("Another instance is already running.", file=sys.stderr)
        sys.exit(1)
    return fd


def _release_lock(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.unlink(_get_lock_file())
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _log_dir() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        base = Path(xdg)
    else:
        base = Path.home() / ".local" / "state"
    d = base / "warp-vpn"
    d.mkdir(parents=True, exist_ok=True)
    return d


LOG_FILE = _log_dir() / "warp-gui.log"


def _setup_logging() -> logging.Logger:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    fh = logging.FileHandler(str(LOG_FILE))
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    ch = logging.StreamHandler(sys.stderr)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    return logging.getLogger("warp-gui")


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


class WarpState(enum.Enum):
    UNKNOWN = "Unknown"
    DISCONNECTED = "Disconnected"
    CONNECTING = "Connecting"
    CONNECTED = "Connected"
    DISCONNECTING = "Disconnecting"
    ERROR = "Error"


# ---------------------------------------------------------------------------
# warp-cli controller
# ---------------------------------------------------------------------------


class WarpCLIError(RuntimeError):
    """Raised when warp-cli returns a non-zero exit or unparseable output."""


class WarpCLI:
    """Safe subprocess wrapper around warp-cli."""

    def __init__(self, logger: logging.Logger) -> None:
        self._log = logger
        self._check_binary()

    # -- helpers -----------------------------------------------------------

    def _check_binary(self) -> None:
        p = Path(WARP_CLI)
        if not p.is_file():
            raise WarpCLIError(f"warp-cli not found at {WARP_CLI}")
        if not os.access(str(p), os.X_OK):
            raise WarpCLIError(f"warp-cli at {WARP_CLI} is not executable")

    def _run(
        self, args: list[str], *, timeout: int = ACTION_TIMEOUT
    ) -> subprocess.CompletedProcess:
        cmd = [WARP_CLI, "--accept-tos"] + args
        self._log.debug("Running: %s", " ".join(cmd))
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False,
            )
        except FileNotFoundError:
            raise WarpCLIError(f"warp-cli binary not found at {WARP_CLI}")
        except subprocess.TimeoutExpired:
            raise WarpCLIError(
                f"Command timed out after {timeout}s: {' '.join(cmd)}"
            )
        except OSError as exc:
            raise WarpCLIError(f"OS error running warp-cli: {exc}") from exc

        self._log.debug(
            "rc=%d stdout=%r stderr=%r", proc.returncode, proc.stdout, proc.stderr
        )
        return proc

    def _run_json(self, args: list[str], **kw) -> dict:
        """Run a warp-cli subcommand that returns JSON (use -j).

        Validates the response: checks return code, JSON parse, and
        ``{"status": "Error"}`` response bodies.
        """
        proc = self._run(["-j"] + args, **kw)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise WarpCLIError(
                f"warp-cli {' '.join(args)} failed (rc={proc.returncode}): {detail}"
            )
        try:
            data = json.loads(proc.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise WarpCLIError(
                f"Invalid JSON from warp-cli {' '.join(args)}: {proc.stdout!r}"
            ) from exc
        if not isinstance(data, dict):
            raise WarpCLIError(
                f"Unexpected response type from warp-cli "
                f"{' '.join(args)}: {type(data).__name__} ({proc.stdout!r})"
            )
        status_val = data.get("status")
        if status_val == "Error":
            err_detail = data.get("error") or data.get("reason") or "Unknown error"
            raise WarpCLIError(
                f"warp-cli {' '.join(args)} returned error: {err_detail}"
            )
        return dict(data)

    # -- public API --------------------------------------------------------

    def raw_status(self) -> dict:
        """Return the parsed JSON object from ``warp-cli -j status``."""
        return self._run_json(["status"], timeout=STATUS_TIMEOUT)

    def status(self) -> tuple[WarpState, str]:
        """
        Return (state, detail) derived from warp-cli status.

        Known status values:
          - ``"Connected"``    (reason e.g. ``"NetworkHealthy"``)
          - ``"Disconnected"`` (reason e.g. ``"Manual"``)
          - ``"Connecting"``
          - ``"Disconnecting"``
        """
        data = self.raw_status()
        raw = data.get("status", "")
        reason = data.get("reason", "")

        # reason can sometimes be a nested dict (e.g. network degradation info)
        if not isinstance(reason, str):
            if isinstance(reason, dict):
                parts: list[str] = []
                for k, v in reason.items():
                    if isinstance(v, dict):
                        sub = ", ".join(f"{sk}: {sv}" for sk, sv in v.items())
                        parts.append(f"{k} ({sub})")
                    else:
                        parts.append(f"{k}: {v}")
                reason = "; ".join(parts)
            else:
                reason = str(reason)

        state = _parse_warp_status(raw)
        detail = reason if reason else raw
        self._log.debug("Parsed status: %s (%s) <- %s", state, detail, data)
        return state, detail

    def connect(self) -> None:
        """Connect via WARP. Raises on failure or if verification fails."""
        self._log.info("Issuing warp-cli connect")
        self._run_json(["connect"])
        self._log.info("Connect command accepted; verifying...")

        for attempt in range(1, POST_ACTION_MAX_POLLS + 1):
            time.sleep(POST_ACTION_POLL_INTERVAL)
            state, detail = self.status()
            if state == WarpState.CONNECTED:
                self._log.info(
                    "Connect verified after poll %d/%d", attempt, POST_ACTION_MAX_POLLS
                )
                return
            self._log.debug("Poll %d: state=%s detail=%s", attempt, state, detail)

        state, detail = self.status()
        raise WarpCLIError(
            f"Connect did not reach Connected state after "
            f"{POST_ACTION_MAX_POLLS * POST_ACTION_POLL_INTERVAL:.0f}s "
            f"(last state: {state.value}, detail: {detail})"
        )

    def disconnect(self) -> None:
        """Disconnect from WARP. Raises on failure or if verification fails."""
        self._log.info("Issuing warp-cli disconnect")
        self._run_json(["disconnect"])
        self._log.info("Disconnect command accepted; verifying...")

        for attempt in range(1, POST_ACTION_MAX_POLLS + 1):
            time.sleep(POST_ACTION_POLL_INTERVAL)
            state, detail = self.status()
            if state == WarpState.DISCONNECTED:
                self._log.info(
                    "Disconnect verified after poll %d/%d",
                    attempt,
                    POST_ACTION_MAX_POLLS,
                )
                return
            self._log.debug("Poll %d: state=%s detail=%s", attempt, state, detail)

        state, detail = self.status()
        raise WarpCLIError(
            f"Disconnect did not reach Disconnected state after "
            f"{POST_ACTION_MAX_POLLS * POST_ACTION_POLL_INTERVAL:.0f}s "
            f"(last state: {state.value}, detail: {detail})"
        )


def _parse_warp_status(raw: str) -> WarpState:
    """Map a raw status string to a WarpState using safe substring matching."""
    lower = raw.lower()

    # Order matters: check longer / more specific substrings first.
    if lower == "connected":
        return WarpState.CONNECTED
    if lower == "disconnected":
        return WarpState.DISCONNECTED
    if lower == "connecting":
        return WarpState.CONNECTING
    if lower == "disconnecting":
        return WarpState.DISCONNECTING

    # Fallback substring matching for any unexpected variants.
    if "disconnected" in lower or "disconnect" in lower:
        return WarpState.DISCONNECTED
    if "connected" in lower or "connecting" in lower:
        return WarpState.CONNECTED
    return WarpState.UNKNOWN


# ---------------------------------------------------------------------------
# Tkinter GUI
# ---------------------------------------------------------------------------

try:
    import tkinter as tk
    from tkinter import scrolledtext, ttk
except ImportError:
    print(
        "Error: python3-tk is not installed.  Install it with:\n"
        "  sudo apt install python3-tk        # Debian/Ubuntu\n"
        "  sudo dnf install python3-tkinter   # Fedora",
        file=sys.stderr,
    )
    sys.exit(1)


class WarpApp:
    """Tkinter-based GUI for controlling Cloudflare WARP."""

    _STATE_COLORS: dict[WarpState, str] = {
        WarpState.DISCONNECTED: "#dc3545",
        WarpState.CONNECTED: "#28a745",
        WarpState.CONNECTING: "#ffc107",
        WarpState.DISCONNECTING: "#ffc107",
        WarpState.UNKNOWN: "#6c757d",
        WarpState.ERROR: "#dc3545",
    }

    def __init__(self, cli: WarpCLI, logger: logging.Logger) -> None:
        self._cli = cli
        self._log = logger

        self._state: WarpState = WarpState.UNKNOWN
        self._detail: str = ""
        self._busy: bool = False
        self._stop = threading.Event()
        self._q: queue.Queue = queue.Queue()
        self._last_check_ts: float = 0.0

        self._build_ui()
        self._poll_queue()
        self._schedule_periodic_refresh()
        self._enqueue_status_refresh("Initial status")

    # -- GUI construction --------------------------------------------------

    def _build_ui(self) -> None:
        self.root = tk.Tk()
        self.root.title("Cloudflare WARP VPN")
        self.root.resizable(False, False)
        self.root.configure(padx=0, pady=0)

        try:
            icon = tk.PhotoImage(file=self._find_icon())
            self.root.iconphoto(True, icon)
        except (tk.TclError, Exception):
            pass

        f = ttk.Frame(self.root, padding="16")
        f.grid(row=0, column=0, sticky="nsew")

        # -- row 0: status indicator + label --
        self._indicator = tk.Canvas(
            f, width=36, height=36, highlightthickness=0
        )
        self._indicator.grid(row=0, column=0, padx=(0, 12), pady=(0, 2))

        self._indicator.bind("<Configure>", self._redraw_dot)
        self._dot = None

        self._status_lbl = ttk.Label(
            f, text="Status: Unknown", font=("", 16, "bold")
        )
        self._status_lbl.grid(row=0, column=1, sticky="w", pady=(0, 2))

        # -- row 1: detail --
        self._detail_lbl = ttk.Label(f, text="", font=("", 10))
        self._detail_lbl.grid(
            row=1, column=0, columnspan=2, sticky="w", padx=(48, 0), pady=(0, 10)
        )

        # -- row 2: separator --
        ttk.Separator(f, orient="horizontal").grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=(0, 10)
        )

        # -- row 3: buttons --
        bf = ttk.Frame(f)
        bf.grid(row=3, column=0, columnspan=2, pady=(0, 6), sticky="ew")

        self._connect_btn = ttk.Button(
            bf, text="Connect", command=self._on_connect, width=14
        )
        self._connect_btn.pack(side="left", padx=(0, 8))

        self._disconnect_btn = ttk.Button(
            bf, text="Disconnect", command=self._on_disconnect, width=14
        )
        self._disconnect_btn.pack(side="left", padx=(0, 8))

        self._refresh_btn = ttk.Button(
            bf, text="Refresh", command=self._on_refresh
        )
        self._refresh_btn.pack(side="left")

        # -- row 4: last-checked label --
        self._checked_lbl = ttk.Label(f, text="", font=("", 9), foreground="#888")
        self._checked_lbl.grid(
            row=4, column=0, columnspan=2, sticky="w", padx=(48, 0), pady=(0, 8)
        )

        # -- row 5: separator --
        ttk.Separator(f, orient="horizontal").grid(
            row=5, column=0, columnspan=2, sticky="ew", pady=(0, 6)
        )

        # -- row 6: log --
        ttk.Label(f, text="Event Log", font=("", 9)).grid(
            row=6, column=0, columnspan=2, sticky="w", pady=(0, 2)
        )

        self._log_area = scrolledtext.ScrolledText(
            f,
            width=62,
            height=12,
            font=("Monospace", 8),
            state="disabled",
            wrap="word",
            relief="flat",
            borderwidth=1,
        )
        self._log_area.grid(row=7, column=0, columnspan=2, pady=(0, 0), sticky="ew")

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._sync_buttons()
        self._draw_indicator()

    @staticmethod
    def _find_icon() -> str:
        candidates = [
            "/usr/share/icons/hicolor/48x48/apps/warp-vpn.png",
            "/usr/share/icons/hicolor/48x48/apps/cloudflare-warp.png",
            "/usr/share/pixmaps/warp-vpn.png",
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c
        return candidates[0]

    # -- indicator drawing -------------------------------------------------

    def _redraw_dot(self, _event: object = None) -> None:
        self._draw_indicator()

    def _draw_indicator(self) -> None:
        self._indicator.delete("all")
        cw = self._indicator.winfo_width() or 36
        ch = self._indicator.winfo_height() or 36
        r = min(cw, ch) // 2 - 2
        cx, cy = cw // 2, ch // 2
        color = self._STATE_COLORS.get(self._state, "#6c757d")

        # Main circle with dark border
        self._indicator.create_oval(
            cx - r, cy - r, cx + r, cy + r,
            fill=color, outline="#555555", width=1,
        )

    # -- state sync helpers (called from GUI thread only) ------------------

    def _set_state(self, state: WarpState, detail: str = "") -> None:
        self._state = state

        state_display = state.value
        display_detail = detail if detail and detail != state_display else ""

        self._status_lbl.config(text=state_display)
        self._detail_lbl.config(text=display_detail)
        self._draw_indicator()
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        if self._busy:
            for b in (self._connect_btn, self._disconnect_btn, self._refresh_btn):
                b.config(state="disabled")
            return

        self._refresh_btn.config(state="normal")

        if self._state == WarpState.CONNECTED:
            self._connect_btn.config(state="disabled")
            self._disconnect_btn.config(state="normal")
        elif self._state == WarpState.DISCONNECTED:
            self._connect_btn.config(state="normal")
            self._disconnect_btn.config(state="disabled")
        else:
            self._connect_btn.config(state="normal")
            self._disconnect_btn.config(state="normal")

    def _update_checked_label(self) -> None:
        if self._last_check_ts <= 0:
            self._checked_lbl.config(text="")
            return
        elapsed = time.time() - self._last_check_ts
        if elapsed < 60:
            text = f"Last checked: {int(elapsed)}s ago"
        else:
            text = f"Last checked: {int(elapsed // 60)}m {int(elapsed % 60)}s ago"
        self._checked_lbl.config(text=text)

    def _write_log(self, msg: str) -> None:
        now = datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")
        self._log_area.config(state="normal")
        self._log_area.insert("end", f"{now} | {msg}\n")
        self._log_area.see("end")
        self._log_area.config(state="disabled")

    # -- thread-safe message loop ------------------------------------------

    def _poll_queue(self) -> None:
        if self._stop.is_set():
            return
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "log":
                    self._write_log(payload)
                elif kind == "state":
                    self._set_state(*payload)
                elif kind == "busy":
                    self._busy = payload
                    self._sync_buttons()
                elif kind == "checked":
                    self._last_check_ts = payload
                elif kind == "error":
                    self._set_state(WarpState.ERROR, payload)
                    self._write_log(f"ERROR: {payload}")
                    self._busy = False
                    self._sync_buttons()
        except queue.Empty:
            pass
        except Exception as exc:
            self._log.error("Exception in _poll_queue: %s", exc, exc_info=True)
        finally:
            self._update_checked_label()
            self.root.after(QUEUE_POLL_MS, self._poll_queue)

    # -- periodic refresh --------------------------------------------------

    def _schedule_periodic_refresh(self) -> None:
        if self._stop.is_set():
            return
        try:
            self._enqueue_status_refresh("Periodic refresh")
        except Exception as exc:
            self._log.error("Failed to schedule refresh: %s", exc, exc_info=True)
        self.root.after(REFRESH_INTERVAL_MS, self._schedule_periodic_refresh)

    # -- background workers ------------------------------------------------

    def _enqueue_status_refresh(self, tag: str = "") -> None:
        def _work() -> None:
            try:
                state, detail = self._cli.status()
                self._q.put(("state", (state, detail)))
                self._q.put(("checked", time.time()))
            except Exception as exc:
                self._q.put(("state", (WarpState.ERROR, str(exc))))
                self._q.put(("log", f"Status check failed: {exc}"))

        threading.Thread(target=_work, daemon=True).start()

    def _do_action(self, action: str) -> None:
        if self._busy:
            return

        self._busy = True
        self._q.put(("busy", True))

        if action == "connect":
            target_state = WarpState.CONNECTED
            self._q.put(("state", (WarpState.CONNECTING, "Connecting to WARP...")))
        else:
            target_state = WarpState.DISCONNECTED
            self._q.put(("state", (WarpState.DISCONNECTING, "Disconnecting...")))

        def _work() -> None:
            try:
                self._q.put(("log", f"{action.capitalize()} command sent..."))
                if action == "connect":
                    self._cli.connect()
                else:
                    self._cli.disconnect()

                state, detail = self._cli.status()
                if state == target_state:
                    self._q.put(("log", f"{action.capitalize()} successful — {detail}"))
                else:
                    raise WarpCLIError(
                        f"Post-{action} state mismatch: expected {target_state.value}, "
                        f"got {state.value} ({detail})"
                    )
                self._q.put(("state", (state, detail)))
                self._q.put(("checked", time.time()))
            except Exception as exc:
                self._q.put(("error", str(exc)))
                self._q.put(("log", f"{action.capitalize()} failed: {exc}"))
            finally:
                self._q.put(("busy", False))

        threading.Thread(target=_work, daemon=True).start()

    # -- button callbacks --------------------------------------------------

    def _on_connect(self) -> None:
        if self._busy:
            return
        if self._state == WarpState.CONNECTED:
            self._q.put(("log", "Already connected."))
            return
        self._do_action("connect")

    def _on_disconnect(self) -> None:
        if self._busy:
            return
        if self._state == WarpState.DISCONNECTED:
            self._q.put(("log", "Already disconnected."))
            return
        self._do_action("disconnect")

    def _on_refresh(self) -> None:
        self._enqueue_status_refresh("Manual refresh")

    # -- shutdown ----------------------------------------------------------

    def _on_close(self) -> None:
        self._stop.set()
        self._log.info("Shutting down GUI")
        self.root.destroy()

    def run(self) -> None:
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            self._log.info("KeyboardInterrupt received")
            self._on_close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    lock_fd = _acquire_lock()
    logger = _setup_logging()
    logger.info("=" * 50)
    logger.info("WARP VPN GUI starting — PID %d, log: %s", os.getpid(), LOG_FILE)
    logger.debug("Lock file: %s", _get_lock_file())

    try:
        cli = WarpCLI(logger)
    except WarpCLIError as exc:
        logger.critical(str(exc))
        print(f"FATAL: {exc}", file=sys.stderr)
        _release_lock(lock_fd)
        sys.exit(1)

    try:
        app = WarpApp(cli, logger)
        app.run()
    except Exception as exc:
        logger.critical("Unhandled exception", exc_info=True)
        print(f"FATAL: Unhandled exception: {exc}", file=sys.stderr)
    finally:
        _release_lock(lock_fd)
        logger.info("WARP VPN GUI stopped")


if __name__ == "__main__":
    main()
