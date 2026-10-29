import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

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
)


class TestAutoCADMCP(unittest.TestCase):
    def setUp(self):
        self.ctx = MagicMock()
        self.ctx.request_id = 1

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
            self.assertEqual(create_res["total_entities"], 4)

            inspect_res = json.loads(
                inspect_or_create_dxf(
                    self.ctx,
                    dxf_path=dxf_file,
                    action="inspect",
                    open_in_autocad=False,
                )
            )
            self.assertTrue(inspect_res["ok"])
            self.assertIn("WALL", inspect_res["layers"])
            self.assertEqual(inspect_res["entity_counts_by_type"]["LINE"], 1)

    def test_03_live_or_console_drawing_workflow(self):
        status = json.loads(get_connection_status(self.ctx, auto_bootstrap=True))
        self.assertTrue(status["accoreconsole_available"])
        self.assertTrue(status["live_bridge_connected"])

        info = json.loads(get_drawing_info(self.ctx))
        self.assertTrue(info["ok"])
        self.assertEqual(info["mode"], "live_gui")
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
        self.assertEqual(rect_res["mode"], "live_gui")
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
        self.assertEqual(circ_res["mode"], "live_gui")
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
        self.assertEqual(line_res["mode"], "live_gui")
        self.assertEqual(line_res["result"]["type"], "LINE")

        # Draw room label text
        txt_res = json.loads(
            draw_text(
                self.ctx,
                position=[1800.0, 1400.0, 0.0],
                text="AutoCAD 2027 MCP Live Studio",
                height=120.0,
                layer="A-ANNO-TEXT",
                color=4,
            )
        )
        self.assertTrue(txt_res["ok"])
        self.assertEqual(txt_res["mode"], "live_gui")
        self.assertEqual(txt_res["result"]["type"], "TEXT")

        # Zoom extents in live AutoCAD 2027 GUI
        zoom_res = json.loads(eval_autolisp(self.ctx, code='(command "_.ZOOM" "_E")'))
        self.assertTrue(zoom_res["ok"])

        # Query entities on layer A-WALL
        q_res = json.loads(query_entities(self.ctx, layer="A-WALL"))
        self.assertTrue(q_res["ok"])
        self.assertGreaterEqual(q_res["result"]["total"], 1)

    def test_04_blocks_layers_dimensions_modify_and_export(self):
        # 1. Layer management
        layers_res = json.loads(
            manage_layer(self.ctx, action="create", name="A-FURN", color=6)
        )
        self.assertTrue(layers_res["ok"])
        layer_names = [l["name"] for l in layers_res["result"]]
        self.assertIn("A-FURN", layer_names)

        # 2. Draw arc & dimension
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

        # 3. Create entities, turn into a Block, and insert another instance
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

        # 4. Modify entity (move & recolor)
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

        # 5. Export active drawing to DWG in /tmp
        out_dwg = "/tmp/autocad_mcp_exported_scene.dwg"
        if os.path.exists(out_dwg):
            os.remove(out_dwg)
        exp_res = json.loads(
            export_drawing(self.ctx, output_path=out_dwg, format="dwg")
        )
        self.assertTrue(exp_res["ok"])
        self.assertTrue(os.path.exists(out_dwg))

        # 6. Headless AcCoreConsole inspection of the exported DWG
        headless_res = json.loads(
            run_accoreconsole_script(
                self.ctx,
                script_or_lisp="(mcp:get-doc-info)",
                is_lisp=True,
                input_dwg=out_dwg,
            )
        )
        self.assertTrue(headless_res["ok"])
        self.assertEqual(headless_res["mode"], "accoreconsole")
        self.assertGreaterEqual(headless_res["result"]["entity_count"], 5)


if __name__ == "__main__":
    unittest.main()
