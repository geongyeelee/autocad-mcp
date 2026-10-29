import json
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

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

    def request(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 20.0) -> Dict[str, Any]:
        """Send a JSON-RPC request to the AutoCAD 2027 bridge and return the result."""
        if not self.connect():
            if not self.bootstrap_live_bridge(launch_if_needed=True):
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
                    text = buf.decode("utf-8", errors="replace").strip()
                    try:
                        resp = json.loads(text)
                        break
                    except json.JSONDecodeError as je:
                        # If the outer JSON-RPC envelope terminator ',"id":...}' has arrived,
                        # do not block waiting for more bytes on a malformed inner payload.
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
        return self.request("ping", {}, timeout=3.0)

    def eval_lisp(self, code: str, timeout_ms: int = 15000) -> Dict[str, Any]:
        return self.request(
            "eval_lisp",
            {"code": code, "timeout_ms": timeout_ms},
            timeout=(timeout_ms / 1000.0) + 5.0,
        )

    def post_command(self, command: str) -> Dict[str, Any]:
        return self.request("post_command", {"command": command}, timeout=5.0)


class AcCoreConsoleRunner:
    """Headless batch runner using AutoCAD 2027's AcCoreConsole."""

    def __init__(self, console_path: str = ACCORECONSOLE_PATH):
        self.console_path = console_path

    def is_available(self) -> bool:
        return Path(self.console_path).is_file()

    def eval_lisp_headless(
        self,
        lisp_code: str,
        input_dwg: Optional[str] = None,
        save_as_dwg: Optional[str] = None,
        timeout: float = 30.0,
    ) -> Dict[str, Any]:
        """Run AutoLISP code inside headless AcCoreConsole and return structured JSON output."""
        if not self.is_available():
            return {"ok": False, "error": f"AcCoreConsole not found at {self.console_path}"}

        with tempfile.TemporaryDirectory(prefix="autocad_mcp_batch_") as tmpdir:
            tmp_path = Path(tmpdir)
            req_lsp = tmp_path / "request.lsp"
            resp_json = tmp_path / "response.json"
            scr_file = tmp_path / "runner.scr"

            if save_as_dwg:
                abs_save_path = Path(save_as_dwg).expanduser().resolve()
                abs_save_path.unlink(missing_ok=True)
                abs_save = str(abs_save_path)
                escaped_save = abs_save.replace("\\", "\\\\").replace('"', '\\"')
                full_lisp = (
                    f"{lisp_code}\n"
                    f'(setvar "FILEDIA" 0)\n'
                    f'(setvar "EXPERT" 5)\n'
                    f'(setvar "DELOBJ" 0)\n'
                    f'(command "_.-WBLOCK" "{escaped_save}" "*")\n'
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
                    text=True,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                return {"ok": False, "error": f"AcCoreConsole timed out after {timeout}s"}

            if resp_json.exists():
                try:
                    data = json.loads(resp_json.read_text(encoding="utf-8"))
                    data["mode"] = "accoreconsole"
                    if save_as_dwg:
                        data["saved_dwg"] = str(Path(save_as_dwg).expanduser().resolve())
                    return data
                except Exception as e:
                    return {
                        "ok": False,
                        "error": f"Failed to parse AcCoreConsole JSON output: {e}",
                        "stdout": proc.stdout[-1000:],
                    }

            return {
                "ok": False,
                "error": "AcCoreConsole did not produce a response JSON file",
                "returncode": proc.returncode,
                "stdout": proc.stdout[-1500:],
                "stderr": proc.stderr[-1000:],
            }

    def run_script_file(
        self,
        script_content: str,
        input_dwg: Optional[str] = None,
        timeout: float = 45.0,
    ) -> Dict[str, Any]:
        """Run a raw AutoCAD .scr script file in AcCoreConsole."""
        if not self.is_available():
            return {"ok": False, "error": f"AcCoreConsole not found at {self.console_path}"}

        with tempfile.TemporaryDirectory(prefix="autocad_mcp_scr_") as tmpdir:
            scr_file = Path(tmpdir) / "batch.scr"
            body = script_content.rstrip()
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
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
                return {
                    "ok": proc.returncode == 0,
                    "mode": "accoreconsole",
                    "returncode": proc.returncode,
                    "stdout": proc.stdout[-2000:],
                    "stderr": proc.stderr[-1000:],
                }
            except subprocess.TimeoutExpired:
                return {"ok": False, "error": f"AcCoreConsole script timed out after {timeout}s"}


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


def execute_autolisp_dual_mode(
    code: str,
    prefer_live: bool = True,
    timeout_ms: int = 15000,
    input_dwg: Optional[str] = None,
    save_as_dwg: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute AutoLISP code via live GUI bridge first, or fallback to AcCoreConsole."""
    if prefer_live and not input_dwg:
        live = get_live_connection()
        try:
            res = live.eval_lisp(code, timeout_ms=timeout_ms)
            if isinstance(res, dict):
                res.setdefault("mode", "live_gui")
            return res
        except Exception as live_err:
            logger.warning(f"Live GUI execution unavailable ({live_err}), falling back to AcCoreConsole")

    runner = get_console_runner()
    return runner.eval_lisp_headless(
        code,
        input_dwg=input_dwg,
        save_as_dwg=save_as_dwg,
        timeout=(timeout_ms / 1000.0) + 15.0,
    )
