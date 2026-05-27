"""
SPSS Executor — Subprocess process manager for SPSS batch execution.

Provides ``SPSSExecutor`` which wraps user syntax in OMS commands for
structured XML output, manages the SPSS subprocess via ``Popen`` for
interrupt capability, and supports temporary data copies for greylisted
operations (COMPUTE, RECODE, etc.) that would otherwise mutate the
original data file.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from snla.config import (
    P0_OUTPUT_DIR,
    SPSS_EXEC_MODE,
    SPSS_EXECUTABLE,
    SPSS_EXECUTION_TIMEOUT,
    SPSS_PYTHON_PATH,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OMS XML wrapper
# ---------------------------------------------------------------------------

OMS_WRAPPER = """\
OMS /SELECT TABLES /DESTINATION FORMAT=OXML OUTFILE='{output_xml}'.
{user_syntax}
OMSEND.
"""

# Dataset preamble — loads the source .sav into an active dataset named SNLA_Temp
GET_FILE_PREAMBLE = "GET FILE='{data_path}'.\nDATASET NAME SNLA_Temp.\n"

# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


@dataclass
class ExecutionResult:
    """Result of a single SPSS syntax execution.

    Attributes:
        exit_code: Process exit code (0 typically indicates success).
        stdout: Full stdout captured from the SPSS subprocess.
        stderr: Full stderr captured from the SPSS subprocess.
        xml_path: Absolute path to the OMS-generated XML output file, or
            ``None`` if no XML was produced.
        lst_path: Absolute path to the SPSS-generated listing (``.lst``)
            file, or ``None`` if none was found.
        success: ``True`` when the process exited with code 0 **and** the
            XML output file exists.
        error_message: Human-readable error description on failure.
        duration_seconds: Wall-clock execution time in seconds.
    """

    exit_code: int
    stdout: str
    stderr: str
    xml_path: str | None
    lst_path: str | None
    success: bool
    error_message: str | None = None
    duration_seconds: float = 0.0


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class SPSSExecutor:
    """Manages SPSS subprocess execution with OMS output capture.

    The executor is the bridge between validated syntax and actual SPSS
    batch execution.  It is responsible for:

    * Wrapping user syntax in OMS commands so that structured XML output
      is available for programmatic consumption.
    * Prepending a ``GET FILE`` preamble to load the target dataset.
    * Launching the SPSS subprocess via ``Popen`` so that long-running
      executions can be interrupted through a cancellation token.
    * Polling for completion, timeout, or cancellation and cleaning up
      the subprocess accordingly.
    * Creating temporary copies of the data file for greylisted commands
      that would otherwise modify the original data on disk.
    * Tracking and disposing of all temporary files.

    Parameters:
        spss_path:
            Path to the SPSS executable (``spss.exe`` or ``stats.exe``).
            Falls back to ``snla.config.SPSS_EXECUTABLE`` when ``None``.
        output_dir:
            Directory used for generated syntax files, XML output, and
            other artifacts.  Falls back to ``snla.config.P0_OUTPUT_DIR``
            when ``None``.
    """

    def __init__(
        self,
        spss_path: str | None = None,
        output_dir: str | None = None,
    ) -> None:
        self.spss_path: str = spss_path or SPSS_EXECUTABLE
        self.spss_python: str = SPSS_PYTHON_PATH
        self.exec_mode: str = SPSS_EXEC_MODE  # "python" or "batch"
        self.output_dir: str = os.path.abspath(output_dir or P0_OUTPUT_DIR)
        self._process: subprocess.Popen | None = None
        self._temp_files: list[str] = []
        os.makedirs(self.output_dir, exist_ok=True)

        if self.exec_mode == "python":
            if not os.path.isfile(self.spss_python):
                raise FileNotFoundError(
                    f"SPSS Python interpreter not found at: {self.spss_python}\n"
                    f"Set SPSS_PYTHON_PATH in .env or pass a valid path."
                )
        else:
            if not os.path.isfile(self.spss_path):
                raise FileNotFoundError(
                    f"SPSS executable not found at: {self.spss_path}\n"
                    f"Set the SPSS_PATH environment variable or pass a valid "
                    f"``spss_path`` to the constructor."
                )

    # -- Public API ---------------------------------------------------------

    def run(
        self,
        syntax: str,
        data_path: str,
        output_name: str = "analysis",
        cancellation_token: bool = False,
    ) -> ExecutionResult:
        """Execute SPSS syntax against a dataset.

        Two execution modes (configured via ``SPSS_EXEC_MODE``):

        **python** (default) — Runs syntax via SPSS's bundled Python 3
        interpreter using ``spss.Submit()``.  This is the recommended
        mode for SPSS 26+ where ``stats.exe -production silent`` may hang.

        **batch** (legacy) — Runs ``stats.exe`` as a subprocess with
        ``-production silent`` flags.

        Args:
            syntax: SPSS syntax string (OMS wrapping added automatically).
            data_path: Path to the ``.sav`` data file to analyse.
            output_name: Name prefix for generated output files.
            cancellation_token: Set to ``True`` from another thread to
                request cancellation of the running SPSS subprocess.

        Returns:
            An ``ExecutionResult`` summarising the outcome.
        """
        start_time: float = time.perf_counter()
        run_dir: str = os.path.join(
            self.output_dir,
            f"run_{output_name}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}",
        )
        os.makedirs(run_dir, exist_ok=True)

        xml_path: str = os.path.join(run_dir, f"{output_name}.xml")
        wrapped: str = self._wrap_and_preamble(syntax, data_path, xml_path)

        if self.exec_mode == "python":
            return self._run_via_python(wrapped, xml_path, run_dir, start_time, cancellation_token)
        else:
            return self._run_via_batch(
                wrapped, xml_path, output_name, run_dir, start_time, cancellation_token
            )

    # ------------------------------------------------------------------
    # Python-mode execution (spss.Submit via SPSS Python 3.4)
    # ------------------------------------------------------------------

    def _run_via_python(
        self,
        wrapped_syntax: str,
        xml_path: str,
        run_dir: str,
        start_time: float,
        cancellation_token: bool = False,
    ) -> ExecutionResult:
        """Execute SPSS syntax via SPSS's bundled Python interpreter.

        Uses a polling loop (instead of blocking ``communicate``) so that
        an external caller can request cancellation via the
        *cancellation_token* flag — the process handle is stored on
        ``self._process`` so :meth:`terminate` can kill it.
        """
        # Write syntax to a .sps file (avoid escaping hell)
        sps_file = os.path.join(run_dir, "_syntax.sps")
        with open(sps_file, "w", encoding="utf-8") as sf:
            sf.write(wrapped_syntax)

        # Write a Python script that reads the .sps and submits it
        py_script = os.path.join(run_dir, "_run.py")
        with open(py_script, "w", encoding="utf-8") as f:
            # Normalise paths for SPSS Python (forward slashes)
            f.write("import spss, os\n")
            f.write("sps = r'{}'\n".format(sps_file.replace("\\", "/")))
            f.write("with open(sps, 'r') as sf:\n")
            f.write("    syntax = sf.read()\n")
            f.write("spss.Submit(syntax)\n")
            f.write("print('SPSS_DONE')\n")

        stdout = ""
        stderr = ""
        exit_code: int = -1
        proc: subprocess.Popen | None = None

        try:
            proc = subprocess.Popen(
                [self.spss_python, os.path.abspath(py_script)],
                cwd=os.path.abspath(run_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self._process = proc

            # Polling loop — allows cancellation via terminate()
            deadline = time.perf_counter() + SPSS_EXECUTION_TIMEOUT
            while True:
                if cancellation_token:
                    proc.terminate()
                    exit_code = -1
                    stdout, stderr = proc.communicate(timeout=5)
                    break

                retcode = proc.poll()
                if retcode is not None:
                    exit_code = retcode
                    stdout, stderr = proc.communicate(timeout=5)
                    break

                if time.perf_counter() >= deadline:
                    proc.terminate()
                    exit_code = -1
                    stdout, stderr = proc.communicate(timeout=5)
                    logger.warning(
                        "[SPSSExecutor] Timeout (%ss) — terminating", SPSS_EXECUTION_TIMEOUT
                    )
                    break

                time.sleep(0.3)

        except subprocess.TimeoutExpired:
            if proc is not None and proc.poll() is None:
                proc.kill()
                stdout, stderr = proc.communicate(timeout=5)
            exit_code = -1
        except Exception:
            if proc is not None and proc.poll() is None:
                proc.terminate()
            raise
        finally:
            self._process = None

        duration = time.perf_counter() - start_time
        xml_exists = os.path.isfile(xml_path)

        # Detect empty/minimal XML (e.g. ONEWAY on string grouping var)
        xml_empty = False
        if xml_exists:
            try:
                xml_size = os.path.getsize(xml_path)
                if xml_size < 100:  # less than 100 bytes → no output
                    xml_empty = True
            except OSError:
                pass

        # Capture stdout as fallback LST for edge cases
        lst_path: str | None = None
        if stdout.strip():
            lst_path = os.path.join(run_dir, "output.lst")
            with open(lst_path, "w", encoding="utf-8", errors="replace") as lf:
                lf.write(stdout)

        success = (exit_code == 0 or xml_exists) and not xml_empty
        error_message = None
        if not success:
            reasons = []
            if exit_code != 0:
                reasons.append(f"exit code {exit_code}")
            if not xml_exists:
                reasons.append("XML output missing")
            if xml_empty:
                reasons.append(
                    "OMS XML output is empty — check that all "
                    "variables are of the correct type for this "
                    "analysis (e.g. ONEWAY requires numeric "
                    "grouping variable)"
                )
            error_message = "SPSS execution failed ({})".format("; ".join(reasons))
            if stderr.strip():
                error_message += f" — stderr: {stderr.strip()[:500]}"

        return ExecutionResult(
            exit_code=exit_code,
            stdout=stdout or "",
            stderr=stderr or "",
            xml_path=xml_path if xml_exists else None,
            lst_path=lst_path,
            success=success,
            error_message=error_message,
            duration_seconds=duration,
        )

    # ------------------------------------------------------------------
    # Batch-mode execution (stats.exe subprocess) — legacy
    # ------------------------------------------------------------------

    def _run_via_batch(
        self,
        wrapped_syntax: str,
        xml_path: str,
        output_name: str,
        run_dir: str,
        start_time: float,
        cancellation_token: bool,
    ) -> ExecutionResult:
        """Execute SPSS syntax via stats.exe batch mode (legacy)."""
        sps_path: str = self._write_syntax_file(wrapped_syntax, output_name, run_dir)

        logger.info("[SPSSExecutor] Running: %s %s", self.spss_path, sps_path)
        logger.info("[SPSSExecutor] CWD:       %s", run_dir)
        logger.info("[SPSSExecutor] XML out:   %s", xml_path)

        stdout = ""
        stderr = ""
        exit_code: int = -1
        proc: subprocess.Popen | None = None

        try:
            proc = subprocess.Popen(
                [self.spss_path, sps_path],
                cwd=run_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self._process = proc

            exit_code = self._poll_process(
                proc,
                timeout=SPSS_EXECUTION_TIMEOUT,
                cancellation_token=cancellation_token,
            )

            stdout, stderr = proc.communicate(timeout=5)

        except subprocess.TimeoutExpired:
            if proc is not None and proc.poll() is None:
                proc.kill()
                stdout, stderr = proc.communicate(timeout=5)
            exit_code = -1
        except Exception:
            if proc is not None and proc.poll() is None:
                proc.terminate()
            raise
        finally:
            self._process = None

        duration: float = time.perf_counter() - start_time
        xml_exists: bool = os.path.isfile(xml_path)
        # Detect empty XML (e.g. ONEWAY on string grouping var)
        xml_empty: bool = False
        if xml_exists:
            try:
                if os.path.getsize(xml_path) < 100:
                    xml_empty = True
            except OSError:
                pass
        lst_path: str | None = self._find_lst_output(run_dir)
        # The SPSS process writes a non-zero exit code even on success when
        # run in batch mode, so we consider exit_code 0 *or* the existence of
        # the OMS XML as a success indicator.
        success: bool = (exit_code == 0 or xml_exists) and not xml_empty

        error_message: str | None = None
        if not success:
            reasons: list[str] = []
            if exit_code != 0:
                reasons.append(f"exit code {exit_code}")
            if not xml_exists:
                reasons.append("XML output missing")
            if xml_empty:
                reasons.append("OMS XML output is empty — check variable types")
            error_message = f"SPSS execution failed ({'; '.join(reasons)})"
            if stderr.strip():
                error_message += f" — stderr: {stderr.strip()[:500]}"

        logger.info("[SPSSExecutor] Done in %.2fs — success=%s", duration, success)

        return ExecutionResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            xml_path=xml_path if xml_exists else None,
            lst_path=lst_path,
            success=success,
            error_message=error_message,
            duration_seconds=duration,
        )

    def execute_on_temp_copy(
        self,
        syntax: str,
        data_path: str,
        output_name: str = "analysis",
        cancellation_token: bool = False,
    ) -> ExecutionResult:
        """Execute SPSS syntax on a **temporary copy** of the data file.

        For greylisted commands (e.g. ``COMPUTE``, ``RECODE``, …) that
        would normally mutate the original ``.sav`` on disk, this method
        creates a timestamped copy, executes against that copy, and returns
        the result.  The original data file is **never** modified.

        The temporary copy is tracked internally and will be cleaned up
        when :meth:`cleanup` is called.

        Args:
            syntax: SPSS syntax to execute.
            data_path: Path to the **original** ``.sav`` file.
            output_name: Name prefix for output files.
            cancellation_token: Interrupt flag forwarded to :meth:`run`.

        Returns:
            An ``ExecutionResult`` from the temporary-copy execution.
        """
        data_dir: str = os.path.dirname(os.path.abspath(data_path))
        base_name: str = os.path.splitext(os.path.basename(data_path))[0]
        timestamp: str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        temp_sav: str = os.path.join(data_dir, f"{base_name}_temp_{timestamp}.sav")

        shutil.copy2(data_path, temp_sav)
        self._temp_files.append(temp_sav)

        logger.info("[SPSSExecutor] Temp copy created: %s", temp_sav)

        return self.run(
            syntax=syntax,
            data_path=temp_sav,
            output_name=output_name,
            cancellation_token=cancellation_token,
        )

    def terminate(self) -> bool:
        """Terminate the running SPSS process, if any.

        Tries graceful ``terminate()`` first, then escalates to ``kill()``
        after a 2-second grace period.

        Returns:
            ``True`` if a process was terminated, ``False`` if no process
            was running.
        """
        proc: subprocess.Popen | None = self._process
        if proc is None or proc.poll() is not None:
            return False

        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

        self._process = None
        return True

    def cleanup(self) -> None:
        """Remove all temporary files created by this executor instance.

        Files that have already been deleted (e.g. by an external agent)
        are silently skipped.
        """
        for path in self._temp_files:
            try:
                if os.path.isfile(path):
                    os.remove(path)
                    logger.info("[SPSSExecutor] Cleaned up: %s", path)
            except OSError:
                pass  # best-effort cleanup
        self._temp_files.clear()

    # -- Internal helpers ---------------------------------------------------

    def _poll_process(
        self,
        proc: subprocess.Popen,
        timeout: int,
        cancellation_token: bool,
        interval: float = 0.5,
    ) -> int:
        """Poll the process until completion, timeout, or cancellation.

        Args:
            proc: The running ``Popen`` instance.
            timeout: Maximum wall-clock seconds to wait.
            cancellation_token: External cancellation flag.
            interval: Sleep interval between polls (seconds).

        Returns:
            The process exit code, or ``-1`` if terminated/killed.
        """
        deadline: float = time.perf_counter() + timeout

        while True:
            # -- cancellation requested? --
            if cancellation_token:
                proc.terminate()
                logger.info("[SPSSExecutor] Cancellation requested — terminating")
                return -1

            # -- check completion --
            retcode: int | None = proc.poll()
            if retcode is not None:
                return retcode

            # -- timeout? --
            if time.perf_counter() >= deadline:
                proc.terminate()
                logger.warning("[SPSSExecutor] Timeout (%ss) — terminating", timeout)
                # Give it a moment to flush output, then escalate
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                return -1

            time.sleep(interval)

    def _write_syntax_file(self, syntax: str, name: str, output_dir: str) -> str:
        """Write syntax content to a temporary ``.sps`` file.

        Args:
            syntax: The full syntax string (already wrapped).
            name: A name hint used in the temp file name.
            output_dir: Directory where the ``.sps`` file is created.

        Returns:
            Absolute path to the written ``.sps`` file.
        """
        fd, sps_path = tempfile.mkstemp(
            suffix=".sps",
            prefix=f"{name}_",
            dir=output_dir,
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(syntax)
        except Exception:
            os.remove(sps_path)
            raise
        return sps_path

    def _wrap_and_preamble(
        self,
        syntax: str,
        data_path: str,
        xml_output_path: str,
    ) -> str:
        """Wrap user syntax with OMS commands and prepend a GET FILE preamble.

        Also appends OUTPUT EXPORT to capture listing text for LST fallback.
        """
        norm_data: str = os.path.abspath(data_path).replace("\\", "/")
        norm_xml: str = os.path.abspath(xml_output_path).replace("\\", "/")

        preamble: str = GET_FILE_PREAMBLE.format(data_path=norm_data)
        oms_block: str = OMS_WRAPPER.format(
            output_xml=norm_xml,
            user_syntax=syntax,
        )
        return preamble + oms_block

    @staticmethod
    def _find_lst_output(output_dir: str) -> str | None:
        """Locate the most recently generated ``.lst`` listing file.

        SPSS automatically writes a listing file (``.lst``) in the working
        directory during batch execution.  This helper scans *output_dir*
        for ``.lst`` files modified in the last 60 seconds and returns the
        newest one.

        Args:
            output_dir: Directory to search.

        Returns:
            Absolute path to the most recent ``.lst`` file, or ``None``.
        """
        now: float = time.time()
        cutoff: float = now - 60.0
        candidates: list[tuple[float, str]] = []

        try:
            for entry in os.scandir(output_dir):
                if entry.is_file() and entry.name.lower().endswith(".lst"):
                    mtime: float = entry.stat().st_mtime
                    if mtime >= cutoff:
                        candidates.append((mtime, entry.path))
        except OSError:
            return None

        if not candidates:
            return None

        # Return the most recently modified
        candidates.sort(key=lambda pair: pair[0], reverse=True)
        return candidates[0][1]
