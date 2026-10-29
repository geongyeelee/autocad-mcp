import json
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("AutoCADMCPServer")

AUTOCAD_APP_PATH = os.environ.get(
    "AUTOCAD_APP_PATH",
    "/Applications/Autodesk/AutoCAD 2027/AutoCAD 2027.app",
)
ACCORECONSOLE_PATH = os.environ.get(
    "ACCORECONSOLE_PATH",
    f"{AUTOCAD_APP_PATH}/Contents/Helpers/AcCoreConsole.app/Contents/MacOS/AcCoreConsole",
)
BRIDGE_DIR = Path(__file__).resolve().parent.parent.parent / "bridge"
HELPERS_LSP_PATH = BRIDGE_DIR / "mcp_helpers.lsp"
BRIDGE_CRX_PATH = BRIDGE_DIR / "autocad_mcp_bridge.crx"
RAW_LOG_DIR = Path("/tmp/autocad_mcp_logs")


def new_request_id(prefix: str = "req") -> str:
    return f"{prefix}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"


def decode_bytes_with_diagnostics(raw_bytes: bytes) -> Tuple[str, str, bool]:
    """Decode bytes safely across UTF-8, UTF-16LE (AcCoreConsole stdout), and GB18030 (ANSI_936).

    Returns:
        (decoded_text, encoding_used, used_fallback)
    """
    if not raw_bytes:
        return "", "utf-8", False

    # AcCoreConsole on macOS frequently emits UTF-16LE with null bytes on stdout
    if b"\x00" in raw_bytes[:128]:
        try:
            text16 = raw_bytes.decode("utf-16-le", errors="replace").replace("\x00", "")
            return text16, "utf-16-le", False
        except Exception:
            pass

    try:
        return raw_bytes.decode("utf-8"), "utf-8", False
    except UnicodeDecodeError:
        pass

    try:
        return raw_bytes.decode("gb18030"), "gb18030", True
    except UnicodeDecodeError:
        pass

    return raw_bytes.decode("utf-8", errors="replace"), "utf-8-replace", True


def save_raw_diagnostic_log(
    request_id: str,
    header_info: Dict[str, Any],
    stdout_bytes: bytes = b"",
    stderr_bytes: bytes = b"",
    resp_bytes: bytes = b"",
) -> str:
    """Persist raw console/bridge diagnostics to /tmp/autocad_mcp_logs/<request_id>.log."""
    try:
        RAW_LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = RAW_LOG_DIR / f"{request_id}.log"
        stdout_text, stdout_enc, _ = decode_bytes_with_diagnostics(stdout_bytes)
        stderr_text, stderr_enc, _ = decode_bytes_with_diagnostics(stderr_bytes)
        resp_text, resp_enc, _ = decode_bytes_with_diagnostics(resp_bytes)
        content = [
            f"=== AutoCAD MCP Diagnostic Log ({request_id}) ===",
            json.dumps(header_info, ensure_ascii=False, indent=2),
            f"\n--- STDOUT (encoding={stdout_enc}, bytes={len(stdout_bytes)}) ---",
            stdout_text[-4000:],
            f"\n--- STDERR (encoding={stderr_enc}, bytes={len(stderr_bytes)}) ---",
            stderr_text[-4000:],
            f"\n--- RESPONSE PAYLOAD (encoding={resp_enc}, bytes={len(resp_bytes)}) ---",
            resp_text[-4000:],
        ]
        log_path.write_text("\n".join(content), encoding="utf-8", errors="replace")
        return str(log_path)
    except Exception as e:
        logger.warning(f"Failed to write diagnostic log: {e}")
        return ""


def ensure_diagnostic_fields(
    data: Dict[str, Any],
    *,
    requested_mode: str = "auto",
    actual_mode: str = "live_gui",
    fallback_reason: Optional[str] = None,
    request_id: Optional[str] = None,
    elapsed_ms: Optional[int] = None,
    execution_stage: Optional[str] = None,
) -> Dict[str, Any]:
    """Ensure all 17 unified diagnostic fields exist on the returned dictionary."""
    ok = bool(data.get("ok", False))
    data["requested_mode"] = data.get("requested_mode") or requested_mode
    data["actual_mode"] = data.get("actual_mode") or data.get("mode") or actual_mode
    data["mode"] = data["actual_mode"]
    if fallback_reason is not None or "fallback_reason" not in data:
        data["fallback_reason"] = fallback_reason
    data["request_id"] = data.get("request_id") or request_id or new_request_id("mcp")
    if elapsed_ms is not None and "elapsed_ms" not in data:
        data["elapsed_ms"] = int(elapsed_ms)
    else:
        data.setdefault("elapsed_ms", 0)

    if execution_stage is not None and "execution_stage" not in data:
        data["execution_stage"] = execution_stage
    else:
        data.setdefault("execution_stage", "completed" if ok else "failed")

    data.setdefault("active_document", None)
    data.setdefault("command_names", "")
    data.setdefault("busy", False)
    data.setdefault("modal_dialog", False)
    data.setdefault("script_success", ok)
    data.setdefault("command_success", ok)
    data.setdefault("output_exists", None)
    data.setdefault("output_size", None)
    data.setdefault("reopen_verified", None)
    data.setdefault("error_code", None if ok else "EXECUTION_FAILED")
    data.setdefault("raw_log_path", None)
    return data


def _find_default_template_dwt() -> Optional[Path]:
    base = Path.home() / "Library/Application Support/Autodesk/AutoCAD 2027/R26.0/local"
    if base.exists():
        for dwt in sorted(base.glob("@*@/Template/acadiso.dwt")):
            return dwt
    app_dwt = Path(AUTOCAD_APP_PATH) / "Contents/Resources/WaterMark/acadiso.dwt"
    if app_dwt.exists():
        return app_dwt
    return None


def is_autocad_gui_running() -> bool:
    """Check if the AutoCAD 2027 GUI process is currently running."""
    try:
        res = subprocess.run(
            ["pgrep", "-f", "AutoCAD 2027.app/Contents/MacOS/AutoCAD"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        return res.returncode == 0 and bool(res.stdout.strip())
    except Exception:
        return False


def ensure_bootstrap_dwg(target_dwg: Optional[str] = None) -> str:
    """Ensure a valid DWG file exists to trigger document load in AutoCAD 2027."""
    dwg_path = Path(target_dwg or "/tmp/autocad_mcp_live_workspace.dwg")
    if not dwg_path.exists():
        tpl = _find_default_template_dwt()
        if tpl and tpl.exists():
            shutil.copyfile(tpl, dwg_path)
    return str(dwg_path)


@dataclass
class AutoCADLiveConnection:
    host: str = "127.0.0.1"
    port: int = 9877
    sock: Optional[socket.socket] = None
    _req_id: int = 0

    def connect(self, timeout: float = 1.5) -> bool:
        """Connect to the in-process AutoCAD 2027 TCP bridge."""
        if self.sock is not None:
            try:
                self.sock.settimeout(0.2)
                self.sock.send(b"")
                return True
            except Exception:
                self.disconnect()

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.settimeout(timeout)
            s.connect((self.host, self.port))
            self.sock = s
            return True
        except Exception:
            try:
                s.close()
            except Exception:
                pass
            self.sock = None
            return False

    def disconnect(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            finally:
                self.sock = None

    def bootstrap_live_bridge(self, launch_if_needed: bool = True, wait_seconds: float = 8.0) -> bool:
        """Bootstrap the live TCP bridge inside AutoCAD 2027 by opening a workspace drawing."""
        if self.connect(timeout=0.5):
            return True

        if not is_autocad_gui_running() and not launch_if_needed:
            return False

        dwg_path = ensure_bootstrap_dwg()
        try:
            subprocess.run(
                ["open", "-a", AUTOCAD_APP_PATH, dwg_path],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except Exception as e:
            logger.warning(f"Failed to invoke 'open -a AutoCAD 2027': {e}")
            return False

        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            if self.connect(timeout=0.5):
                logger.info("Successfully connected to live AutoCAD 2027 bridge after bootstrap")
                return True
            time.sleep(0.3)

        return False

    def request(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 20.0,
        auto_bootstrap: bool = True,
    ) -> Dict[str, Any]:
        """Send a JSON-RPC request to the AutoCAD 2027 bridge and return the result."""
        if not self.connect():
            if not auto_bootstrap or not self.bootstrap_live_bridge(launch_if_needed=True):
                raise ConnectionError(
                    "Could not connect to AutoCAD 2027 live bridge on "
                    f"{self.host}:{self.port}."
                )

        self._req_id += 1
        req_id = self._req_id
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": req_id,
        }
        raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")

        for attempt in range(2):
            try:
                assert self.sock is not None
                self.sock.settimeout(timeout)
                self.sock.sendall(raw)

                buf = b""
                resp = None
                while True:
                    chunk = self.sock.recv(8192)
                    if not chunk:
                        raise ConnectionError("AutoCAD bridge closed connection unexpectedly")
                    buf += chunk
                    text, _, _ = decode_bytes_with_diagnostics(buf)
                    text = text.strip()
                    try:
                        resp = json.loads(text)
                        break
                    except json.JSONDecodeError as je:
                        if text.endswith("}") and ',"id":' in text:
                            raise RuntimeError(f"Invalid JSON returned from AutoCAD bridge: {text[:300]}") from je
                        continue

                if "error" in resp and resp["error"]:
                    err = resp["error"]
                    msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                    raise RuntimeError(f"AutoCAD bridge RPC error: {msg}")
                return resp.get("result", {})
            except (socket.timeout, ConnectionError, BrokenPipeError, OSError) as e:
                self.disconnect()
                if attempt == 0 and self.connect():
                    continue
                raise ConnectionError(f"Communication with AutoCAD 2027 failed: {e}") from e

        raise ConnectionError("Failed to communicate with AutoCAD 2027 bridge")

    def ping(self) -> Dict[str, Any]:
        return self.request("ping", {}, timeout=3.5, auto_bootstrap=False)

    def status(self, auto_bootstrap: bool = False) -> Dict[str, Any]:
        try:
            res = self.request("status", {}, timeout=3.5, auto_bootstrap=auto_bootstrap)
        except RuntimeError:
            res = self.request("ping", {}, timeout=3.5, auto_bootstrap=auto_bootstrap)
        if not res or "drawing_ready" not in res:
            res.setdefault("bridge_online", True)
            res.setdefault("main_thread_responsive", True)
            res.setdefault("drawing_ready", True)
            res.setdefault("command_executable", True)
            res.setdefault("busy", False)
            res.setdefault("modal_dialog", False)
            res.setdefault("command_names", "")
            res.setdefault("active_document", "")
        return res

    def eval_lisp(
        self,
        code: str,
        timeout_ms: int = 15000,
        auto_cancel: bool = False,
        auto_bootstrap: bool = True,
    ) -> Dict[str, Any]:
        return self.request(
            "eval_lisp",
            {"code": code, "timeout_ms": timeout_ms, "auto_cancel": auto_cancel},
            timeout=(timeout_ms / 1000.0) + 5.0,
            auto_bootstrap=auto_bootstrap,
        )

    def post_command(self, command: str) -> Dict[str, Any]:
        return self.request("post_command", {"command": command}, timeout=5.0)


_CONSOLE_ERROR_PATTERNS = [
    (re.compile(r"找不到指定的图形文件|Cannot find the specified drawing file", re.I), "INPUT_DRAWING_NOT_FOUND"),
    (re.compile(r";\s*error:\s*([^\r\n]+)", re.I), "AUTOLISP_CONSOLE_ERROR"),
    (re.compile(r"Invalid option keyword|无效的选项关键字", re.I), "INVALID_COMMAND_OPTION"),
    (re.compile(r"Unknown command|未知命令", re.I), "UNKNOWN_AUTOCAD_COMMAND"),
]


def _scan_console_errors(stdout_text: str, stderr_text: str) -> Optional[Tuple[str, str]]:
    combined = f"{stdout_text}\n{stderr_text}"
    for pattern, code in _CONSOLE_ERROR_PATTERNS:
        m = pattern.search(combined)
        if m:
            snippet = m.group(0).strip()
            return code, snippet
    return None


class AcCoreConsoleRunner:
    """Headless batch runner using AutoCAD 2027's AcCoreConsole."""

    def __init__(self, console_path: str = ACCORECONSOLE_PATH):
        self.console_path = console_path

    def is_available(self) -> bool:
        return Path(self.console_path).is_file()

    def verify_dwg_reopen(self, dwg_path: str, timeout: float = 15.0) -> Dict[str, Any]:
        """Verify a saved DWG file on disk by checking its size/header and reopening it in AcCoreConsole."""
        p = Path(dwg_path).expanduser().resolve()
        if not p.is_file():
            return {
                "output_exists": False,
                "output_size": 0,
                "reopen_verified": False,
                "error_code": "OUTPUT_FILE_MISSING",
                "error": f"Saved DWG file does not exist on disk: {p}",
            }

        size = p.stat().st_size
        if size <= 128:
            return {
                "output_exists": True,
                "output_size": size,
                "reopen_verified": False,
                "error_code": "OUTPUT_FILE_TOO_SMALL",
                "error": f"Saved DWG file is unexpectedly small ({size} bytes): {p}",
            }

        try:
            with p.open("rb") as f:
                header = f.read(6)
            if not header.startswith(b"AC10"):
                return {
                    "output_exists": True,
                    "output_size": size,
                    "reopen_verified": False,
                    "error_code": "INVALID_DWG_HEADER",
                    "error": f"File does not have a valid DWG header ({header!r}): {p}",
                }
        except Exception as e:
            return {
                "output_exists": True,
                "output_size": size,
                "reopen_verified": False,
                "error_code": "OUTPUT_READ_ERROR",
                "error": f"Could not read saved DWG header: {e}",
            }

        # Reopen inside AcCoreConsole to confirm AutoCAD loads the file without code 76
        reopen_res = self.eval_lisp_headless(
            lisp_code="(mcp:get-doc-info)",
            input_dwg=str(p),
            save_as_dwg=None,
            verify_reopen=False,
            timeout=timeout,
        )
        if not reopen_res.get("ok"):
            return {
                "output_exists": True,
                "output_size": size,
                "reopen_verified": False,
                "error_code": reopen_res.get("error_code") or "REOPEN_VERIFICATION_FAILED",
                "error": f"Reopen verification in AcCoreConsole failed: {reopen_res.get('error')}",
                "raw_log_path": reopen_res.get("raw_log_path"),
            }

        doc_info = reopen_res.get("result") if isinstance(reopen_res.get("result"), dict) else {}
        return {
            "output_exists": True,
            "output_size": size,
            "reopen_verified": True,
            "reopened_entity_count": doc_info.get("entity_count"),
            "reopened_dwgname": doc_info.get("dwgname"),
        }

    def eval_lisp_headless(
        self,
        lisp_code: str,
        input_dwg: Optional[str] = None,
        save_as_dwg: Optional[str] = None,
        verify_reopen: bool = True,
        timeout: float = 30.0,
    ) -> Dict[str, Any]:
        """Run AutoLISP code inside headless AcCoreConsole and return structured JSON output."""
        t0 = time.time()
        req_id = new_request_id("headless")

        if not self.is_available():
            return ensure_diagnostic_fields(
                {
                    "ok": False,
                    "error_code": "ACCORECONSOLE_NOT_FOUND",
                    "error": f"AcCoreConsole not found at {self.console_path}",
                },
                requested_mode="accoreconsole",
                actual_mode="accoreconsole",
                request_id=req_id,
                elapsed_ms=int((time.time() - t0) * 1000),
                execution_stage="process_start",
            )

        if input_dwg:
            in_path = Path(input_dwg).expanduser().resolve()
            if not in_path.is_file():
                return ensure_diagnostic_fields(
                    {
                        "ok": False,
                        "error_code": "INPUT_DRAWING_NOT_FOUND",
                        "error": f"Input drawing file does not exist: {in_path}",
                    },
                    requested_mode="accoreconsole",
                    actual_mode="accoreconsole",
                    request_id=req_id,
                    elapsed_ms=int((time.time() - t0) * 1000),
                    execution_stage="precheck_failed",
                )

        with tempfile.TemporaryDirectory(prefix="autocad_mcp_batch_") as tmpdir:
            tmp_path = Path(tmpdir)
            req_lsp = tmp_path / "request.lsp"
            resp_json = tmp_path / "response.json"
            scr_file = tmp_path / "runner.scr"

            abs_save_path: Optional[Path] = None
            if save_as_dwg:
                abs_save_path = Path(save_as_dwg).expanduser().resolve()
                abs_save_path.parent.mkdir(parents=True, exist_ok=True)
                abs_save_path.unlink(missing_ok=True)
                abs_save = str(abs_save_path)
                escaped_save = abs_save.replace("\\", "\\\\").replace('"', '\\"')
                full_lisp = (
                    f"(setq __mcp_user_res__ (progn\n{lisp_code}\n))\n"
                    f'(setvar "FILEDIA" 0)\n'
                    f'(setvar "EXPERT" 5)\n'
                    f'(setvar "DELOBJ" 0)\n'
                    f'(command "_.-WBLOCK" "{escaped_save}" "*")\n'
                    f"__mcp_user_res__\n"
                )
            else:
                full_lisp = lisp_code

            req_lsp.write_text(full_lisp, encoding="utf-8")

            scr_lines = [
                "_.SECURELOAD 0",
                f'(load "{HELPERS_LSP_PATH}")',
                f'(mcp:exec-file "{req_lsp}" "{resp_json}")',
                "_.QUIT _Y",
            ]
            scr_file.write_text("\n".join(scr_lines) + "\n", encoding="utf-8")

            cmd = [self.console_path]
            if input_dwg:
                abs_in = str(Path(input_dwg).expanduser().resolve())
                cmd.extend(["/i", abs_in])
            cmd.extend(["/s", str(scr_file)])

            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired as te:
                raw_log = save_raw_diagnostic_log(
                    req_id,
                    {"cmd": cmd, "input_dwg": input_dwg, "save_as_dwg": save_as_dwg, "timeout": timeout},
                    stdout_bytes=te.stdout or b"",
                    stderr_bytes=te.stderr or b"",
                )
                return ensure_diagnostic_fields(
                    {
                        "ok": False,
                        "error_code": "ACCORECONSOLE_TIMEOUT",
                        "error": f"AcCoreConsole timed out after {timeout}s",
                        "raw_log_path": raw_log,
                    },
                    requested_mode="accoreconsole",
                    actual_mode="accoreconsole",
                    request_id=req_id,
                    elapsed_ms=int((time.time() - t0) * 1000),
                    execution_stage="script_execution",
                )

            stdout_text, stdout_enc, stdout_fb = decode_bytes_with_diagnostics(proc.stdout)
            stderr_text, stderr_enc, stderr_fb = decode_bytes_with_diagnostics(proc.stderr)
            resp_bytes = resp_json.read_bytes() if resp_json.exists() else b""
            resp_text, resp_enc, resp_fb = decode_bytes_with_diagnostics(resp_bytes)

            raw_log = save_raw_diagnostic_log(
                req_id,
                {
                    "cmd": cmd,
                    "returncode": proc.returncode,
                    "input_dwg": input_dwg,
                    "save_as_dwg": save_as_dwg,
                    "encodings": {
                        "stdout": stdout_enc,
                        "stderr": stderr_enc,
                        "response_json": resp_enc,
                    },
                },
                stdout_bytes=proc.stdout,
                stderr_bytes=proc.stderr,
                resp_bytes=resp_bytes,
            )

            if resp_bytes:
                try:
                    data = json.loads(resp_text)
                    data["mode"] = "accoreconsole"
                    data["actual_mode"] = "accoreconsole"
                    data["request_id"] = req_id
                    data["elapsed_ms"] = int((time.time() - t0) * 1000)
                    data["raw_log_path"] = raw_log
                    data["response_encoding"] = resp_enc
                    if resp_fb or stdout_fb or stderr_fb:
                        data["encoding_fallback_used"] = True

                    # Check if console output reported a command-level error
                    console_err = _scan_console_errors(stdout_text, stderr_text)
                    if console_err and data.get("ok"):
                        err_code, snippet = console_err
                        data["ok"] = False
                        data["command_success"] = False
                        data["error_code"] = err_code
                        data["execution_stage"] = "command_failed"
                        data["error"] = f"AutoCAD console reported command error: {snippet}"

                    # Check DXFIN special case: if code explicitly invoked DXFIN and entity count stayed 0
                    if "DXFIN" in lisp_code.upper() and data.get("entity_count_after", 0) == 0:
                        data["ok"] = False
                        data["command_success"] = False
                        data["error_code"] = "DXFIN_ZERO_ENTITIES"
                        data["execution_stage"] = "output_verification_failed"
                        data["error"] = (
                            "DXFIN executed in AcCoreConsole produced 0 entities "
                            "(DXFIN requires an empty drawing prior to entity creation or valid DXF path)."
                        )

                    # Verify saved output file if save_as_dwg was requested
                    if abs_save_path is not None:
                        if verify_reopen:
                            vinfo = self.verify_dwg_reopen(str(abs_save_path))
                        else:
                            exists = abs_save_path.is_file()
                            sz = abs_save_path.stat().st_size if exists else 0
                            vinfo = {
                                "output_exists": exists,
                                "output_size": sz,
                                "reopen_verified": None,
                            }
                            if not exists or sz <= 128:
                                vinfo["error_code"] = "OUTPUT_FILE_MISSING"
                                vinfo["error"] = f"Saved DWG missing or empty at {abs_save_path}"

                        data["output_exists"] = vinfo.get("output_exists", False)
                        data["output_size"] = vinfo.get("output_size", 0)
                        data["reopen_verified"] = vinfo.get("reopen_verified")
                        if "reopened_entity_count" in vinfo:
                            data["reopened_entity_count"] = vinfo["reopened_entity_count"]

                        if not vinfo.get("output_exists") or (
                            verify_reopen and not vinfo.get("reopen_verified")
                        ):
                            data["ok"] = False
                            data["command_success"] = False
                            data["saved_dwg"] = None
                            data["execution_stage"] = "output_verification_failed"
                            data["error_code"] = vinfo.get("error_code") or "SAVE_VERIFICATION_FAILED"
                            data["error"] = vinfo.get("error") or f"Failed to verify saved DWG at {abs_save_path}"
                        else:
                            data["saved_dwg"] = str(abs_save_path)
                            if data.get("ok"):
                                data["execution_stage"] = "output_verified"

                    return ensure_diagnostic_fields(
                        data,
                        requested_mode="accoreconsole",
                        actual_mode="accoreconsole",
                        request_id=req_id,
                    )
                except Exception as e:
                    return ensure_diagnostic_fields(
                        {
                            "ok": False,
                            "script_success": False,
                            "command_success": False,
                            "error_code": "JSON_PARSE_OR_DECODE_ERROR",
                            "error": f"Failed to parse AcCoreConsole JSON output (encoding={resp_enc}): {e}",
                            "response_encoding": resp_enc,
                            "raw_log_path": raw_log,
                            "stdout": stdout_text[-1000:],
                        },
                        requested_mode="accoreconsole",
                        actual_mode="accoreconsole",
                        request_id=req_id,
                        elapsed_ms=int((time.time() - t0) * 1000),
                        execution_stage="response_parse_failed",
                    )

            console_err = _scan_console_errors(stdout_text, stderr_text)
            err_code = console_err[0] if console_err else "NO_RESPONSE_JSON"
            err_detail = f" ({console_err[1]})" if console_err else ""
            return ensure_diagnostic_fields(
                {
                    "ok": False,
                    "script_success": False,
                    "command_success": False,
                    "error_code": err_code,
                    "error": f"AcCoreConsole did not produce a response JSON file{err_detail}",
                    "returncode": proc.returncode,
                    "raw_log_path": raw_log,
                    "stdout": stdout_text[-1500:],
                    "stderr": stderr_text[-1000:],
                },
                requested_mode="accoreconsole",
                actual_mode="accoreconsole",
                request_id=req_id,
                elapsed_ms=int((time.time() - t0) * 1000),
                execution_stage="script_execution_failed",
            )

    def run_script_file(
        self,
        script_content: str,
        input_dwg: Optional[str] = None,
        save_as_dwg: Optional[str] = None,
        verify_reopen: bool = True,
        timeout: float = 45.0,
    ) -> Dict[str, Any]:
        """Run a raw AutoCAD .scr script file in AcCoreConsole with save/output verification."""
        t0 = time.time()
        req_id = new_request_id("scr")

        if not self.is_available():
            return ensure_diagnostic_fields(
                {
                    "ok": False,
                    "error_code": "ACCORECONSOLE_NOT_FOUND",
                    "error": f"AcCoreConsole not found at {self.console_path}",
                },
                requested_mode="accoreconsole",
                actual_mode="accoreconsole",
                request_id=req_id,
                elapsed_ms=int((time.time() - t0) * 1000),
                execution_stage="process_start",
            )

        if input_dwg:
            in_path = Path(input_dwg).expanduser().resolve()
            if not in_path.is_file():
                return ensure_diagnostic_fields(
                    {
                        "ok": False,
                        "error_code": "INPUT_DRAWING_NOT_FOUND",
                        "error": f"Input drawing file does not exist: {in_path}",
                    },
                    requested_mode="accoreconsole",
                    actual_mode="accoreconsole",
                    request_id=req_id,
                    elapsed_ms=int((time.time() - t0) * 1000),
                    execution_stage="precheck_failed",
                )

        with tempfile.TemporaryDirectory(prefix="autocad_mcp_scr_") as tmpdir:
            scr_file = Path(tmpdir) / "batch.scr"
            body = script_content.rstrip()

            abs_save_path: Optional[Path] = None
            if save_as_dwg:
                abs_save_path = Path(save_as_dwg).expanduser().resolve()
                abs_save_path.parent.mkdir(parents=True, exist_ok=True)
                abs_save_path.unlink(missing_ok=True)
                escaped_save = str(abs_save_path).replace("\\", "\\\\").replace('"', '\\"')
                if "_.-WBLOCK" not in body.upper() and "_.SAVEAS" not in body.upper():
                    body += (
                        '\n(setvar "FILEDIA" 0)\n'
                        '(setvar "EXPERT" 5)\n'
                        '(setvar "DELOBJ" 0)\n'
                        f'(command "_.-WBLOCK" "{escaped_save}" "*")\n'
                    )

            if "_.QUIT" not in body.upper():
                body += "\n_.QUIT _Y\n"
            else:
                body += "\n"
            scr_file.write_text(body, encoding="utf-8")

            cmd = [self.console_path]
            if input_dwg:
                cmd.extend(["/i", str(Path(input_dwg).expanduser().resolve())])
            cmd.extend(["/s", str(scr_file)])

            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
            except subprocess.TimeoutExpired as te:
                raw_log = save_raw_diagnostic_log(
                    req_id,
                    {"cmd": cmd, "input_dwg": input_dwg, "save_as_dwg": save_as_dwg, "timeout": timeout},
                    stdout_bytes=te.stdout or b"",
                    stderr_bytes=te.stderr or b"",
                )
                return ensure_diagnostic_fields(
                    {
                        "ok": False,
                        "error_code": "ACCORECONSOLE_TIMEOUT",
                        "error": f"AcCoreConsole script timed out after {timeout}s",
                        "raw_log_path": raw_log,
                    },
                    requested_mode="accoreconsole",
                    actual_mode="accoreconsole",
                    request_id=req_id,
                    elapsed_ms=int((time.time() - t0) * 1000),
                    execution_stage="script_execution",
                )

            stdout_text, stdout_enc, _ = decode_bytes_with_diagnostics(proc.stdout)
            stderr_text, stderr_enc, _ = decode_bytes_with_diagnostics(proc.stderr)
            raw_log = save_raw_diagnostic_log(
                req_id,
                {
                    "cmd": cmd,
                    "returncode": proc.returncode,
                    "input_dwg": input_dwg,
                    "save_as_dwg": save_as_dwg,
                    "encodings": {"stdout": stdout_enc, "stderr": stderr_enc},
                },
                stdout_bytes=proc.stdout,
                stderr_bytes=proc.stderr,
            )

            console_err = _scan_console_errors(stdout_text, stderr_text)
            ok = (proc.returncode == 0) and (console_err is None)
            res: Dict[str, Any] = {
                "ok": ok,
                "mode": "accoreconsole",
                "actual_mode": "accoreconsole",
                "request_id": req_id,
                "elapsed_ms": int((time.time() - t0) * 1000),
                "returncode": proc.returncode,
                "script_success": proc.returncode == 0,
                "command_success": ok,
                "execution_stage": "command_completed" if ok else "command_failed",
                "error_code": console_err[0] if console_err else (None if ok else f"EXIT_{proc.returncode}"),
                "raw_log_path": raw_log,
                "stdout": stdout_text[-2000:],
                "stderr": stderr_text[-1000:],
            }
            if console_err:
                res["error"] = f"AutoCAD console script error: {console_err[1]}"

            if abs_save_path is not None:
                vinfo = (
                    self.verify_dwg_reopen(str(abs_save_path))
                    if verify_reopen
                    else {
                        "output_exists": abs_save_path.is_file(),
                        "output_size": abs_save_path.stat().st_size if abs_save_path.is_file() else 0,
                        "reopen_verified": None,
                    }
                )
                res["output_exists"] = vinfo.get("output_exists", False)
                res["output_size"] = vinfo.get("output_size", 0)
                res["reopen_verified"] = vinfo.get("reopen_verified")
                if not vinfo.get("output_exists") or (verify_reopen and not vinfo.get("reopen_verified")):
                    res["ok"] = False
                    res["command_success"] = False
                    res["saved_dwg"] = None
                    res["execution_stage"] = "output_verification_failed"
                    res["error_code"] = vinfo.get("error_code") or "SAVE_VERIFICATION_FAILED"
                    res["error"] = vinfo.get("error") or f"Saved DWG verification failed: {abs_save_path}"
                else:
                    res["saved_dwg"] = str(abs_save_path)
                    if res["ok"]:
                        res["execution_stage"] = "output_verified"

            return ensure_diagnostic_fields(
                res,
                requested_mode="accoreconsole",
                actual_mode="accoreconsole",
                request_id=req_id,
            )


_live_conn: Optional[AutoCADLiveConnection] = None
_console_runner: Optional[AcCoreConsoleRunner] = None


def get_live_connection() -> AutoCADLiveConnection:
    global _live_conn
    if _live_conn is None:
        host = os.environ.get("AUTOCAD_MCP_HOST", "127.0.0.1")
        port = int(os.environ.get("AUTOCAD_MCP_PORT", "9877"))
        _live_conn = AutoCADLiveConnection(host=host, port=port)
    return _live_conn


def get_console_runner() -> AcCoreConsoleRunner:
    global _console_runner
    if _console_runner is None:
        _console_runner = AcCoreConsoleRunner()
    return _console_runner


def open_and_verify_in_autocad(file_path: str, wait_seconds: float = 12.0) -> Dict[str, Any]:
    """Open a DWG or DXF file in the live AutoCAD 2027 GUI and verify that it finishes loading."""
    target = Path(file_path).expanduser().resolve()
    if not target.is_file():
        return {
            "open_dispatched": False,
            "document_loaded_verified": False,
            "opened_in_autocad": False,
            "error_code": "FILE_NOT_FOUND",
            "error": f"File to open does not exist: {target}",
        }

    try:
        subprocess.run(
            ["open", "-a", AUTOCAD_APP_PATH, str(target)],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as e:
        return {
            "open_dispatched": False,
            "document_loaded_verified": False,
            "opened_in_autocad": False,
            "error_code": "OPEN_DISPATCH_FAILED",
            "error": str(e),
        }

    live = get_live_connection()
    target_resolved = str(target.resolve())
    target_name_lower = target.name.lower()
    deadline = time.time() + wait_seconds
    last_status: Dict[str, Any] = {}

    while time.time() < deadline:
        try:
            if live.connect(timeout=0.5):
                st = live.status(auto_bootstrap=False)
                last_status = st
                active_doc = str(st.get("active_document") or "")
                if "/" in active_doc:
                    try:
                        active_resolved = str(Path(active_doc).expanduser().resolve())
                    except Exception:
                        active_resolved = active_doc
                    doc_matches = active_resolved.lower() == target_resolved.lower()
                else:
                    doc_matches = active_doc.lower() == target_name_lower
                if doc_matches and st.get("drawing_ready") and not st.get("busy"):
                    # Also query doc info to confirm LISP is executable on the newly opened file
                    doc_res = live.eval_lisp("(mcp:get-doc-info)", timeout_ms=4000, auto_bootstrap=False)
                    if doc_res.get("ok"):
                        doc_info = doc_res.get("result") if isinstance(doc_res.get("result"), dict) else {}
                        return {
                            "open_dispatched": True,
                            "document_loaded_verified": True,
                            "opened_in_autocad": True,
                            "active_document": active_doc,
                            "loaded_entity_count": doc_info.get("entity_count"),
                            "drawing_ready": True,
                            "command_executable": True,
                            "busy": False,
                            "modal_dialog": False,
                        }
        except Exception:
            pass
        time.sleep(0.35)

    return {
        "open_dispatched": True,
        "document_loaded_verified": False,
        "opened_in_autocad": False,
        "active_document": last_status.get("active_document"),
        "drawing_ready": last_status.get("drawing_ready", False),
        "command_executable": last_status.get("command_executable", False),
        "busy": last_status.get("busy", True),
        "modal_dialog": last_status.get("modal_dialog", False),
        "modal_window_title": last_status.get("modal_window_title", ""),
        "error_code": (
            "MODAL_DIALOG_BLOCKING_LOAD"
            if last_status.get("modal_dialog")
            else "DOCUMENT_LOAD_NOT_VERIFIED"
        ),
        "error": (
            f"Dispatched open for '{target.name}', but AutoCAD did not finish loading it as the active ready document within {wait_seconds}s "
            f"(active_document={last_status.get('active_document')!r}, busy={last_status.get('busy')}, modal_dialog={last_status.get('modal_dialog')})."
        ),
    }


def execute_autolisp_dual_mode(
    code: str,
    prefer_live: bool = True,
    require_live: bool = False,
    allow_headless_fallback: bool = False,
    auto_cancel_active_command: bool = False,
    timeout_ms: int = 15000,
    input_dwg: Optional[str] = None,
    save_as_dwg: Optional[str] = None,
    verify_reopen: bool = True,
) -> Dict[str, Any]:
    """Execute AutoLISP code via live GUI bridge or headless AcCoreConsole with explicit mode & diagnostic tracking.

    - Never silently falls back to a blank temporary AcCoreConsole drawing when querying/modifying the live GUI
      unless `allow_headless_fallback=True` or `save_as_dwg` is provided while GUI is offline.
    """
    t0 = time.time()
    if input_dwg is not None or not prefer_live:
        requested_mode = "accoreconsole"
    elif require_live:
        requested_mode = "live_gui"
    else:
        requested_mode = "live_gui" if (save_as_dwg is None) else "auto"

    fallback_reason: Optional[str] = None

    if prefer_live and input_dwg is None:
        live = get_live_connection()
        try:
            res = live.eval_lisp(
                code,
                timeout_ms=timeout_ms,
                auto_cancel=auto_cancel_active_command,
            )
            if isinstance(res, dict):
                return ensure_diagnostic_fields(
                    res,
                    requested_mode=requested_mode,
                    actual_mode="live_gui",
                    fallback_reason=None,
                    elapsed_ms=int((time.time() - t0) * 1000),
                )
        except Exception as live_err:
            if require_live or (not allow_headless_fallback and save_as_dwg is None):
                return ensure_diagnostic_fields(
                    {
                        "ok": False,
                        "script_success": False,
                        "command_success": False,
                        "error_code": "LIVE_BRIDGE_UNAVAILABLE",
                        "error": (
                            f"Live AutoCAD GUI execution failed and silent headless fallback is disabled "
                            f"to avoid reading/modifying a blank background Drawing1.dwg: {live_err}"
                        ),
                    },
                    requested_mode=requested_mode,
                    actual_mode="live_gui",
                    fallback_reason=None,
                    elapsed_ms=int((time.time() - t0) * 1000),
                    execution_stage="live_connection_failed",
                )
            fallback_reason = f"Live GUI unavailable ({live_err})"
            logger.warning(f"{fallback_reason}, falling back to AcCoreConsole")
    elif input_dwg is not None and prefer_live:
        fallback_reason = "input_dwg specified; using headless AcCoreConsole batch mode"

    runner = get_console_runner()
    headless_res = runner.eval_lisp_headless(
        code,
        input_dwg=input_dwg,
        save_as_dwg=save_as_dwg,
        verify_reopen=verify_reopen,
        timeout=(timeout_ms / 1000.0) + 15.0,
    )
    return ensure_diagnostic_fields(
        headless_res,
        requested_mode=requested_mode,
        actual_mode="accoreconsole",
        fallback_reason=fallback_reason,
    )
