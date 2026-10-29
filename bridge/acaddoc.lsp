;;; ==========================================================================
;;; AutoCAD 2027 for Mac - MCP Bridge Document Auto-Loader (acaddoc.lsp)
;;; ==========================================================================

(vl-catch-all-apply
  '(lambda ()
     (if (/= (getvar "SECURELOAD") 0)
       (setvar "SECURELOAD" 0)
     )
     (load "/Users/charlie/.gemini/antigravity/scratch/autocad-mcp/bridge/mcp_helpers.lsp")
     ;; Only load the native TCP bridge inside GUI AutoCAD (where acapp.crx is loaded),
     ;; skipping headless AcCoreConsole subprocesses.
     (if (and (or (member "acapp.crx" (arx)) (member "AcApp.crx" (arx)))
              (not (member "autocad_mcp_bridge.crx" (arx))))
       (arxload "/Users/charlie/.gemini/antigravity/scratch/autocad-mcp/bridge/autocad_mcp_bridge.crx")
     )
   )
  nil
)
(princ)
