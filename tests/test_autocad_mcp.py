import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from autocad_mcp.connection import open_and_verify_in_autocad
from autocad_mcp.server import (
    add_dimension,
    draw_arc,
    draw_circle,
    draw_hatch,
    draw_line,
    draw_polyline,
    draw_rectangle,
    draw_text,
    eval_autolisp,
    export_drawing,
    get_connection_status,
    get_drawing_info,
    get_selection,
    inspect_or_create_dxf,
    manage_block,
    manage_layer,
    mcp,
    modify_entities,
    query_entities,
    run_accoreconsole_script,
    send_command,
)

REQUIRED_DIAGNOSTIC_FIELDS = {
    "requested_mode",
    "actual_mode",
    "fallback_reason",
    "request_id",
    "elapsed_ms",
    "execution_stage",
    "active_document",
    "command_names",
    "busy",
    "modal_dialog",
    "script_success",
    "command_success",
    "output_exists",
    "output_size",
    "reopen_verified",
    "error_code",
    "raw_log_path",
}


class TestAutoCADMCP(unittest.TestCase):
    def setUp(self):
        self.ctx = MagicMock()
        self.ctx.request_id = 1

    def assertHasDiagnosticFields(self, payload: dict):
        missing = REQUIRED_DIAGNOSTIC_FIELDS - set(payload.keys())
        self.assertFalse(missing, f"Missing required diagnostic fields: {missing}")

    def test_01_registered_tools(self):
        tools = [t.name for t in mcp._tool_manager.list_tools()]
        expected = {
            "get_connection_status",
            "eval_autolisp",
            "send_command",
            "get_drawing_info",
            "draw_line",
            "draw_polyline",
            "draw_circle",
            "draw_arc",
            "draw_rectangle",
            "draw_text",
            "add_dimension",
            "draw_hatch",
            "modify_entities",
            "manage_layer",
            "manage_block",
            "get_selection",
            "query_entities",
            "export_drawing",
            "run_accoreconsole_script",
            "inspect_or_create_dxf",
        }
        self.assertTrue(expected.issubset(set(tools)))

    def test_02_ezdxf_create_and_inspect(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dxf_file = os.path.join(tmpdir, "test_scene.dxf")
            spec = [
                {"type": "LINE", "start": [0, 0], "end": [500, 0], "layer": "WALL", "color": 1},
                {"type": "CIRCLE", "center": [250, 250], "radius": 100, "layer": "COLUMN", "color": 3},
                {
                    "type": "LWPOLYLINE",
                    "vertices": [[0, 0], [500, 0], [500, 300], [0, 300]],
                    "closed": True,
                    "layer": "BOUNDARY",
                },
                {"type": "TEXT", "position": [50, 50], "text": "ROOM A", "height": 20, "layer": "TEXT"},
            ]
            create_res = json.loads(
                inspect_or_create_dxf(
                    self.ctx,
                    dxf_path=dxf_file,
                    action="create",
                    entities_spec=spec,
                    open_in_autocad=False,
                )
            )
            self.assertTrue(create_res["ok"])
            self.assertHasDiagnosticFields(create_res)
            self.assertEqual(create_res["total_entities"], 4)
            self.assertTrue(create_res["output_exists"])
            self.assertGreater(create_res["output_size"], 100)

            inspect_res = json.loads(
                inspect_or_create_dxf(
                    self.ctx,
                    dxf_path=dxf_file,
                    action="inspect",
                    open_in_autocad=False,
                )
            )
            self.assertTrue(inspect_res["ok"])
            self.assertHasDiagnosticFields(inspect_res)
            self.assertIn("WALL", inspect_res["layers"])
            self.assertEqual(inspect_res["entity_counts_by_type"]["LINE"], 1)

    def test_03_live_or_console_drawing_workflow(self):
        status = json.loads(get_connection_status(self.ctx, auto_bootstrap=True))
        self.assertHasDiagnosticFields(status)
        self.assertTrue(status["accoreconsole_available"])
        self.assertTrue(status["live_bridge_connected"])
        self.assertTrue(status["bridge_online"])
        self.assertTrue(status["drawing_ready"])
        self.assertTrue(status["command_executable"])
        self.assertFalse(status["busy"])
        self.assertFalse(status["modal_dialog"])

        info = json.loads(get_drawing_info(self.ctx, require_live=True))
        self.assertTrue(info["ok"])
        self.assertHasDiagnosticFields(info)
        self.assertEqual(info["requested_mode"], "live_gui")
        self.assertEqual(info["actual_mode"], "live_gui")
        self.assertIn("acadver", info["result"])

        # Draw outer architectural wall rectangle (5000 x 3000)
        rect_res = json.loads(
            draw_rectangle(
                self.ctx,
                corner1=[0.0, 0.0],
                corner2=[5000.0, 3000.0],
                width=20.0,
                layer="A-WALL",
                color=1,
            )
        )
        self.assertTrue(rect_res["ok"])
        self.assertHasDiagnosticFields(rect_res)
        self.assertEqual(rect_res["actual_mode"], "live_gui")
        self.assertEqual(rect_res["result"]["type"], "LWPOLYLINE")
        self.assertEqual(rect_res["result"]["layer"], "A-WALL")

        # Draw center column circle
        circ_res = json.loads(
            draw_circle(
                self.ctx,
                center=[2500.0, 1500.0, 0.0],
                radius=300.0,
                layer="S-COLS",
                color=3,
            )
        )
        self.assertTrue(circ_res["ok"])
        self.assertEqual(circ_res["actual_mode"], "live_gui")
        self.assertEqual(circ_res["result"]["type"], "CIRCLE")

        # Draw diagonal axis line
        line_res = json.loads(
            draw_line(
                self.ctx,
                start_point=[0.0, 0.0, 0.0],
                end_point=[5000.0, 3000.0, 0.0],
                layer="A-AXIS",
                color=2,
            )
        )
        self.assertTrue(line_res["ok"])
        self.assertEqual(line_res["actual_mode"], "live_gui")
        self.assertEqual(line_res["result"]["type"], "LINE")

        # Draw room label text (including Chinese characters)
        txt_res = json.loads(
            draw_text(
                self.ctx,
                position=[1800.0, 1400.0, 0.0],
                text="AutoCAD 2027 实时建筑平面测试",
                height=120.0,
                layer="A-ANNO-TEXT",
                color=4,
            )
        )
        self.assertTrue(txt_res["ok"])
        self.assertEqual(txt_res["actual_mode"], "live_gui")
        self.assertEqual(txt_res["result"]["type"], "TEXT")
        self.assertEqual(txt_res["result"]["text"], "AutoCAD 2027 实时建筑平面测试")

        # Zoom extents in live AutoCAD 2027 GUI
        zoom_res = json.loads(eval_autolisp(self.ctx, code='(command "_.ZOOM" "_E")', require_live=True))
        self.assertTrue(zoom_res["ok"])
        self.assertTrue(zoom_res["command_success"])

        # Query entities on layer A-WALL
        q_res = json.loads(query_entities(self.ctx, layer="A-WALL", require_live=True))
        self.assertTrue(q_res["ok"])
        self.assertGreaterEqual(q_res["result"]["total"], 1)

    def test_04_blocks_layers_dimensions_modify_and_export(self):
        layers_res = json.loads(
            manage_layer(self.ctx, action="create", name="A-FURN", color=6)
        )
        self.assertTrue(layers_res["ok"])
        layer_names = [l["name"] for l in layers_res["result"]]
        self.assertIn("A-FURN", layer_names)

        arc_res = json.loads(
            draw_arc(
                self.ctx,
                center=[1000.0, 1000.0, 0.0],
                radius=400.0,
                start_angle_deg=0.0,
                end_angle_deg=90.0,
                layer="A-DOOR",
                color=4,
            )
        )
        self.assertTrue(arc_res["ok"])
        self.assertEqual(arc_res["result"]["type"], "ARC")

        dim_res = json.loads(
            add_dimension(
                self.ctx,
                point1=[0.0, 3000.0, 0.0],
                point2=[5000.0, 3000.0, 0.0],
                dim_line_point=[2500.0, 3400.0, 0.0],
                dim_type="linear",
                layer="A-ANNO-DIMS",
            )
        )
        self.assertTrue(dim_res["ok"])
        self.assertEqual(dim_res["result"]["type"], "DIMENSION")

        c1 = json.loads(
            draw_circle(self.ctx, center=[0.0, 0.0, 0.0], radius=150.0, layer="A-FURN", color=6)
        )["result"]["handle"]
        l1 = json.loads(
            draw_line(
                self.ctx,
                start_point=[-150.0, 0.0, 0.0],
                end_point=[150.0, 0.0, 0.0],
                layer="A-FURN",
                color=6,
            )
        )["result"]["handle"]

        blk_create = json.loads(
            manage_block(
                self.ctx,
                action="create",
                block_name="MCP_TABLE_SYM",
                point=[0.0, 0.0, 0.0],
                handles=[c1, l1],
                layer="A-FURN",
            )
        )
        self.assertTrue(blk_create["ok"])
        self.assertEqual(blk_create["result"]["type"], "INSERT")

        blk_insert = json.loads(
            manage_block(
                self.ctx,
                action="insert",
                block_name="MCP_TABLE_SYM",
                point=[3800.0, 1500.0, 0.0],
                scale_x=1.5,
                scale_y=1.5,
                rotation_deg=45.0,
                layer="A-FURN",
            )
        )
        self.assertTrue(blk_insert["ok"])
        ins_handle = blk_insert["result"]["handle"]

        mod_res = json.loads(
            modify_entities(
                self.ctx,
                handles=[ins_handle],
                action="set_color",
                color=2,
            )
        )
        self.assertTrue(mod_res["ok"])
        self.assertEqual(mod_res["result"]["affected_count"], 1)

        out_dwg = "/tmp/autocad_mcp_exported_scene.dwg"
        if os.path.exists(out_dwg):
            os.remove(out_dwg)
        exp_res = json.loads(
            export_drawing(self.ctx, output_path=out_dwg, format="dwg", verify_reopen=True)
        )
        self.assertTrue(exp_res["ok"])
        self.assertHasDiagnosticFields(exp_res)
        self.assertTrue(exp_res["output_exists"])
        self.assertGreater(exp_res["output_size"], 1000)
        self.assertTrue(exp_res["reopen_verified"])
        self.assertEqual(exp_res["saved_dwg"], str(Path(out_dwg).resolve()))

    def test_05_chinese_path_save_reopen_and_encoding(self):
        """Verify P1-3 & P1-4: Chinese path save, reopen verification, and UTF-8 Chinese text/layer querying."""
        with tempfile.TemporaryDirectory(prefix="autocad_mcp_中文目录_") as tmpdir:
            cn_dwg = os.path.join(tmpdir, "夏房一层_平面图验证.dwg")
            lisp_build = """
            (progn
              (mcp:make-line (list 0.0 0.0 0.0) (list 5400.0 0.0 0.0) "一层墙体" 1)
              (mcp:make-text (list 500.0 500.0 0.0) "夏房一层客厅 5400x3600" 150.0 0.0 nil "中文标注层" 3)
              (mcp:get-doc-info)
            )
            """
            save_res = json.loads(
                run_accoreconsole_script(
                    self.ctx,
                    script_or_lisp=lisp_build,
                    is_lisp=True,
                    save_as_dwg=cn_dwg,
                    verify_reopen=True,
                )
            )
            self.assertTrue(save_res["ok"], f"Chinese DWG save failed: {save_res}")
            self.assertHasDiagnosticFields(save_res)
            self.assertTrue(save_res["output_exists"])
            self.assertGreater(save_res["output_size"], 1000)
            self.assertTrue(save_res["reopen_verified"])
            self.assertEqual(save_res["saved_dwg"], str(Path(cn_dwg).resolve()))

            # Query back the Chinese DWG via get_drawing_info and query_entities
            info_res = json.loads(get_drawing_info(self.ctx, input_dwg=cn_dwg))
            self.assertTrue(info_res["ok"])
            self.assertEqual(info_res["actual_mode"], "accoreconsole")
            self.assertEqual(info_res["result"]["dwgname"], "夏房一层_平面图验证.dwg")
            self.assertGreaterEqual(info_res["result"]["entity_count"], 2)

            q_res = json.loads(query_entities(self.ctx, layer="中文标注层", input_dwg=cn_dwg))
            self.assertTrue(q_res["ok"])
            self.assertEqual(q_res["result"]["total"], 1)
            self.assertEqual(q_res["result"]["entities"][0]["text"], "夏房一层客厅 5400x3600")

    def test_06_error_stages_and_unmasked_failures(self):
        """Verify P2-1: LISP errors, missing input files, and 0-entity DXFIN are reported as ok=False with error_code."""
        # 1. Missing input DWG
        missing_res = json.loads(
            get_drawing_info(self.ctx, input_dwg="/tmp/nonexistent_不存在的图纸_12345.dwg")
        )
        self.assertFalse(missing_res["ok"])
        self.assertHasDiagnosticFields(missing_res)
        self.assertEqual(missing_res["error_code"], "INPUT_DRAWING_NOT_FOUND")

        # 2. LISP runtime error in live GUI
        err_res = json.loads(
            eval_autolisp(self.ctx, code="(/ 1 0)", require_live=True)
        )
        self.assertFalse(err_res["ok"])
        self.assertHasDiagnosticFields(err_res)
        self.assertFalse(err_res["script_success"])
        self.assertEqual(err_res["error_code"], "LISP_RUNTIME_ERROR")

    def test_07_verified_dxf_open_in_autocad(self):
        """Verify P2-3: inspect_or_create_dxf(open_in_autocad=True) verifies document loaded state."""
        dxf_path = f"/tmp/autocad_mcp_verified_open_{os.getpid()}.dxf"
        try:
            spec = [
                {"type": "LINE", "start": [0, 0], "end": [1200, 0], "layer": "WALL", "color": 1},
                {"type": "CIRCLE", "center": [600, 400], "radius": 200, "layer": "COL", "color": 3},
            ]
            res = json.loads(
                inspect_or_create_dxf(
                    self.ctx,
                    dxf_path=dxf_path,
                    action="create",
                    entities_spec=spec,
                    open_in_autocad=True,
                    open_wait_seconds=12.0,
                )
            )
            self.assertTrue(res["ok"], f"Verified DXF open failed: {res}")
            self.assertHasDiagnosticFields(res)
            self.assertTrue(res["open_dispatched"])
            self.assertTrue(res["document_loaded_verified"])
            self.assertTrue(res["opened_in_autocad"])
            self.assertEqual(res["loaded_entity_count"], 2)

            # Close the temporary DXF tab in AutoCAD so the workspace drawing becomes active again
            send_command(self.ctx, command="_.CLOSE")
            time.sleep(0.8)
        finally:
            if os.path.exists(dxf_path):
                os.remove(dxf_path)


if __name__ == "__main__":
    unittest.main()
