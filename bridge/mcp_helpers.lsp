;;; ==========================================================================
;;; AutoCAD 2027 for Mac - MCP AutoLISP Helper Library (mcp_helpers.lsp)
;;; ==========================================================================

(defun mcp:json-escape (s / i len ch code out)
  (if (not (= (type s) 'STR))
    (setq s (vl-princ-to-string s))
  )
  (setq out ""
        i 1
        len (strlen s))
  (while (<= i len)
    (setq ch (substr s i 1)
          code (ascii ch))
    (cond
      ((= ch "\"") (setq out (strcat out "\\\"")))
      ((= ch "\\") (setq out (strcat out "\\\\")))
      ((= code 8)  (setq out (strcat out "\\b")))
      ((= code 9)  (setq out (strcat out "\\t")))
      ((= code 10) (setq out (strcat out "\\n")))
      ((= code 12) (setq out (strcat out "\\f")))
      ((= code 13) (setq out (strcat out "\\r")))
      ((< code 32) (setq out (strcat out " ")))
      (T           (setq out (strcat out ch)))
    )
    (setq i (1+ i))
  )
  out
)

(defun mcp:raw-json (json-str)
  (cons '__MCP_RAW_JSON__ json-str)
)

(defun mcp:to-json (val / tp items first-item cur)
  (setq tp (type val))
  (cond
    ((= val nil) "null")
    ((= val T) "true")
    ((= tp 'INT) (itoa val))
    ((= tp 'REAL)
      (let-real-str (rtos val 2 8))
    )
    ((= tp 'STR)
      (strcat "\"" (mcp:json-escape val) "\"")
    )
    ((= tp 'SYM)
      (strcat "\"" (mcp:json-escape (vl-symbol-name val)) "\"")
    )
    ((= tp 'ENAME)
      (strcat "{\"type\":\"ENAME\",\"handle\":\""
              (mcp:json-escape (cdr (assoc 5 (entget val))))
              "\",\"entity_type\":\""
              (mcp:json-escape (cdr (assoc 0 (entget val))))
              "\"}")
    )
    ((= tp 'PICKSET)
      (strcat "{\"type\":\"PICKSET\",\"count\":" (itoa (sslength val)) "}")
    )
    ((= tp 'LIST)
      (cond
        ((= (car val) '__MCP_RAW_JSON__)
          (cdr val)
        )
        ;; Dotted pair (a . b) where cdr is a non-nil atom
        ((and (cdr val) (atom (cdr val)))
          (strcat "[" (mcp:to-json (car val)) "," (mcp:to-json (cdr val)) "]")
        )
        ;; Proper list (or dotted list)
        (T
          (setq items "["
                first-item T
                cur val)
          (while (and cur (listp cur))
            (if (not first-item)
              (setq items (strcat items ","))
            )
            (setq first-item nil)
            (setq items (strcat items (mcp:to-json (car cur))))
            (setq cur (cdr cur))
            (if (and cur (atom cur))
              (progn
                (setq items (strcat items "," (mcp:to-json cur)))
                (setq cur nil)
              )
            )
          )
          (strcat items "]")
        )
      )
    )
    (T
      (strcat "\"" (mcp:json-escape (vl-princ-to-string val)) "\"")
    )
  )
)

(defun let-real-str (s / dot-pos trimmed)
  (if (vl-string-search "." s)
    (progn
      (setq trimmed (vl-string-right-trim "0" s))
      (if (= (substr trimmed (strlen trimmed) 1) ".")
        (strcat trimmed "0")
        trimmed
      )
    )
    s
  )
)

(defun mcp:open-utf8 (filepath mode / fp)
  (setq fp (vl-catch-all-apply 'open (list filepath mode "utf8")))
  (if (vl-catch-all-error-p fp)
    (open filepath mode)
    fp
  )
)

(defun mcp:read-file-text (filepath / fp line content)
  (setq content "")
  (if (setq fp (mcp:open-utf8 filepath "r"))
    (progn
      (while (setq line (read-line fp))
        (setq content (strcat content line "\n"))
      )
      (close fp)
    )
  )
  content
)

(defun mcp:write-atomic-json (resp-path json-payload / tmp-path fp)
  (setq tmp-path (strcat resp-path ".tmp"))
  (if (setq fp (mcp:open-utf8 tmp-path "w"))
    (progn
      (write-line json-payload fp)
      (close fp)
      (vl-file-delete resp-path)
      (vl-file-rename tmp-path resp-path)
      T
    )
    nil
  )
)

(defun mcp:current-doc-path ( / pfx nam)
  (setq pfx (getvar "DWGPREFIX")
        nam (getvar "DWGNAME"))
  (if (and pfx nam)
    (strcat pfx nam)
    (if nam nam "")
  )
)

(defun mcp:total-entity-count ( / ss)
  (setq ss (ssget "_X"))
  (if ss (sslength ss) 0)
)

(defun mcp:is-cmd-busy (cmd-active cmd-names)
  (or (and cmd-active (/= (logand cmd-active 11) 0))
      (and cmd-names (/= cmd-names "")))
)

(defun mcp:exec-file (req-path resp-path / code-str wrapped-expr parsed-res eval-res json-out old-cmdecho cmd-names cmd-active err-no doc-path is-busy busy-flag cmd-ok ent-before ent-after)
  (setq old-cmdecho (getvar "CMDECHO"))
  (setvar "CMDECHO" 0)
  (setvar "ERRNO" 0)
  (setq ent-before (mcp:total-entity-count))
  (setq code-str (mcp:read-file-text req-path))
  (setq wrapped-expr (strcat "(progn\n" code-str "\n)"))
  (setq parsed-res (vl-catch-all-apply 'read (list wrapped-expr)))
  (if (vl-catch-all-error-p parsed-res)
    (progn
      (setq cmd-names (getvar "CMDNAMES")
            cmd-active (getvar "CMDACTIVE")
            doc-path (mcp:current-doc-path)
            is-busy (mcp:is-cmd-busy cmd-active cmd-names)
            busy-flag (if is-busy "true" "false"))
      (setq json-out
        (strcat "{\"ok\":false"
                ",\"script_success\":false"
                ",\"command_success\":false"
                ",\"execution_stage\":\"lisp_parse_failed\""
                ",\"error_code\":\"LISP_SYNTAX_ERROR\""
                ",\"entity_count_before\":" (itoa ent-before)
                ",\"entity_count_after\":" (itoa ent-before)
                ",\"active_document\":\"" (mcp:json-escape doc-path) "\""
                ",\"command_names\":\"" (mcp:json-escape (if cmd-names cmd-names "")) "\""
                ",\"busy\":" busy-flag
                ",\"error\":\"LISP Syntax Error: "
                (mcp:json-escape (vl-catch-all-error-message parsed-res))
                "\"}")))
    (progn
      (setq eval-res (vl-catch-all-apply 'eval (list parsed-res)))
      (setq cmd-names (getvar "CMDNAMES")
            cmd-active (getvar "CMDACTIVE")
            err-no (getvar "ERRNO")
            ent-after (mcp:total-entity-count)
            doc-path (mcp:current-doc-path)
            is-busy (mcp:is-cmd-busy cmd-active cmd-names)
            busy-flag (if is-busy "true" "false"))
      (if (vl-catch-all-error-p eval-res)
        (setq json-out
          (strcat "{\"ok\":false"
                  ",\"script_success\":false"
                  ",\"command_success\":false"
                  ",\"execution_stage\":\"lisp_eval_failed\""
                  ",\"error_code\":\"LISP_RUNTIME_ERROR\""
                  ",\"errno\":" (itoa (if err-no err-no 0))
                  ",\"entity_count_before\":" (itoa ent-before)
                  ",\"entity_count_after\":" (itoa ent-after)
                  ",\"active_document\":\"" (mcp:json-escape doc-path) "\""
                  ",\"command_names\":\"" (mcp:json-escape (if cmd-names cmd-names "")) "\""
                  ",\"busy\":" busy-flag
                  ",\"error\":\"LISP Runtime Error: "
                  (mcp:json-escape (vl-catch-all-error-message eval-res))
                  "\"}"))
        (progn
          (setq cmd-ok (not is-busy))
          (setq json-out
            (strcat "{\"ok\":" (if cmd-ok "true" "false")
                    ",\"script_success\":true"
                    ",\"command_success\":" (if cmd-ok "true" "false")
                    ",\"execution_stage\":\"" (if cmd-ok "command_completed" "command_incomplete") "\""
                    ",\"error_code\":" (if cmd-ok "null" "\"COMMAND_STILL_ACTIVE\"")
                    ",\"errno\":" (itoa (if err-no err-no 0))
                    ",\"entity_count_before\":" (itoa ent-before)
                    ",\"entity_count_after\":" (itoa ent-after)
                    ",\"active_document\":\"" (mcp:json-escape doc-path) "\""
                    ",\"command_names\":\"" (mcp:json-escape (if cmd-names cmd-names "")) "\""
                    ",\"busy\":" busy-flag
                    (if cmd-ok
                      ""
                      (strcat ",\"error\":\"Command left active after script execution: "
                              (mcp:json-escape (if cmd-names cmd-names "")) "\""))
                    ",\"result\":"
                    (mcp:to-json eval-res)
                    "}"))
        )
      )
    )
  )
  (setvar "CMDECHO" old-cmdecho)
  (mcp:write-atomic-json resp-path json-out)
  (princ)
)

;;; ==========================================================================
;;; Document, Layer, Block & Entity Inspection Helpers
;;; ==========================================================================

(defun mcp:pt-to-json (pt)
  (if (and pt (listp pt))
    (strcat "["
            (let-real-str (rtos (car pt) 2 6)) ","
            (let-real-str (rtos (cadr pt) 2 6)) ","
            (let-real-str (rtos (if (caddr pt) (caddr pt) 0.0) 2 6))
            "]")
    "null"
  )
)

(defun mcp:get-doc-info ( / ss total-ents extmin extmax limmin limmax)
  (setq ss (ssget "_X"))
  (setq total-ents (if ss (sslength ss) 0))
  (setq extmin (getvar "EXTMIN")
        extmax (getvar "EXTMAX")
        limmin (getvar "LIMMIN")
        limmax (getvar "LIMMAX"))
  (mcp:raw-json
    (strcat
      "{"
      "\"dwgname\":\"" (mcp:json-escape (getvar "DWGNAME")) "\","
      "\"dwgprefix\":\"" (mcp:json-escape (getvar "DWGPREFIX")) "\","
      "\"acadver\":\"" (mcp:json-escape (getvar "ACADVER")) "\","
      "\"platform\":\"" (mcp:json-escape (getvar "PLATFORM")) "\","
      "\"clayer\":\"" (mcp:json-escape (getvar "CLAYER")) "\","
      "\"cecolor\":\"" (mcp:json-escape (getvar "CECOLOR")) "\","
      "\"celtype\":\"" (mcp:json-escape (getvar "CELTYPE")) "\","
      "\"insunits\":" (itoa (getvar "INSUNITS")) ","
      "\"lunits\":" (itoa (getvar "LUNITS")) ","
      "\"tilemode\":" (itoa (getvar "TILEMODE")) ","
      "\"entity_count\":" (itoa total-ents) ","
      "\"extmin\":" (mcp:pt-to-json extmin) ","
      "\"extmax\":" (mcp:pt-to-json extmax) ","
      "\"limmin\":" (mcp:pt-to-json limmin) ","
      "\"limmax\":" (mcp:pt-to-json limmax)
      "}"
    )
  )
)

(defun mcp:list-layers ( / tbl item items first-item name color ltype flags is-frozen is-locked is-off)
  (setq items "["
        first-item T
        tbl (tblnext "LAYER" T))
  (while tbl
    (setq name (cdr (assoc 2 tbl))
          color (cdr (assoc 62 tbl))
          ltype (cdr (assoc 6 tbl))
          flags (cdr (assoc 70 tbl)))
    (setq is-off (< color 0)
          is-frozen (= 1 (logand flags 1))
          is-locked (= 4 (logand flags 4)))
    (if (not first-item) (setq items (strcat items ",")))
    (setq first-item nil)
    (setq items
      (strcat items
        "{"
        "\"name\":\"" (mcp:json-escape name) "\","
        "\"color\":" (itoa (abs color)) ","
        "\"linetype\":\"" (mcp:json-escape ltype) "\","
        "\"is_off\":" (if is-off "true" "false") ","
        "\"is_frozen\":" (if is-frozen "true" "false") ","
        "\"is_locked\":" (if is-locked "true" "false")
        "}"
      )
    )
    (setq tbl (tblnext "LAYER"))
  )
  (mcp:raw-json (strcat items "]"))
)

(defun mcp:list-blocks ( / tbl items first-item name flags)
  (setq items "["
        first-item T
        tbl (tblnext "BLOCK" T))
  (while tbl
    (setq name (cdr (assoc 2 tbl))
          flags (cdr (assoc 70 tbl)))
    ;; Skip anonymous/model/paper space blocks starting with *
    (if (/= (substr name 1 1) "*")
      (progn
        (if (not first-item) (setq items (strcat items ",")))
        (setq first-item nil)
        (setq items
          (strcat items
            "{"
            "\"name\":\"" (mcp:json-escape name) "\","
            "\"flags\":" (itoa flags) ","
            "\"base_point\":" (mcp:pt-to-json (cdr (assoc 10 tbl)))
            "}"
          )
        )
      )
    )
    (setq tbl (tblnext "BLOCK"))
  )
  (mcp:raw-json (strcat items "]"))
)

(defun mcp:entity-to-json-str (en / ed etype handle layer color ltype p10 p11 rad start-ang end-ang txt bname verts pair vfirst closed)
  (setq ed (entget en))
  (if (not ed)
    "null"
    (progn
      (setq etype (cdr (assoc 0 ed))
            handle (cdr (assoc 5 ed))
            layer (cdr (assoc 8 ed))
            color (cdr (assoc 62 ed))
            ltype (cdr (assoc 6 ed))
            p10 (cdr (assoc 10 ed))
            p11 (cdr (assoc 11 ed))
            rad (cdr (assoc 40 ed))
            start-ang (cdr (assoc 50 ed))
            end-ang (cdr (assoc 51 ed))
            txt (cdr (assoc 1 ed))
            bname (cdr (assoc 2 ed)))
      (strcat
        "{"
        "\"handle\":\"" (mcp:json-escape (if handle handle "")) "\","
        "\"type\":\"" (mcp:json-escape (if etype etype "")) "\","
        "\"layer\":\"" (mcp:json-escape (if layer layer "0")) "\""
        (if color (strcat ",\"color\":" (itoa color)) "")
        (if ltype (strcat ",\"linetype\":\"" (mcp:json-escape ltype) "\"") "")
        (if (and p10 (/= etype "LWPOLYLINE")) (strcat ",\"point\":" (mcp:pt-to-json p10)) "")
        (if p11 (strcat ",\"end_point\":" (mcp:pt-to-json p11)) "")
        (if rad (strcat ",\"radius_or_height\":" (let-real-str (rtos rad 2 6))) "")
        (if start-ang (strcat ",\"start_angle_rad\":" (let-real-str (rtos start-ang 2 6))) "")
        (if end-ang (strcat ",\"end_angle_rad\":" (let-real-str (rtos end-ang 2 6))) "")
        (if txt (strcat ",\"text\":\"" (mcp:json-escape txt) "\"") "")
        (if bname (strcat ",\"name\":\"" (mcp:json-escape bname) "\"") "")
        (if (= etype "LWPOLYLINE")
          (progn
            (setq verts "[" vfirst T)
            (foreach pair ed
              (if (= (car pair) 10)
                (progn
                  (if (not vfirst) (setq verts (strcat verts ",")))
                  (setq vfirst nil)
                  (setq verts (strcat verts (mcp:pt-to-json (cdr pair))))
                )
              )
            )
            (setq closed (= 1 (logand 1 (if (cdr (assoc 70 ed)) (cdr (assoc 70 ed)) 0))))
            (strcat ",\"closed\":" (if closed "true" "false") ",\"vertices\":" verts "]")
          )
          ""
        )
        "}"
      )
    )
  )
)

(defun mcp:get-selection ( / ss count i items en)
  (setq ss (ssget "_I"))
  (if (not ss)
    (setq ss (ssget "_P"))
  )
  (if (not ss)
    (mcp:raw-json "{\"count\":0,\"entities\":[]}")
    (progn
      (setq count (sslength ss)
            i 0
            items "[")
      (while (< i count)
        (if (> i 0) (setq items (strcat items ",")))
        (setq en (ssname ss i))
        (setq items (strcat items (mcp:entity-to-json-str en)))
        (setq i (1+ i))
      )
      (mcp:raw-json (strcat "{\"count\":" (itoa count) ",\"entities\":" items "]}"))
    )
  )
)

(defun mcp:query-entities (etype-filter layer-filter limit / filter-list ss total count i items en)
  (setq filter-list nil)
  (if (and etype-filter (> (strlen etype-filter) 0))
    (setq filter-list (cons (cons 0 etype-filter) filter-list))
  )
  (if (and layer-filter (> (strlen layer-filter) 0))
    (setq filter-list (cons (cons 8 layer-filter) filter-list))
  )
  (setq ss (if filter-list (ssget "_X" filter-list) (ssget "_X")))
  (if (not ss)
    (mcp:raw-json "{\"total\":0,\"returned\":0,\"entities\":[]}")
    (progn
      (setq total (sslength ss)
            count (if (and limit (> limit 0) (< limit total)) limit total)
            i 0
            items "[")
      (while (< i count)
        (if (> i 0) (setq items (strcat items ",")))
        (setq en (ssname ss i))
        (setq items (strcat items (mcp:entity-to-json-str en)))
        (setq i (1+ i))
      )
      (mcp:raw-json
        (strcat "{\"total\":" (itoa total)
                ",\"returned\":" (itoa count)
                ",\"entities\":" items "]}")
      )
    )
  )
)

;;; ==========================================================================
;;; High-Precision Geometry Creation & Modification Helpers (entmakex-based)
;;; ==========================================================================

(defun mcp:ensure-layer (layer-name color ltype / existing elist)
  (if (or (not layer-name) (= layer-name ""))
    (setq layer-name (getvar "CLAYER"))
  )
  (if (not (tblsearch "LAYER" layer-name))
    (progn
      (setq elist
        (list
          '(0 . "LAYER")
          '(100 . "AcDbSymbolTableRecord")
          '(100 . "AcDbLayerTableRecord")
          (cons 2 layer-name)
          '(70 . 0)
          (cons 62 (if color color 7))
          (cons 6 (if (and ltype (/= ltype "")) ltype "Continuous"))
        )
      )
      (entmakex elist)
    )
  )
  layer-name
)

(defun mcp:make-line (p1 p2 layer color / lyr elist en)
  (setq lyr (mcp:ensure-layer layer color nil))
  (setq elist
    (list
      '(0 . "LINE")
      (cons 8 lyr)
      (cons 10 p1)
      (cons 11 p2)
    )
  )
  (if color (setq elist (append elist (list (cons 62 color)))))
  (setq en (entmakex elist))
  (mcp:raw-json (mcp:entity-to-json-str en))
)

(defun mcp:make-circle (center radius layer color / lyr elist en)
  (setq lyr (mcp:ensure-layer layer color nil))
  (setq elist
    (list
      '(0 . "CIRCLE")
      (cons 8 lyr)
      (cons 10 center)
      (cons 40 radius)
    )
  )
  (if color (setq elist (append elist (list (cons 62 color)))))
  (setq en (entmakex elist))
  (mcp:raw-json (mcp:entity-to-json-str en))
)

(defun mcp:make-arc (center radius start-deg end-deg layer color / lyr start-rad end-rad elist en)
  (setq lyr (mcp:ensure-layer layer color nil)
        start-rad (* pi (/ start-deg 180.0))
        end-rad (* pi (/ end-deg 180.0)))
  (setq elist
    (list
      '(0 . "ARC")
      (cons 8 lyr)
      (cons 10 center)
      (cons 40 radius)
      (cons 50 start-rad)
      (cons 51 end-rad)
    )
  )
  (if color (setq elist (append elist (list (cons 62 color)))))
  (setq en (entmakex elist))
  (mcp:raw-json (mcp:entity-to-json-str en))
)

(defun mcp:make-lwpolyline (pts closed width layer color / lyr elist en)
  (setq lyr (mcp:ensure-layer layer color nil))
  (setq elist
    (list
      '(0 . "LWPOLYLINE")
      '(100 . "AcDbEntity")
      (cons 8 lyr)
      '(100 . "AcDbPolyline")
      (cons 90 (length pts))
      (cons 70 (if closed 1 0))
      (cons 43 (if width width 0.0))
    )
  )
  (if color (setq elist (append elist (list (cons 62 color)))))
  (setq elist
    (append elist
      (mapcar '(lambda (p) (cons 10 (list (car p) (cadr p)))) pts)
    )
  )
  (setq en (entmakex elist))
  (mcp:raw-json (mcp:entity-to-json-str en))
)

(defun mcp:make-text (pt content height rotation-deg multiline layer color / lyr rot-rad elist en)
  (setq lyr (mcp:ensure-layer layer color nil)
        rot-rad (* pi (/ (if rotation-deg rotation-deg 0.0) 180.0)))
  (if multiline
    (setq elist
      (list
        '(0 . "MTEXT")
        '(100 . "AcDbEntity")
        (cons 8 lyr)
        '(100 . "AcDbMText")
        (cons 10 pt)
        (cons 40 height)
        (cons 1 content)
        (cons 50 rot-rad)
      )
    )
    (setq elist
      (list
        '(0 . "TEXT")
        (cons 8 lyr)
        (cons 10 pt)
        (cons 40 height)
        (cons 1 content)
        (cons 50 rot-rad)
      )
    )
  )
  (if color (setq elist (append elist (list (cons 62 color)))))
  (setq en (entmakex elist))
  (mcp:raw-json (mcp:entity-to-json-str en))
)

(defun mcp:insert-block (block-name pt scale-x scale-y scale-z rotation-deg layer color / lyr rot-rad elist en)
  (setq lyr (mcp:ensure-layer layer color nil)
        rot-rad (* pi (/ (if rotation-deg rotation-deg 0.0) 180.0)))
  (setq elist
    (list
      '(0 . "INSERT")
      (cons 8 lyr)
      (cons 2 block-name)
      (cons 10 pt)
      (cons 41 (if scale-x scale-x 1.0))
      (cons 42 (if scale-y scale-y 1.0))
      (cons 43 (if scale-z scale-z 1.0))
      (cons 50 rot-rad)
    )
  )
  (if color (setq elist (append elist (list (cons 62 color)))))
  (setq en (entmakex elist))
  (mcp:raw-json (mcp:entity-to-json-str en))
)

(princ "\n[AutoCAD-MCP] mcp_helpers.lsp loaded.\n")
(princ)
