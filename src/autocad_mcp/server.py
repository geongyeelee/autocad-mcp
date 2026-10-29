import json
import logging
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

import ezdxf

try:
    from mcp.server.mcpserver import Context, MCPServer as FastMCP
except ImportError:
    from mcp.server.fastmcp import Context, FastMCP

from .connection import (
    ACCORECONSOLE_PATH,
    AUTOCAD_APP_PATH,
    BRIDGE_CRX_PATH,
    HELPERS_LSP_PATH,
    ensure_diagnostic_fields,
    execute_autolisp_dual_mode,
    get_console_runner,
    get_live_connection,
    is_autocad_gui_running,
    new_request_id,
    open_and_verify_in_autocad,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("AutoCADMCPServer")


def _lisp_str(val: str) -> str:
    """Escape a Python string into a safe AutoLISP string literal."""
    escaped = val.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _lisp_pt(pt: Optional[List[float]], default: Optional[List[float]] = None) -> str:
    """Convert a 2D or 3D point list into an AutoLISP point list literal."""
    coords = pt if pt is not None else (default or [0.0, 0.0, 0.0])
    x = float(coords[0]) if len(coords) > 0 else 0.0
    y = float(coords[1]) if len(coords) > 1 else 0.0
    z = float(coords[2]) if len(coords) > 2 else 0.0
    return f"(list {x:.8f} {y:.8f} {z:.8f})"


def _lisp_opt_str(val: Optional[str]) -> str:
    return _lisp_str(val) if val else "nil"


def _lisp_opt_int(val: Optional[int]) -> str:
    return str(int(val)) if val is not None else "nil"


def _json_response(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    logger.info("AutoCAD 2027 MCP Server v0.2.0 starting up")
    try:
        if is_autocad_gui_running():
            conn = get_live_connection()
            if conn.connect(timeout=0.5):
                logger.info("Connected to live AutoCAD 2027 bridge on startup")
            else:
                logger.info("AutoCAD 2027 GUI is running; live bridge will auto-bootstrap on first tool call")
        yield {}
    finally:
        conn = get_live_connection()
        conn.disconnect()
        logger.info("AutoCAD 2027 MCP Server shut down")


mcp = FastMCP(
    "AutoCADMCP",
    instructions=(
        "AutoCAD 2027 for Mac integration through the Model Context Protocol (v0.2.0). "
        "Supports real-time GUI drawing/inspection via a native C++/AutoLISP bridge, "
        "headless batch processing via AcCoreConsole with UTF-8 + reopen verification, "
        "and direct DXF operations via ezdxf."
    ),
    lifespan=server_lifespan,
)


@mcp.tool()
def get_connection_status(
    ctx: Context,
    auto_bootstrap: bool = True,
) -> str:
    """Check AutoCAD 2027 GUI process, live TCP bridge (127.0.0.1:9877), drawing readiness, command executability, and AcCoreConsole status.

    Args:
        auto_bootstrap: If True and the live bridge is not yet connected, automatically open a workspace drawing in AutoCAD 2027 to load the native bridge.
    """
    t0 = time.time()
    req_id = new_request_id("status")
    gui_running = is_autocad_gui_running()
    live = get_live_connection()
    connected = live.connect(timeout=0.5)
    status_info: Dict[str, Any] = {}

    if not connected and auto_bootstrap:
        connected = live.bootstrap_live_bridge(launch_if_needed=True)

    if connected:
        try:
            status_info = live.status(auto_bootstrap=False)
        except Exception as e:
            status_info = {"ok": False, "error": str(e)}

    bridge_online = bool(connected and status_info.get("ok", False))
    drawing_ready = bool(status_info.get("drawing_ready", False))
    command_executable = bool(status_info.get("command_executable", False))
    busy = bool(status_info.get("busy", False))
    modal_dialog = bool(status_info.get("modal_dialog", False))
    active_document = status_info.get("active_document") or None
    command_names = status_info.get("command_names") or ""

    console_runner = get_console_runner()
    payload: Dict[str, Any] = {
        "ok": bridge_online or console_runner.is_available(),
        "autocad_app_path": AUTOCAD_APP_PATH,
        "gui_process_running": gui_running,
        "live_bridge_connected": connected,
        "bridge_online": bridge_online,
        "main_thread_responsive": bool(status_info.get("main_thread_responsive", False)),
        "drawing_ready": drawing_ready,
        "command_executable": command_executable,
        "active_document": active_document,
        "command_names": command_names,
        "busy": busy,
        "modal_dialog": modal_dialog,
        "modal_window_title": status_info.get("modal_window_title", ""),
        "live_bridge_endpoint": f"{live.host}:{live.port}",
        "live_bridge_ping": status_info if status_info else None,
        "accoreconsole_available": console_runner.is_available(),
        "accoreconsole_path": ACCORECONSOLE_PATH,
        "bridge_crx_exists": BRIDGE_CRX_PATH.exists(),
        "helpers_lsp_exists": HELPERS_LSP_PATH.exists(),
        "script_success": True,
        "command_success": True,
        "error_code": None if (bridge_online or console_runner.is_available()) else "NO_BACKEND_AVAILABLE",
    }
    return _json_response(
        ensure_diagnostic_fields(
            payload,
            requested_mode="live_gui",
            actual_mode="live_gui" if bridge_online else "accoreconsole",
            fallback_reason=None if bridge_online else "Live bridge not connected",
            request_id=req_id,
            elapsed_ms=int((time.time() - t0) * 1000),
            execution_stage="status_checked",
        )
    )


@mcp.tool()
def eval_autolisp(
    ctx: Context,
    code: str,
    prefer_live: bool = True,
    require_live: bool = False,
    auto_cancel_active_command: bool = False,
    verify_reopen: bool = True,
    timeout_ms: int = 15000,
    input_dwg: Optional[str] = None,
    save_as_dwg: Optional[str] = None,
) -> str:
    """Evaluate arbitrary AutoLISP code in AutoCAD 2027 and return the serialized result with full diagnostics.

    Args:
        code: AutoLISP expression(s) to evaluate, e.g. '(getvar "ACADVER")' or '(mcp:make-line ...)'.
        prefer_live: Use the live AutoCAD 2027 GUI window if available; uses headless AcCoreConsole if False or if input_dwg is provided.
        require_live: If True, strictly require live GUI execution and never fall back to AcCoreConsole.
        auto_cancel_active_command: If True and an interactive command is active in the GUI, send ESC ESC before executing.
        verify_reopen: When save_as_dwg is specified, verify the saved file exists, is non-empty, and reopens cleanly in AcCoreConsole.
        timeout_ms: Execution timeout in milliseconds.
        input_dwg: Optional path to an existing .dwg file to process in headless AcCoreConsole mode.
        save_as_dwg: Optional path to save the resulting .dwg file when running in headless mode.
    """
    res = execute_autolisp_dual_mode(
        code=code,
        prefer_live=prefer_live,
        require_live=require_live,
        auto_cancel_active_command=auto_cancel_active_command,
        verify_reopen=verify_reopen,
        timeout_ms=timeout_ms,
        input_dwg=input_dwg,
        save_as_dwg=save_as_dwg,
    )
    return _json_response(res)


@mcp.tool()
def send_command(
    ctx: Context,
    command: str,
) -> str:
    """Send a raw command string directly to the active AutoCAD 2027 command line.

    Args:
        command: Raw AutoCAD command sequence (e.g. '_.ZOOM _E' or '_.REGEN').
    """
    t0 = time.time()
    req_id = new_request_id("cmd")
    live = get_live_connection()
    try:
        res = live.post_command(command)
        return _json_response(
            ensure_diagnostic_fields(
                res,
                requested_mode="live_gui",
                actual_mode="live_gui",
                request_id=req_id,
                elapsed_ms=int((time.time() - t0) * 1000),
                execution_stage="command_dispatched" if res.get("ok") else "dispatch_failed",
            )
        )
    except Exception as e:
        return _json_response(
            ensure_diagnostic_fields(
                {
                    "ok": False,
                    "script_success": False,
                    "command_success": False,
                    "error_code": "LIVE_BRIDGE_UNAVAILABLE",
                    "error": str(e),
                },
                requested_mode="live_gui",
                actual_mode="live_gui",
                request_id=req_id,
                elapsed_ms=int((time.time() - t0) * 1000),
                execution_stage="dispatch_failed",
            )
        )


@mcp.tool()
def get_drawing_info(
    ctx: Context,
    input_dwg: Optional[str] = None,
    require_live: bool = False,
) -> str:
    """Get metadata, system variables, bounds, and entity count of the active drawing (or a file via input_dwg).

    Args:
        input_dwg: Optional path to a .dwg file to inspect in headless AcCoreConsole mode.
        require_live: If True, strictly require live foreground AutoCAD execution.
    """
    res = execute_autolisp_dual_mode(
        code="(mcp:get-doc-info)",
        prefer_live=(input_dwg is None),
        require_live=require_live or (input_dwg is None),
        input_dwg=input_dwg,
    )
    return _json_response(res)


@mcp.tool()
def draw_line(
    ctx: Context,
    start_point: List[float],
    end_point: List[float],
    layer: Optional[str] = None,
    color: Optional[int] = None,
) -> str:
    """Draw a line between two 2D/3D points in AutoCAD 2027.

    Args:
        start_point: [x, y] or [x, y, z] start coordinates.
        end_point: [x, y] or [x, y, z] end coordinates.
        layer: Optional layer name (automatically created if it does not exist).
        color: Optional AutoCAD Color Index (ACI 1=Red, 2=Yellow, 3=Green, 4=Cyan, 5=Blue, 6=Magenta, 7=White).
    """
    code = (
        f"(mcp:make-line {_lisp_pt(start_point)} {_lisp_pt(end_point)} "
        f"{_lisp_opt_str(layer)} {_lisp_opt_int(color)})"
    )
    res = execute_autolisp_dual_mode(code)
    return _json_response(res)


@mcp.tool()
def draw_polyline(
    ctx: Context,
    vertices: List[List[float]],
    closed: bool = False,
    width: float = 0.0,
    layer: Optional[str] = None,
    color: Optional[int] = None,
) -> str:
    """Draw a lightweight polyline (LWPOLYLINE) through a sequence of vertices.

    Args:
        vertices: List of [x, y] vertices (at least 2 points).
        closed: Whether the polyline should be closed.
        width: Constant global width of the polyline segments.
        layer: Optional layer name (auto-created if missing).
        color: Optional ACI color (1-255).
    """
    if len(vertices) < 2:
        return _json_response({"ok": False, "error": "At least 2 vertices are required"})

    pts_lisp = "(list " + " ".join(_lisp_pt(v) for v in vertices) + ")"
    closed_lisp = "T" if closed else "nil"
    code = (
        f"(mcp:make-lwpolyline {pts_lisp} {closed_lisp} {float(width):.6f} "
        f"{_lisp_opt_str(layer)} {_lisp_opt_int(color)})"
    )
    res = execute_autolisp_dual_mode(code)
    return _json_response(res)


@mcp.tool()
def draw_circle(
    ctx: Context,
    center: List[float],
    radius: float,
    layer: Optional[str] = None,
    color: Optional[int] = None,
) -> str:
    """Draw a circle in AutoCAD 2027.

    Args:
        center: [x, y] or [x, y, z] center coordinates.
        radius: Circle radius (> 0).
        layer: Optional layer name.
        color: Optional ACI color (1-255).
    """
    code = (
        f"(mcp:make-circle {_lisp_pt(center)} {float(radius):.8f} "
        f"{_lisp_opt_str(layer)} {_lisp_opt_int(color)})"
    )
    res = execute_autolisp_dual_mode(code)
    return _json_response(res)


@mcp.tool()
def draw_arc(
    ctx: Context,
    center: List[float],
    radius: float,
    start_angle_deg: float,
    end_angle_deg: float,
    layer: Optional[str] = None,
    color: Optional[int] = None,
) -> str:
    """Draw a circular arc counter-clockwise from start_angle_deg to end_angle_deg.

    Args:
        center: [x, y] or [x, y, z] center coordinates.
        radius: Arc radius (> 0).
        start_angle_deg: Start angle in degrees.
        end_angle_deg: End angle in degrees.
        layer: Optional layer name.
        color: Optional ACI color (1-255).
    """
    code = (
        f"(mcp:make-arc {_lisp_pt(center)} {float(radius):.8f} "
        f"{float(start_angle_deg):.8f} {float(end_angle_deg):.8f} "
        f"{_lisp_opt_str(layer)} {_lisp_opt_int(color)})"
    )
    res = execute_autolisp_dual_mode(code)
    return _json_response(res)


@mcp.tool()
def draw_rectangle(
    ctx: Context,
    corner1: List[float],
    corner2: List[float],
    width: float = 0.0,
    layer: Optional[str] = None,
    color: Optional[int] = None,
) -> str:
    """Draw an axis-aligned rectangle (as a closed LWPOLYLINE) from two opposite corners.

    Args:
        corner1: First corner [x1, y1].
        corner2: Opposite corner [x2, y2].
        width: Optional line width.
        layer: Optional layer name.
        color: Optional ACI color (1-255).
    """
    x1, y1 = float(corner1[0]), float(corner1[1])
    x2, y2 = float(corner2[0]), float(corner2[1])
    verts = [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
    return draw_polyline(
        ctx=ctx,
        vertices=verts,
        closed=True,
        width=width,
        layer=layer,
        color=color,
    )


@mcp.tool()
def draw_text(
    ctx: Context,
    position: List[float],
    text: str,
    height: float = 2.5,
    rotation_deg: float = 0.0,
    multiline: bool = False,
    layer: Optional[str] = None,
    color: Optional[int] = None,
) -> str:
    """Create single-line TEXT or multiline MTEXT in AutoCAD 2027.

    Args:
        position: Insertion point [x, y] or [x, y, z].
        text: Text string content.
        height: Text height.
        rotation_deg: Rotation angle in degrees.
        multiline: If True, creates MTEXT; otherwise creates TEXT.
        layer: Optional layer name.
        color: Optional ACI color (1-255).
    """
    ml_lisp = "T" if multiline else "nil"
    code = (
        f"(mcp:make-text {_lisp_pt(position)} {_lisp_str(text)} "
        f"{float(height):.6f} {float(rotation_deg):.6f} {ml_lisp} "
        f"{_lisp_opt_str(layer)} {_lisp_opt_int(color)})"
    )
    res = execute_autolisp_dual_mode(code)
    return _json_response(res)


@mcp.tool()
def add_dimension(
    ctx: Context,
    point1: List[float],
    point2: List[float],
    dim_line_point: List[float],
    dim_type: str = "aligned",
    layer: Optional[str] = None,
) -> str:
    """Add a linear or aligned dimension between point1 and point2.

    Args:
        point1: First extension line origin [x, y].
        point2: Second extension line origin [x, y].
        dim_line_point: Point specifying the dimension line location [x, y].
        dim_type: 'aligned' or 'linear'.
        layer: Optional layer name for the dimension.
    """
    cmd_name = "_.DIMALIGNED" if dim_type.lower() == "aligned" else "_.DIMLINEAR"
    code = f"""
    ((lambda ( / old-osmode old-clayer)
       (setq old-osmode (getvar "OSMODE")
             old-clayer (getvar "CLAYER"))
       (setvar "OSMODE" 0)
       (if {_lisp_opt_str(layer)}
         (progn
           (mcp:ensure-layer {_lisp_opt_str(layer)} nil nil)
           (setvar "CLAYER" {_lisp_opt_str(layer)})
         )
       )
       (command "{cmd_name}" {_lisp_pt(point1)} {_lisp_pt(point2)} {_lisp_pt(dim_line_point)})
       (setvar "OSMODE" old-osmode)
       (setvar "CLAYER" old-clayer)
       (mcp:raw-json (mcp:entity-to-json-str (entlast)))
     ))
    """
    res = execute_autolisp_dual_mode(code)
    return _json_response(res)


@mcp.tool()
def draw_hatch(
    ctx: Context,
    boundary_handle: str,
    pattern: str = "ANSI31",
    scale: float = 1.0,
    angle_deg: float = 0.0,
    layer: Optional[str] = None,
    color: Optional[int] = None,
) -> str:
    """Create a HATCH inside a closed entity specified by its handle.

    Args:
        boundary_handle: Hex handle of the closed boundary entity (e.g. from draw_rectangle, draw_polyline, or draw_circle).
        pattern: Hatch pattern name (e.g. 'SOLID', 'ANSI31', 'ANSI37', 'NET').
        scale: Hatch pattern scale (ignored for SOLID).
        angle_deg: Hatch pattern angle in degrees.
        layer: Optional layer name.
        color: Optional ACI color (1-255).
    """
    pat_upper = pattern.upper()
    if pat_upper == "SOLID":
        hatch_cmd = '(command "_.-HATCH" "_P" "SOLID" "_S" en "" "")'
    else:
        hatch_cmd = (
            f'(command "_.-HATCH" "_P" {_lisp_str(pat_upper)} '
            f'{float(scale):.6f} {float(angle_deg):.6f} "_S" en "" "")'
        )

    code = f"""
    ((lambda ( / en old-clayer h-en ed)
       (setq en (handent {_lisp_str(boundary_handle)}))
       (if (not en)
         (mcp:raw-json "{{\\"error\\":\\"Boundary handle not found\\"}}")
         (progn
           (setq old-clayer (getvar "CLAYER"))
           (if {_lisp_opt_str(layer)}
             (progn
               (mcp:ensure-layer {_lisp_opt_str(layer)} {_lisp_opt_int(color)} nil)
               (setvar "CLAYER" {_lisp_opt_str(layer)})
             )
           )
           {hatch_cmd}
           (setvar "CLAYER" old-clayer)
           (setq h-en (entlast))
           (if (and h-en {_lisp_opt_int(color)})
             (progn
               (setq ed (entget h-en))
               (if (assoc 62 ed)
                 (setq ed (subst (cons 62 {_lisp_opt_int(color)}) (assoc 62 ed) ed))
                 (setq ed (append ed (list (cons 62 {_lisp_opt_int(color)}))))
               )
               (entmod ed)
             )
           )
           (mcp:raw-json (mcp:entity-to-json-str h-en))
         )
       )
     ))
    """
    res = execute_autolisp_dual_mode(code)
    return _json_response(res)


@mcp.tool()
def modify_entities(
    ctx: Context,
    handles: List[str],
    action: str,
    base_point: Optional[List[float]] = None,
    target_point: Optional[List[float]] = None,
    rotation_deg: Optional[float] = None,
    scale_factor: Optional[float] = None,
    layer: Optional[str] = None,
    color: Optional[int] = None,
) -> str:
    """Modify one or more entities identified by their handles.

    Args:
        handles: List of entity hex handles (e.g. ['2DA', '2DB']).
        action: One of 'move', 'copy', 'rotate', 'scale', 'mirror', 'erase', 'set_layer', 'set_color'.
        base_point: Base point [x, y, z] for move/copy/rotate/scale/mirror.
        target_point: Target point [x, y, z] for move/copy or second mirror point.
        rotation_deg: Rotation angle in degrees (for action='rotate').
        scale_factor: Scale factor (for action='scale').
        layer: Target layer name (for action='set_layer').
        color: Target ACI color 1-255 (for action='set_color').
    """
    act = action.lower().strip()
    handles_lisp = "(list " + " ".join(_lisp_str(h) for h in handles) + ")"
    bp = _lisp_pt(base_point, [0.0, 0.0, 0.0])
    tp = _lisp_pt(target_point, [0.0, 0.0, 0.0])
    rot = float(rotation_deg or 0.0)
    scl = float(scale_factor or 1.0)

    if act == "erase":
        op_body = """
        (foreach h hlist
          (if (setq en (handent h))
            (progn (entdel en) (setq count (1+ count)))
          )
        )
        """
    elif act == "set_layer":
        op_body = f"""
        (mcp:ensure-layer {_lisp_opt_str(layer)} nil nil)
        (foreach h hlist
          (if (setq en (handent h))
            (progn
              (setq ed (entget en))
              (setq ed (subst (cons 8 {_lisp_str(layer or "0")}) (assoc 8 ed) ed))
              (entmod ed)
              (setq count (1+ count))
            )
          )
        )
        """
    elif act == "set_color":
        cval = int(color if color is not None else 7)
        op_body = f"""
        (foreach h hlist
          (if (setq en (handent h))
            (progn
              (setq ed (entget en))
              (if (assoc 62 ed)
                (setq ed (subst (cons 62 {cval}) (assoc 62 ed) ed))
                (setq ed (append ed (list (cons 62 {cval}))))
              )
              (entmod ed)
              (setq count (1+ count))
            )
          )
        )
        """
    elif act in ("move", "copy", "rotate", "scale", "mirror"):
        cmd_map = {
            "move": f'(command "_.MOVE" ss "" {bp} {tp})',
            "copy": f'(command "_.COPY" ss "" "_M" {bp} {tp} "")',
            "rotate": f'(command "_.ROTATE" ss "" {bp} {rot:.6f})',
            "scale": f'(command "_.SCALE" ss "" {bp} {scl:.6f})',
            "mirror": f'(command "_.MIRROR" ss "" {bp} {tp} "_N")',
        }
        op_body = f"""
        (setq ss (ssadd))
        (foreach h hlist
          (if (setq en (handent h))
            (progn (ssadd en ss) (setq count (1+ count)))
          )
        )
        (if (> count 0)
          (progn
            (setq old-osmode (getvar "OSMODE"))
            (setvar "OSMODE" 0)
            {cmd_map[act]}
            (setvar "OSMODE" old-osmode)
          )
        )
        """
    else:
        return _json_response({"ok": False, "error": f"Unsupported action: {action}"})

    code = f"""
    ((lambda ( / hlist count en ed ss old-osmode)
       (setq hlist {handles_lisp}
             count 0)
       {op_body}
       (mcp:raw-json
         (strcat "{{\\"action\\":\\"{act}\\",\\"affected_count\\":" (itoa count) "}}")
       )
     ))
    """
    res = execute_autolisp_dual_mode(code)
    return _json_response(res)


@mcp.tool()
def manage_layer(
    ctx: Context,
    action: str = "list",
    name: Optional[str] = None,
    color: Optional[int] = None,
    linetype: Optional[str] = None,
) -> str:
    """List, create, switch, or change properties of AutoCAD layers.

    Args:
        action: One of 'list', 'create', 'set_current', 'on', 'off', 'freeze', 'thaw', 'lock', 'unlock'.
        name: Layer name (required for all actions except 'list').
        color: Optional ACI color (1-255) when creating or updating a layer.
        linetype: Optional linetype name (e.g. 'Continuous', 'DASHED', 'CENTER').
    """
    act = action.lower().strip()
    if act == "list":
        return _json_response(execute_autolisp_dual_mode("(mcp:list-layers)"))

    if not name:
        return _json_response({"ok": False, "error": "Layer 'name' is required for this action"})

    if act == "create":
        code = f"""
        (progn
          (mcp:ensure-layer {_lisp_str(name)} {_lisp_opt_int(color)} {_lisp_opt_str(linetype)})
          (if {_lisp_opt_int(color)}
            (command "_.-LAYER" "_C" {_lisp_opt_int(color)} {_lisp_str(name)} "")
          )
          (mcp:list-layers)
        )
        """
    elif act == "set_current":
        code = f"""
        (progn
          (mcp:ensure-layer {_lisp_str(name)} {_lisp_opt_int(color)} {_lisp_opt_str(linetype)})
          (setvar "CLAYER" {_lisp_str(name)})
          (mcp:get-doc-info)
        )
        """
    elif act in ("on", "off", "freeze", "thaw", "lock", "unlock"):
        flag_map = {
            "on": "_ON",
            "off": "_OFF",
            "freeze": "_F",
            "thaw": "_T",
            "lock": "_LO",
            "unlock": "_U",
        }
        code = f"""
        (progn
          (command "_.-LAYER" "{flag_map[act]}" {_lisp_str(name)} "")
          (mcp:list-layers)
        )
        """
    else:
        return _json_response({"ok": False, "error": f"Unknown layer action: {action}"})

    res = execute_autolisp_dual_mode(code)
    return _json_response(res)


@mcp.tool()
def manage_block(
    ctx: Context,
    action: str = "list",
    block_name: Optional[str] = None,
    point: Optional[List[float]] = None,
    handles: Optional[List[str]] = None,
    scale_x: float = 1.0,
    scale_y: float = 1.0,
    scale_z: float = 1.0,
    rotation_deg: float = 0.0,
    layer: Optional[str] = None,
    color: Optional[int] = None,
) -> str:
    """List blocks, create a new block definition from entity handles, or insert a block reference.

    Args:
        action: One of 'list', 'create', 'insert'.
        block_name: Name of the block (required for 'create' and 'insert').
        point: Base point (for 'create') or insertion point (for 'insert').
        handles: List of entity handles to group into the block (required for 'create').
        scale_x: X scale factor for 'insert'.
        scale_y: Y scale factor for 'insert'.
        scale_z: Z scale factor for 'insert'.
        rotation_deg: Rotation angle in degrees for 'insert'.
        layer: Optional layer name for 'insert'.
        color: Optional ACI color for 'insert'.
    """
    act = action.lower().strip()
    if act == "list":
        return _json_response(execute_autolisp_dual_mode("(mcp:list-blocks)"))

    if not block_name:
        return _json_response({"ok": False, "error": "'block_name' is required"})

    if act == "insert":
        code = (
            f"(mcp:insert-block {_lisp_str(block_name)} {_lisp_pt(point, [0.0, 0.0, 0.0])} "
            f"{float(scale_x):.6f} {float(scale_y):.6f} {float(scale_z):.6f} "
            f"{float(rotation_deg):.6f} {_lisp_opt_str(layer)} {_lisp_opt_int(color)})"
        )
        return _json_response(execute_autolisp_dual_mode(code))

    if act == "create":
        if not handles:
            return _json_response({"ok": False, "error": "'handles' list is required to create a block"})
        hlist_lisp = "(list " + " ".join(_lisp_str(h) for h in handles) + ")"
        bp_lisp = _lisp_pt(point, [0.0, 0.0, 0.0])
        code = f"""
        ((lambda ( / bp hlist en ed)
           (setq bp {bp_lisp}
                 hlist {hlist_lisp})
           (entmake
             (list
               '(0 . "BLOCK")
               (cons 2 {_lisp_str(block_name)})
               '(70 . 0)
               (cons 10 bp)
             )
           )
           (foreach h hlist
             (if (setq en (handent h))
               (progn
                 (setq ed (entget en))
                 (setq ed
                   (vl-remove-if
                     '(lambda (pair) (member (car pair) '(-1 5 330)))
                     ed
                   )
                 )
                 (entmake ed)
               )
             )
           )
           (entmake '((0 . "ENDBLK")))
           (foreach h hlist
             (if (setq en (handent h)) (entdel en))
           )
           (mcp:insert-block {_lisp_str(block_name)} bp 1.0 1.0 1.0 0.0 {_lisp_opt_str(layer)} {_lisp_opt_int(color)})
         ))
        """
        return _json_response(execute_autolisp_dual_mode(code))

    return _json_response({"ok": False, "error": f"Unsupported block action: {action}"})


@mcp.tool()
def get_selection(ctx: Context) -> str:
    """Get all currently or previously selected entities in the active AutoCAD 2027 drawing."""
    res = execute_autolisp_dual_mode("(mcp:get-selection)", prefer_live=True, require_live=True)
    return _json_response(res)


@mcp.tool()
def query_entities(
    ctx: Context,
    entity_type: Optional[str] = None,
    layer: Optional[str] = None,
    limit: int = 100,
    input_dwg: Optional[str] = None,
    require_live: bool = False,
) -> str:
    """Query entities in the active drawing (or a DWG file) with optional type and layer filters.

    Args:
        entity_type: Optional DXF entity type filter (e.g. 'LINE', 'LWPOLYLINE', 'CIRCLE', 'ARC', 'TEXT', 'MTEXT', 'INSERT', 'DIMENSION', 'HATCH').
        layer: Optional layer name filter.
        limit: Maximum number of entity records to return (default 100).
        input_dwg: Optional path to a .dwg file to inspect in headless mode.
        require_live: If True, strictly require live foreground AutoCAD execution.
    """
    code = (
        f"(mcp:query-entities {_lisp_opt_str(entity_type)} "
        f"{_lisp_opt_str(layer)} {int(limit)})"
    )
    res = execute_autolisp_dual_mode(
        code=code,
        prefer_live=(input_dwg is None),
        require_live=require_live or (input_dwg is None),
        input_dwg=input_dwg,
    )
    return _json_response(res)


@mcp.tool()
def export_drawing(
    ctx: Context,
    output_path: str,
    format: str = "dwg",
    verify_reopen: bool = True,
) -> str:
    """Save or export the active AutoCAD 2027 drawing to DWG, DXF, PDF, or PNG with file & reopen verification.

    Args:
        output_path: Target file path (e.g. '/tmp/output.dwg', '/tmp/output.dxf', '/tmp/output.pdf', '/tmp/preview.png').
        format: One of 'dwg', 'dxf', 'pdf', 'png'.
        verify_reopen: When format='dwg', verify the saved file exists, is non-empty, and reopens cleanly in AcCoreConsole.
    """
    fmt = format.lower().strip()
    out_file = Path(output_path).expanduser().resolve()
    abs_out = str(out_file)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "dwg":
        code = f"""
        ((lambda ( / old-fd old-exp old-del cur-path target-path)
           (setq old-fd (getvar "FILEDIA")
                 old-exp (getvar "EXPERT")
                 old-del (getvar "DELOBJ")
                 cur-path (strcat (getvar "DWGPREFIX") (getvar "DWGNAME"))
                 target-path {_lisp_str(abs_out)})
           (setvar "FILEDIA" 0)
           (setvar "EXPERT" 5)
           (setvar "DELOBJ" 0)
           (if (= (strcase cur-path) (strcase target-path))
             (command "_.QSAVE")
             (progn
               (vl-file-delete target-path)
               (command "_.-WBLOCK" target-path "*")
             )
           )
           (setvar "FILEDIA" old-fd)
           (setvar "EXPERT" old-exp)
           (setvar "DELOBJ" old-del)
           (mcp:raw-json
             (strcat "{{\\"saved\\":true,\\"format\\":\\"dwg\\",\\"path\\":" (mcp:to-json target-path) "}}")
           )
         ))
        """
        res = execute_autolisp_dual_mode(code, prefer_live=True, require_live=True)
        if verify_reopen:
            vinfo = get_console_runner().verify_dwg_reopen(abs_out)
        else:
            exists = out_file.is_file()
            sz = out_file.stat().st_size if exists else 0
            vinfo = {"output_exists": exists, "output_size": sz, "reopen_verified": None}

        res["output_exists"] = vinfo.get("output_exists", False)
        res["output_size"] = vinfo.get("output_size", 0)
        res["reopen_verified"] = vinfo.get("reopen_verified")
        if not res["output_exists"] or (verify_reopen and not res["reopen_verified"]):
            res["ok"] = False
            res["command_success"] = False
            res["saved_dwg"] = None
            res["execution_stage"] = "output_verification_failed"
            res["error_code"] = vinfo.get("error_code") or "SAVE_VERIFICATION_FAILED"
            res["error"] = vinfo.get("error") or f"Failed to verify exported DWG at {abs_out}"
        else:
            res["saved_dwg"] = abs_out
            res["execution_stage"] = "output_verified"
        return _json_response(res)

    if fmt in ("dxf", "pdf"):
        out_file.unlink(missing_ok=True)
        if fmt == "dxf":
            code = f"""
            ((lambda ( / old-fd old-exp)
               (setq old-fd (getvar "FILEDIA")
                     old-exp (getvar "EXPERT"))
               (setvar "FILEDIA" 0)
               (setvar "EXPERT" 5)
               (command "_.DXFOUT" {_lisp_str(abs_out)} "16")
               (setvar "FILEDIA" old-fd)
               (setvar "EXPERT" old-exp)
               (mcp:raw-json
                 (strcat "{{\\"saved\\":true,\\"format\\":\\"dxf\\",\\"path\\":" (mcp:to-json {_lisp_str(abs_out)}) "}}")
               )
             ))
            """
        else:
            code = f"""
            (progn
              (command "_.ZOOM" "_E")
              (command "_.-EXPORT" "_PDF" "_E" "_N" {_lisp_str(abs_out)})
              (mcp:raw-json
                (strcat "{{\\"saved\\":true,\\"format\\":\\"pdf\\",\\"path\\":" (mcp:to-json {_lisp_str(abs_out)}) "}}")
              )
            )
            """
        res = execute_autolisp_dual_mode(code, prefer_live=True, require_live=True)
        exists = out_file.is_file()
        sz = out_file.stat().st_size if exists else 0
        res["output_exists"] = exists
        res["output_size"] = sz
        res["reopen_verified"] = None
        if not exists or sz == 0:
            res["ok"] = False
            res["command_success"] = False
            res["execution_stage"] = "output_verification_failed"
            res["error_code"] = "OUTPUT_FILE_MISSING"
            res["error"] = f"Exported {fmt.upper()} file was not created or is empty: {abs_out}"
        else:
            res["execution_stage"] = "output_verified"
        return _json_response(res)

    if fmt == "png":
        t0 = time.time()
        out_file.unlink(missing_ok=True)
        execute_autolisp_dual_mode('(command "_.ZOOM" "_E")', prefer_live=True)
        try:
            subprocess.run(["screencapture", "-x", abs_out], check=True, timeout=5)
            exists = out_file.is_file()
            sz = out_file.stat().st_size if exists else 0
            return _json_response(
                ensure_diagnostic_fields(
                    {
                        "ok": exists and sz > 0,
                        "format": "png",
                        "path": abs_out,
                        "output_exists": exists,
                        "output_size": sz,
                    },
                    requested_mode="live_gui",
                    actual_mode="live_gui",
                    elapsed_ms=int((time.time() - t0) * 1000),
                    execution_stage="output_verified" if (exists and sz > 0) else "output_verification_failed",
                )
            )
        except Exception as e:
            return _json_response(
                ensure_diagnostic_fields(
                    {
                        "ok": False,
                        "error_code": "PNG_CAPTURE_FAILED",
                        "error": f"Failed to capture PNG: {e}",
                        "output_exists": False,
                        "output_size": 0,
                    },
                    requested_mode="live_gui",
                    actual_mode="live_gui",
                    elapsed_ms=int((time.time() - t0) * 1000),
                    execution_stage="output_verification_failed",
                )
            )

    return _json_response({"ok": False, "error_code": "UNSUPPORTED_FORMAT", "error": f"Unsupported export format: {format}"})


@mcp.tool()
def run_accoreconsole_script(
    ctx: Context,
    script_or_lisp: str,
    is_lisp: bool = True,
    input_dwg: Optional[str] = None,
    save_as_dwg: Optional[str] = None,
    verify_reopen: bool = True,
    timeout_sec: float = 45.0,
) -> str:
    """Run a batch AutoLISP or .scr script in AutoCAD 2027's headless AcCoreConsole with UTF-8 + save reopen verification.

    Args:
        script_or_lisp: AutoLISP expressions (if is_lisp=True) or raw AutoCAD .scr script lines (if is_lisp=False).
        is_lisp: Whether script_or_lisp is AutoLISP code (True) or raw .scr commands (False).
        input_dwg: Optional input .dwg file path to open in AcCoreConsole.
        save_as_dwg: Optional output .dwg file path to save after execution.
        verify_reopen: When save_as_dwg is provided, verify the saved file exists, is non-empty, and reopens cleanly in AcCoreConsole before returning saved_dwg.
        timeout_sec: Maximum execution time in seconds.
    """
    runner = get_console_runner()
    if is_lisp:
        res = runner.eval_lisp_headless(
            lisp_code=script_or_lisp,
            input_dwg=input_dwg,
            save_as_dwg=save_as_dwg,
            verify_reopen=verify_reopen,
            timeout=timeout_sec,
        )
    else:
        res = runner.run_script_file(
            script_content=script_or_lisp,
            input_dwg=input_dwg,
            save_as_dwg=save_as_dwg,
            verify_reopen=verify_reopen,
            timeout=timeout_sec,
        )
    return _json_response(res)


@mcp.tool()
def inspect_or_create_dxf(
    ctx: Context,
    dxf_path: str,
    action: str = "inspect",
    entities_spec: Optional[List[Dict[str, Any]]] = None,
    open_in_autocad: bool = False,
    open_wait_seconds: float = 12.0,
) -> str:
    """High-speed offline DXF inspection or creation using ezdxf, with verified handoff to AutoCAD 2027.

    Args:
        dxf_path: Path to the .dxf file to inspect or create.
        action: 'inspect' to read layers/entities summary of an existing DXF, or 'create' to generate a new DXF from entities_spec.
        entities_spec: For action='create', a list of dicts describing entities, e.g. [{'type': 'LINE', 'start': [0,0], 'end': [100,0], 'layer': 'WALL'}, {'type': 'CIRCLE', 'center': [50,50], 'radius': 20}].
        open_in_autocad: If True, opens the DXF file in the AutoCAD 2027 GUI and verifies that AutoCAD finishes loading it.
        open_wait_seconds: Maximum time to wait for AutoCAD GUI to finish loading the document when open_in_autocad=True.
    """
    t0 = time.time()
    req_id = new_request_id("dxf")
    abs_path = Path(dxf_path).expanduser().resolve()
    act = action.lower().strip()

    try:
        if act == "create":
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            doc = ezdxf.new("R2018", setup=True)
            msp = doc.modelspace()
            created = 0
            for item in entities_spec or []:
                etype = str(item.get("type", "LINE")).upper()
                layer = str(item.get("layer", "0"))
                color = item.get("color")
                if layer not in doc.layers:
                    doc.layers.add(layer, color=int(color) if color else 7)
                dxfattribs: Dict[str, Any] = {"layer": layer}
                if color is not None:
                    dxfattribs["color"] = int(color)

                if etype == "LINE":
                    msp.add_line(item["start"], item["end"], dxfattribs=dxfattribs)
                    created += 1
                elif etype == "CIRCLE":
                    msp.add_circle(item["center"], float(item["radius"]), dxfattribs=dxfattribs)
                    created += 1
                elif etype == "ARC":
                    msp.add_arc(
                        item["center"],
                        float(item["radius"]),
                        float(item.get("start_angle", 0.0)),
                        float(item.get("end_angle", 90.0)),
                        dxfattribs=dxfattribs,
                    )
                    created += 1
                elif etype in ("LWPOLYLINE", "POLYLINE"):
                    msp.add_lwpolyline(
                        item["vertices"],
                        close=bool(item.get("closed", False)),
                        dxfattribs=dxfattribs,
                    )
                    created += 1
                elif etype == "TEXT":
                    txt = msp.add_text(
                        str(item.get("text", "")),
                        height=float(item.get("height", 2.5)),
                        dxfattribs=dxfattribs,
                    )
                    txt.set_placement(item.get("position", [0, 0]))
                    created += 1

            doc.saveas(str(abs_path))
        elif act == "inspect":
            if not abs_path.exists():
                return _json_response(
                    ensure_diagnostic_fields(
                        {
                            "ok": False,
                            "error_code": "DXF_FILE_NOT_FOUND",
                            "error": f"DXF file not found: {abs_path}",
                        },
                        requested_mode="ezdxf_offline",
                        actual_mode="ezdxf_offline",
                        request_id=req_id,
                        elapsed_ms=int((time.time() - t0) * 1000),
                        execution_stage="precheck_failed",
                    )
                )
            doc = ezdxf.readfile(str(abs_path))
            msp = doc.modelspace()
        else:
            return _json_response(
                ensure_diagnostic_fields(
                    {
                        "ok": False,
                        "error_code": "UNSUPPORTED_ACTION",
                        "error": f"Unsupported action: {action}",
                    },
                    requested_mode="ezdxf_offline",
                    actual_mode="ezdxf_offline",
                    request_id=req_id,
                    elapsed_ms=int((time.time() - t0) * 1000),
                    execution_stage="precheck_failed",
                )
            )

        type_counts: Dict[str, int] = {}
        for e in msp:
            t = e.dxftype()
            type_counts[t] = type_counts.get(t, 0) + 1

        layers = [lyr.dxf.name for lyr in doc.layers]
        exists = abs_path.is_file()
        sz = abs_path.stat().st_size if exists else 0

        result_payload: Dict[str, Any] = {
            "ok": True,
            "action": act,
            "dxf_path": str(abs_path),
            "dxf_version": doc.dxfversion,
            "layers": layers,
            "entity_counts_by_type": type_counts,
            "total_entities": sum(type_counts.values()),
            "output_exists": exists if act == "create" else None,
            "output_size": sz if act == "create" else None,
            "open_dispatched": False,
            "document_loaded_verified": False,
            "opened_in_autocad": False,
        }

        if open_in_autocad:
            open_info = open_and_verify_in_autocad(str(abs_path), wait_seconds=open_wait_seconds)
            result_payload.update(open_info)
            if not open_info.get("document_loaded_verified"):
                result_payload["ok"] = False
                result_payload["command_success"] = False
                result_payload["execution_stage"] = "open_verification_failed"
            else:
                result_payload["execution_stage"] = "document_loaded_verified"

        return _json_response(
            ensure_diagnostic_fields(
                result_payload,
                requested_mode="live_gui" if open_in_autocad else "ezdxf_offline",
                actual_mode="live_gui" if open_in_autocad else "ezdxf_offline",
                request_id=req_id,
                elapsed_ms=int((time.time() - t0) * 1000),
            )
        )
    except Exception as e:
        return _json_response(
            ensure_diagnostic_fields(
                {
                    "ok": False,
                    "error_code": "DXF_OPERATION_FAILED",
                    "error": str(e),
                },
                requested_mode="ezdxf_offline",
                actual_mode="ezdxf_offline",
                request_id=req_id,
                elapsed_ms=int((time.time() - t0) * 1000),
                execution_stage="failed",
            )
        )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
