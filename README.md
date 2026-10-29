# AutoCAD 2027 for Mac MCP Server (`autocad-mcp`)

A native **Model Context Protocol (MCP)** server for **AutoCAD 2027 for Mac (`26.0.60.171`, Apple Silicon & Intel)**, featuring a **Dual-Mode Hybrid Architecture**:

1. **Real-Time Live GUI Mode (`autocad_mcp_bridge.crx` + `mcp_helpers.lsp`)**
   - Native Objective-C++ Core Runtime Extension (`.crx`) loaded automatically into the running AutoCAD 2027 GUI process via `acaddoc.lsp`.
   - Exposes a local JSON-RPC over TCP endpoint (`127.0.0.1:9877`) and dispatches AutoLISP evaluation & command execution directly onto AutoCAD's Cocoa main thread (`libaccore.dylib` / `acedPostCommand` / `acedEvaluateLisp`).
2. **Headless Batch & Offline Mode (`AcCoreConsole` + `ezdxf`)**
   - Seamless fallback and batch DWG processing using AutoCAD 2027's built-in `/Applications/Autodesk/AutoCAD 2027/AutoCAD 2027.app/Contents/Helpers/AcCoreConsole.app/Contents/MacOS/AcCoreConsole`.
   - High-speed offline DXF creation and inspection via `ezdxf`.

---

## Quick Start (macOS)

```bash
git clone https://github.com/GeongyeeLee/autocad-mcp.git
cd autocad-mcp
chmod +x install_mac.sh
./install_mac.sh
```

### Register in OpenAI Codex (`~/.codex/config.toml`)

```toml
[mcp_servers.autocad]
command = "/path/to/autocad-mcp/.venv/bin/autocad-mcp"
```

Or via Codex CLI:
```bash
codex mcp add autocad -- /path/to/autocad-mcp/.venv/bin/autocad-mcp
```

---

## Available MCP Tools (20 Tools)

| Category | Tool Name | Description |
| :--- | :--- | :--- |
| **Core & Scripting** | `get_connection_status` | Check AutoCAD 2027 GUI, live TCP bridge (`127.0.0.1:9877`), and `AcCoreConsole` status; auto-bootstraps live bridge if needed |
| | `eval_autolisp` | Evaluate arbitrary AutoLISP expressions in live GUI or headless `AcCoreConsole` and return serialized JSON results |
| | `send_command` | Post raw AutoCAD commands directly to the active command line (e.g. `_.ZOOM _E`, `_.REGEN`) |
| | `get_drawing_info` | Get active drawing metadata, bounds (`EXTMIN`/`EXTMAX`), system variables, and entity count |
| **2D/3D Geometry** | `draw_line` | Draw a `LINE` entity (via `entmakex`, immune to `OSMODE` snapping issues) |
| | `draw_polyline` | Draw a lightweight polyline (`LWPOLYLINE`) with vertices, closed flag, and width |
| | `draw_circle` | Draw a `CIRCLE` entity |
| | `draw_arc` | Draw an `ARC` entity from start/end angles in degrees |
| | `draw_rectangle` | Draw a closed rectangular `LWPOLYLINE` |
| | `draw_text` | Create single-line `TEXT` or multiline `MTEXT` |
| | `add_dimension` | Add linear or aligned dimensions (`DIMLINEAR` / `DIMALIGNED`) |
| | `draw_hatch` | Create a `HATCH` inside a closed boundary entity handle |
| **Editing & Organization** | `modify_entities` | Move, copy, rotate, scale, mirror, erase, or change layer/color of entities by handle |
| | `manage_layer` | List, create, switch current layer, or toggle on/off/freeze/lock states |
| | `manage_block` | List block definitions, create a block from entity handles, or insert a block reference (`INSERT`) |
| **Inspection & Export** | `get_selection` | Retrieve currently selected entities and their DXF properties |
| | `query_entities` | Query entities in the drawing by DXF type and layer |
| | `export_drawing` | Save or export the active drawing to `DWG`, `DXF`, `PDF`, or `PNG` |
| **Batch & Offline** | `run_accoreconsole_script` | Run batch AutoLISP or `.scr` scripts in headless `AcCoreConsole` |
| | `inspect_or_create_dxf` | Fast offline DXF creation and inspection via `ezdxf` with optional AutoCAD 2027 handoff |

---

## Running Tests

```bash
./.venv/bin/python tests/test_autocad_mcp.py
```

## License

MIT
