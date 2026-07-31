import sys
import subprocess
import os
import asyncio
from typing import Any

# The decky plugin module is located at decky-loader/plugin
# For easy intellisense checkout the decky-loader code one directory up
# or add the `decky-loader/plugin` path to `python.analysis.extraPaths` in `.vscode/settings.json`
import decky_plugin
from settings import SettingsManager

def get_all_children(pid: int) -> list[str]:
    pids = []
    tmpPids = [str(pid)]
    try:
        while tmpPids:
            ppid = tmpPids.pop(0)
            lines = []
            clean_env = os.environ.copy()
            clean_env["LD_LIBRARY_PATH"] = ""
            with subprocess.Popen(["ps", "--ppid", ppid, "-o", "pid="], stdout=subprocess.PIPE, env=clean_env) as p:
                lines = p.stdout.readlines()
            for chldPid in lines:
                chldPid = chldPid.strip()
                if not chldPid:
                    continue
                pids.append(chldPid)
                tmpPids.append(chldPid)
        return pids
    except:
        return pids

class Plugin:
    def __init__(self):
        self.stream_monitor_task = None
        self.streaming_active = True
        self.sunshine_paused_appid = None
        self.suspend_resume_task = None
        # The most recent pid we successfully SIGSTOP'd, regardless of
        # which feature called pause() - overlay-pause, focus-loss,
        # pause-before-suspend, or our own sunshine handler. Currently
        # informational only (kept for debugging); the resume-on-wake
        # logic uses focused_pid/sunshine_paused_appid instead, since
        # this can get clobbered by concurrent background-app pauses.
        self.last_paused_pid = None
        # True from the moment logind announces an impending suspend
        # until a short grace period after it announces resume. While
        # true, the sunshine CLIENT DISCONNECTED handler must not
        # auto-pause anything - a suspend commonly drops the stream too,
        # and pausing here would race/fight with the built-in
        # overlay-pause / pause-before-suspend mechanisms that are
        # already responsible for this transition. The post-resume grace
        # period additionally absorbs a CLIENT DISCONNECTED line that
        # Sunshine wrote while the log tailer was frozen during suspend
        # and only gets around to processing after the resume handler
        # already ran.
        self.system_suspending = False
        # Safety valve: if _on_resume never fires (dbus-monitor died,
        # missed the signal, etc), this guarantees system_suspending
        # doesn't stay stuck True forever, which would otherwise
        # permanently suppress streaming auto-pause.
        self.suspend_safety_task = None
        # Pushed from the frontend (backend.ts) every time its own
        # focus-change handler identifies the currently-focused game.
        # Lets the streaming-pause handler and the suspend/resume
        # handler use the exact same notion of "the active game" as the
        # rest of the plugin, instead of deriving their own.
        self.focused_pid = 0

    # Asyncio-compatible long-running code, executed in a task when the plugin is loaded
    async def _main(self):
        self.settings = SettingsManager(name="settings", settings_directory=decky_plugin.DECKY_PLUGIN_SETTINGS_DIR)
        self.settings.read()
        self.stream_monitor_task = asyncio.create_task(self.monitor_sunshine_logs())
        self.suspend_resume_task = asyncio.create_task(self.monitor_suspend_resume())

    # Function called first during the unload process, utilize this to handle your plugin being stopped, but not
    # completely removed
    async def _unload(self):
        if self.stream_monitor_task:
            self.stream_monitor_task.cancel()
        if self.suspend_resume_task:
            self.suspend_resume_task.cancel()
        if self.suspend_safety_task:
            self.suspend_safety_task.cancel()

    # Function called after `_unload` during uninstall, utilize this to clean up processes and other remnants of your
    # plugin that may remain on the system
    async def _uninstall(self):
        pass

    # Migrations that should be performed before entering `_main()`.
    async def _migration(self):
        pass

    # -------------------------------------------------------------------
    # Sunshine log tailing
    # -------------------------------------------------------------------
    # Sunshine can be restarted (e.g. around system sleep/resume, or after
    # a client disconnect/reconnect glitch), which either truncates
    # sunshine.log in place or recreates it at the same path with a new
    # inode. A naive "open once, seek to end, readline() forever" tail
    # will silently stop seeing new lines in that situation (no
    # exception is raised - the fd just parks at an EOF that no longer
    # advances). This loop detects both cases (inode/device change, or
    # size shrinking below our last read position) and reopens fresh.
    # -------------------------------------------------------------------

    async def monitor_sunshine_logs(self):
        home_dir = os.path.expanduser("~")
        log_path = os.path.join(home_dir, ".config/sunshine/sunshine.log")

        while True:
            try:
                await self._tail_sunshine_log_once(log_path)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                decky_plugin.logger.error(f"[pause-games] log monitor error: {e}")
                await asyncio.sleep(5)
            # Loop back around and reopen the log from scratch.

    async def _tail_sunshine_log_once(self, log_path: str):
        # Wait for the log file to exist (first boot, or Sunshine not started yet)
        while not os.path.exists(log_path):
            await asyncio.sleep(5)

        f = open(log_path, "r", buffering=1, errors="ignore")
        try:
            f.seek(0, os.SEEK_END)
            st = os.fstat(f.fileno())
            last_ino, last_dev = st.st_ino, st.st_dev
            last_pos = f.tell()

            while True:
                line = f.readline()
                if line:
                    last_pos = f.tell()
                    await self._handle_log_line(line)
                    continue

                # No new data right now - before waiting, make sure the
                # file we have open is still the file that's being
                # written to.
                await asyncio.sleep(0.2)

                try:
                    path_st = os.stat(log_path)
                except FileNotFoundError:
                    # File disappeared entirely (mid-rotation). Bail out
                    # and let the outer loop wait for it to reappear.
                    return

                rotated = (path_st.st_ino != last_ino) or (path_st.st_dev != last_dev)
                truncated = path_st.st_size < last_pos

                if rotated or truncated:
                    decky_plugin.logger.info(
                        "[pause-games] sunshine.log rotated/truncated, reopening"
                    )
                    return  # outer loop will call this function again
        finally:
            f.close()

    async def _handle_log_line(self, line: str):
        # ---------------------------------------------------------------
        # CLIENT CONNECTED (Streaming Resumed)
        # ---------------------------------------------------------------
        if "CLIENT CONNECTED" in line:
            self.streaming_active = True
            decky_plugin.logger.info("[pause-games] sunshine CLIENT CONNECTED")

            # Resume ONLY the active game that was specifically paused by
            # Sunshine (tracked via self.sunshine_paused_appid). We do NOT
            # broaden this to "resume whatever happens to be paused" -
            # Python has no visibility into the frontend's sticky_state
            # (backend.ts's appMetaDataMap), which is the only source of
            # truth for "the user deliberately paused this and it should
            # stay paused." Resuming indiscriminately here would override
            # that on every stream reconnect.
            if self.sunshine_paused_appid is not None:
                game_pid = await self.pid_from_appid(self.sunshine_paused_appid)
                if game_pid != 0:
                    disabled_games = set(self.settings.getSetting("noAutoPauseSet", []))

                    if self.sunshine_paused_appid not in disabled_games:
                        if await self.is_paused(game_pid):
                            decky_plugin.logger.info(
                                f"[pause-games] CLIENT CONNECTED: resuming pid {game_pid} "
                                f"(appid {self.sunshine_paused_appid}) that we paused on disconnect"
                            )
                            await self.resume(game_pid)

                self.sunshine_paused_appid = None

        # ---------------------------------------------------------------
        # CLIENT DISCONNECTED (Streaming Ended)
        # ---------------------------------------------------------------
        elif "CLIENT DISCONNECTED" in line:
            self.streaming_active = False
            decky_plugin.logger.info("[pause-games] sunshine CLIENT DISCONNECTED")

            # A suspend commonly drops the stream too. If one is in
            # progress (or we're still in the post-resume grace period),
            # the built-in overlay-pause / pause-before-suspend
            # mechanisms already own pausing for this transition - don't
            # also pause here, or the two can race/fight each other.
            if self.system_suspending:
                decky_plugin.logger.info(
                    "[pause-games] CLIENT DISCONNECTED suppressed: system_suspending is True"
                )
                return

            # Refresh settings cache from disk
            if hasattr(self.settings, "read"):
                self.settings.read()

            # Query the exact setting key used by the React frontend ("autoPause")
            raw_auto_pause = self.settings.getSetting("autoPause", True)

            # Type-safe truthiness check for booleans, strings ("true"/"1"), and ints
            if isinstance(raw_auto_pause, str):
                is_auto_pause_enabled = raw_auto_pause.lower() in ("true", "1", "yes")
            else:
                is_auto_pause_enabled = bool(raw_auto_pause)

            # If autoPause is disabled in the UI, do not pause the game
            if not is_auto_pause_enabled:
                return

            # Pause the game the frontend currently considers focused -
            # the same signal setupFocusChangeHandler itself uses, kept
            # up to date via set_focused_pid() - rather than deriving our
            # own notion of "active" via a pgrep heuristic.
            game_pid = self.focused_pid
            if game_pid and not await self.is_paused(game_pid):
                game_appid = await self.appid_from_pid(game_pid)
                disabled_games = set(self.settings.getSetting("noAutoPauseSet", []))

                if game_appid not in disabled_games:
                    if await self.pause(game_pid):
                        decky_plugin.logger.info(
                            f"[pause-games] CLIENT DISCONNECTED: paused pid {game_pid} (appid {game_appid})"
                        )
                        self.sunshine_paused_appid = game_appid

    # -------------------------------------------------------------------
    # OS-level suspend/resume detection (systemd-logind)
    # -------------------------------------------------------------------
    # We deliberately do not rely on Steam's own frontend suspend/resume
    # observable for this - it's proven unreliable in practice (Valve
    # removed the m_bResuming field from SuspendResumeStore in a July
    # 2026 Steam Client update, which is the frontend's own resume
    # signal). logind's PrepareForSleep D-Bus signal is the standard,
    # OS-level suspend/resume notification on Linux: it fires with
    # argument `true` right before the system suspends, and again with
    # `false` right as it resumes. It's independent of Steam's UI
    # process entirely.
    # -------------------------------------------------------------------

    async def monitor_suspend_resume(self):
        while True:
            try:
                await self._watch_prepare_for_sleep()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                decky_plugin.logger.error(f"[pause-games] suspend/resume monitor error: {e}")
                await asyncio.sleep(5)

    async def _watch_prepare_for_sleep(self):
        clean_env = os.environ.copy()
        clean_env["LD_LIBRARY_PATH"] = ""
        process = await asyncio.create_subprocess_exec(
            "dbus-monitor", "--system",
            "type='signal',interface='org.freedesktop.login1.Manager',member='PrepareForSleep'",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=clean_env,
        )
        decky_plugin.logger.info(
            f"[pause-games] dbus-monitor started (pid {process.pid}), watching for PrepareForSleep"
        )
        try:
            awaiting_bool = False
            while True:
                raw_line = await process.stdout.readline()
                if not raw_line:
                    decky_plugin.logger.warning(
                        "[pause-games] dbus-monitor exited unexpectedly, restarting"
                    )
                    break  # dbus-monitor exited; outer loop restarts it
                line = raw_line.decode(errors="ignore")

                if "member=PrepareForSleep" in line:
                    awaiting_bool = True
                    continue

                if awaiting_bool:
                    awaiting_bool = False
                    stripped = line.strip()
                    if stripped == "boolean true":
                        await self._on_suspend()
                    elif stripped == "boolean false":
                        await self._on_resume()
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def _on_suspend(self):
        decky_plugin.logger.info("[pause-games] PrepareForSleep(true): suspend starting")
        self.system_suspending = True
        if self.suspend_safety_task:
            self.suspend_safety_task.cancel()
        self.suspend_safety_task = asyncio.create_task(self._suspend_safety_reset())

    async def _suspend_safety_reset(self):
        try:
            await asyncio.sleep(30)
            decky_plugin.logger.warning(
                "[pause-games] no resume signal received within 30s of suspend - "
                "clearing system_suspending as a safety valve"
            )
            self.system_suspending = False
        except asyncio.CancelledError:
            pass

    async def _on_resume(self):
        decky_plugin.logger.info("[pause-games] PrepareForSleep(false): resume detected")
        if self.suspend_safety_task:
            self.suspend_safety_task.cancel()
            self.suspend_safety_task = None

        # Deliberately do NOT clear system_suspending yet. The sunshine
        # log tailer was frozen for the whole suspend duration same as
        # everything else, so a CLIENT DISCONNECTED line Sunshine wrote
        # during that window may still be sitting unread in the log,
        # and gets processed sometime after this handler runs (not
        # necessarily before). If we clear the suppression flag before
        # that happens, that stale disconnect line would see our
        # just-resumed game as "currently unpaused" and immediately
        # pause it right back.

        disabled_games = set(self.settings.getSetting("noAutoPauseSet", []))

        async def resume_if_paused(pid: int, source: str) -> None:
            if not pid:
                return
            appid = await self.appid_from_pid(pid)
            if appid in disabled_games:
                decky_plugin.logger.info(
                    f"[pause-games] ({source}) pid {pid} (appid {appid}) is in noAutoPauseSet, skipping resume"
                )
                return
            if await self.is_paused(pid):
                decky_plugin.logger.info(f"[pause-games] ({source}) resuming pid {pid} (appid {appid}) on wake")
                await self.resume(pid)
            else:
                decky_plugin.logger.info(f"[pause-games] ({source}) pid {pid} was already unpaused on wake")

        # Primary: the game the frontend currently considers focused -
        # deliberately not last_paused_pid, since onSuspend's own "pause
        # everything" pass in backend.ts can pause several apps
        # concurrently and last_paused_pid would end up pointing at
        # whichever of those calls landed last, not reliably the active
        # game. focused_pid only ever comes from the one place in
        # backend.ts that identifies the actually-focused game.
        if self.focused_pid:
            await resume_if_paused(self.focused_pid, "focused_pid")
        else:
            decky_plugin.logger.info("[pause-games] no focused_pid tracked")

        # Fallback: the game the streaming handler itself paused. Covers
        # e.g. suspending via SSH while Sunshine was still connected -
        # in that case CLIENT DISCONNECTED (not overlay-pause or
        # pause-before-suspend) is what paused the game, and
        # sunshine_paused_appid is the only record of that. Resolved and
        # resumed here too (resume_if_paused is a no-op if it's already
        # running, e.g. because it was the same app as focused_pid).
        if self.sunshine_paused_appid is not None:
            pid = await self.pid_from_appid(self.sunshine_paused_appid)
            await resume_if_paused(pid, "sunshine_paused_appid")
            self.sunshine_paused_appid = None

        # Grace period: give the sunshine log tailer a chance to catch up
        # on any backlog before we stop suppressing streaming-triggered
        # auto-pause. Any CLIENT DISCONNECTED it processes in this window
        # is treated as a stale echo of the same suspend event, not a
        # genuine new disconnect.
        await asyncio.sleep(10)
        self.system_suspending = False
        decky_plugin.logger.info("[pause-games] post-resume grace period over, system_suspending cleared")

    async def is_paused(self, pid: int) -> bool:
        try:
            clean_env = os.environ.copy()
            clean_env["LD_LIBRARY_PATH"] = ""
            with subprocess.Popen(["ps", "--ppid", str(pid), "-o", "stat="], stdout=subprocess.PIPE, env=clean_env) as p:
                return p.stdout.readline().lstrip().startswith(b'T')
        except:
            return False

    async def pause(self, pid: int) -> bool:
        pids = get_all_children(pid)
        if pids:
            command = ["kill", "-SIGSTOP"]
            command.extend(pids)
            try:
                clean_env = os.environ.copy()
                clean_env["LD_LIBRARY_PATH"] = ""
                ok = subprocess.run(command, stderr=sys.stderr, stdout=sys.stdout, env=clean_env).returncode == 0
                if ok:
                    self.last_paused_pid = pid
                return ok
            except:
                return False
        else:
            return False


    async def resume(self, pid: int) -> bool:
        pids = get_all_children(pid)
        if pids:
            command = ["kill", "-SIGCONT"]
            command.extend(pids)
            try:
                clean_env = os.environ.copy()
                clean_env["LD_LIBRARY_PATH"] = ""
                return subprocess.run(command, stderr=sys.stderr, stdout=sys.stdout, env=clean_env).returncode == 0
            except:
                return False
        else:
            return False

    async def terminate(self, pid: int) -> bool:
        pids = get_all_children(pid)
        if pids:
            command = ["kill", "-SIGTERM"]
            command.extend(pids)
            try:
                clean_env = os.environ.copy()
                clean_env["LD_LIBRARY_PATH"] = ""
                return subprocess.run(command, stderr=sys.stderr, stdout=sys.stdout, env=clean_env).returncode == 0
            except:
                return False
        else:
            return False

    async def kill(self, pid: int) -> bool:
        pids = get_all_children(pid)
        if pids:
            command = ["kill", "-SIGKILL"]
            command.extend(pids)
            try:
                clean_env = os.environ.copy()
                clean_env["LD_LIBRARY_PATH"] = ""
                return subprocess.run(command, stderr=sys.stderr, stdout=sys.stdout, env=clean_env).returncode == 0
            except:
                return False
        else:
            return False

    async def pid_from_appid(self, appid: int) -> int:
        pid = ""
        try:
            clean_env = os.environ.copy()
            clean_env["LD_LIBRARY_PATH"] = ""
            with subprocess.Popen(["pgrep", "--full", "--oldest", f"/reaper\\s.*\\bAppId={appid}\\b"], stdout=subprocess.PIPE, env=clean_env) as p:
                pid = p.stdout.read().strip()
        except:
            return 0
        if not pid:
            return 0
        return int(pid)

    async def appid_from_pid(self, pid: int) -> int:
        # search upwards for the process that has the AppId= command line argument
        while pid and pid != 1:
            try:
                args = []
                with open(f"/proc/{pid}/cmdline", "r") as f:
                    args = f.read().split('\0')
                for arg in args:
                    arg = arg.strip()
                    if arg.startswith("AppId="):
                        arg = arg.lstrip("AppId=")
                        if arg:
                            return int(arg)
            except:
                pass
            try:
                strppid = ""
                clean_env = os.environ.copy()
                clean_env["LD_LIBRARY_PATH"] = ""
                with subprocess.Popen(["ps", "--pid", str(pid), "-o", "ppid="], stdout=subprocess.PIPE, env=clean_env) as p:
                    strppid = p.stdout.read().strip()
                if strppid:
                    pid = int(strppid)
                else:
                    break
            except:
                break
        return 0

    async def load_settings(self) -> dict[str, Any]:
        return self.settings.settings

    async def set_focused_pid(self, pid: int) -> None:
        self.focused_pid = pid

    async def save_setting(self, key: str, value: Any) -> None:
        self.settings.setSetting(key, value)

    async def add_no_auto_pause_set(self, appid: int) -> None:
        curr = set(self.settings.getSetting("noAutoPauseSet", []))
        curr.add(appid)
        self.settings.setSetting("noAutoPauseSet", list(curr))

    async def remove_no_auto_pause_set(self, appid: int) -> None:
        curr = set(self.settings.getSetting("noAutoPauseSet", []))
        curr.discard(appid)
        self.settings.setSetting("noAutoPauseSet", list(curr))
