(* ::Package:: *)
(* headless_notebook.wl — notebook sessions with no front end *)
(*
   The addon's notebook commands drive a LIVE WINDOW: NotebookOpen,
   CreateDocument, SelectedNotebook. All of them need a front end, so on a
   headless host (a container, an SSH session, a batch node) every one of them
   fails and the server can only evaluate loose expressions.

   Here a "notebook" is instead the Notebook[...] expression on disk. That is
   enough to do the thing that actually matters headlessly: replay a real .nb
   cell by cell in the persistent kernel, in document order, with results and
   messages captured per cell.

   Design notes:
   - Cells are located by POSITION in the original expression, never by
     rebuilding it. Round-tripping through a hand-rolled box-to-text converter
     is what corrupts \[Gamma] and friends; we never do that conversion.
   - A cell is evaluated exactly as Shift+Enter would:
       ToExpression[content /. BoxData[b_] :> b, StandardForm]
     Retyping code read off a preview is a transcription step with no safety
     net, and its failure mode (silently unevaluated) is easy to misread.
   - Nothing here opens a front end, and nothing rasterises by default.
*)

BeginPackage["MCPHeadlessNotebook`"];

MCPOpen::usage = "MCPOpen[id, path] loads a .nb into a headless session.";
MCPFindDefining::usage = "MCPFindDefining[id, symbol] lists the cells that assign a symbol.";
MCPCells::usage = "MCPCells[id, offset, limit, includeContent, style] lists cells; style \"\" means all.";
MCPEvaluateCell::usage = "MCPEvaluateCell[id, index, timeout] evaluates one cell.";
MCPEvaluateRange::usage = "MCPEvaluateRange[id, from, to, timeout, stopOnError] evaluates a span of cells.";
MCPEvaluateInput::usage = "MCPEvaluateInput[id, ordinal, timeout, writeOutputs, sentinel] evaluates the nth input cell, counting from the top.";
MCPFileDependencies::usage = "MCPFileDependencies[id] reports every file a notebook reads or writes, including from commented cells.";
MCPInputDigests::usage = "MCPInputDigests[id] hashes the stored boxes of every input cell, by ordinal.";
MCPOutputProvenance::usage = "MCPOutputProvenance[id] reports which replay child wrote each output cell.";
MCPWriteCell::usage = "MCPWriteCell[id, content, style, position, anchor, recordTag, evaluatable] inserts a cell. recordTag and evaluatable (\"True\" | \"False\" | \"\") are optional; a non-empty evaluatable stamps Evaluatable on the cell.";
MCPDeleteCell::usage = "MCPDeleteCell[id, index] removes a cell.";
MCPReplaceCell::usage = "MCPReplaceCell[id, index, content] replaces one cell's content, keeping its style and options.";
MCPSave::usage = "MCPSave[id, path] writes the session's notebook expression to disk.";
MCPCreate::usage = "MCPCreate[id, path, title] starts an empty headless notebook.";
MCPClose::usage = "MCPClose[id] discards a session.";
MCPList::usage = "MCPList[] lists open headless sessions.";
MCPRenderCell::usage = "MCPRenderCell[id, index, dpi] rasterises one cell to PNG bytes via the front end.";
MCPRenderExpression::usage = "MCPRenderExpression[code, dpi] rasterises an expression to PNG bytes via the front end.";
MCPVerifySelf::usage = "MCPVerifySelf[id] checks the session document's own cell labels for self-consistency; needs no reference.";
MCPVerifyAgainst::usage = "MCPVerifyAgainst[id, refPath] compares the session's replayed document against a reference notebook and reports structural discrepancies.";
MCPExportMarkdown::usage = "MCPExportMarkdown[id, path, texMath] writes the session notebook as all-text Markdown (no rasterised outputs).";
MCPExportNotebook::usage = "MCPExportNotebook[id, path, openGroups] writes the session notebook through the front end (PDF, PNG, ...).";
MCPFrontEndAvailable::usage = "MCPFrontEndAvailable[] reports whether a headless front end can be started.";
MCPAnnotateCell::usage = "MCPAnnotateCell[id, index, evaluatable, reason] sets Evaluatable and stamps an annotation reason on a cell.";
MCPReadBack::usage = "MCPReadBack[id] reads all cells with full metadata: source digest, TaggingRules, Evaluatable, CellTags.";
MCPFinalize::usage = "MCPFinalize[id, path] saves, then runs NotebookEvaluate via UsingFrontEnd so cells get native In[n]/Out[n] labels.";
MCPBindNotebookDirectory::usage = "MCPBindNotebookDirectory[dir, path] makes NotebookDirectory[] and friends resolve without a front end.";

Begin["`Private`"];

If[!ValueQ[$Sessions], $Sessions = <||>];
(* Output longer than this is truncated per cell; the full value stays in the
   kernel, so a caller that needs it can ask for the variable directly. *)
If[!ValueQ[$MaxOutputChars], $MaxOutputChars = 20000];

truncate[s_String] := If[StringLength[s] > $MaxOutputChars,
  StringTake[s, $MaxOutputChars] <> "\n... [truncated, " <>
    ToString[StringLength[s] - $MaxOutputChars] <> " more chars]",
  s
];
truncate[x_] := truncate[ToString[x]];

(* Compact: the default pretty-printer emits tabs and newlines, which have to
   survive OutputForm rendering and the transport intact before Python can
   parse them. One line has no such failure mode. *)
(* Bytes, not characters. ExportString returns the *encoded bytes rendered
   as characters*, so a \[Gamma] arrives on the other side as two Latin-1
   characters and decodes to mojibake -- and no CharacterEncoding option
   changes that (measured: UTF8 and PrintableASCII are byte-identical).
   ExportByteArray keeps bytes as bytes and the decode happens once, in
   Python, where the encoding is known. *)
json[assoc_] := ExportByteArray[assoc, "RawJSON", "Compact" -> True];
err[msg_String, extra_: <||>] := json[Join[<|"success" -> False, "error" -> msg|>, extra]];
ok[assoc_] := json[Join[<|"success" -> True|>, assoc]];

(* ------------------------------------------------------------------------ *)
(* Cell addressing                                                           *)
(* ------------------------------------------------------------------------ *)

(* Positions of content cells, in document order. A Cell whose first argument
   is CellGroupData is a collapsible GROUP WRAPPER, not content — it exists to
   hold other cells, and evaluating or editing it is never what the caller
   means. Position's depth-first order matches document order here. *)
leafPositions[nb_] := Select[
  Position[nb, _Cell],
  !MatchQ[Extract[nb, #], Cell[_CellGroupData, ___]] &
];

(* Cell[content, style, opts...] — the style is the SECOND argument. Scanning
   level 1 for the first string instead returns the CONTENT of a prose cell
   (Cell["some text", "Text"]), which silently mislabels every text cell. *)
cellStyle[c_] := If[Length[c] >= 2 && StringQ[c[[2]]], c[[2]], "Unknown"];

(* Plain text of a cell, for previews and prose cells. Strings buried in boxes
   are joined; this is deliberately approximate and never used as the source
   for evaluation. *)
cellText[c_] := Module[{content},
  content = If[Length[c] >= 1, First[c], ""];
  Which[
    StringQ[content], content,
    True, StringJoin[Cases[content, _String, Infinity]]
  ]
];

executableQ[c_] := MemberQ[{"Input", "Code"}, cellStyle[c]];

(* HoldRest is load-bearing: without it WL evaluates `body` before sessionOr is
   entered, so the guard runs AFTER the thing it guards. Read-only callers get
   away with it, but MCPClose deletes the session in its body and the guard then
   reports the session missing - closing always "failed" while actually
   succeeding. Holding the body makes the check happen first, for all callers. *)
SetAttributes[sessionOr, HoldRest];
sessionOr[id_, body_] := If[KeyExistsQ[$Sessions, id], body, err["No such headless notebook session: " <> ToString[id]]];

(* ------------------------------------------------------------------------ *)
(* Front-end-free notebook context                                           *)
(* ------------------------------------------------------------------------ *)

(* Without a front end NotebookDirectory[] returns $Failed, so a cell that
   opens with SetDirectory[NotebookDirectory[]] aborts the whole replay.
   Patching each notebook by hand is the usual workaround; binding the symbols
   once is the same fix applied in one place. These symbols carry no meaning
   headless, so overriding them costs nothing. *)
MCPBindNotebookDirectory[dir_String, path_String] := Module[{},
  (* Define the EXACT zero-argument forms. A NotebookDirectory[___] catch-all
     is less specific than the built-in NotebookDirectory[] rule, so it sorts
     after it and never fires; the exact form replaces the built-in rule
     outright, which is what we want. *)
  Quiet[
    Unprotect[System`NotebookDirectory, System`NotebookFileName];
    System`NotebookDirectory[] := dir;
    System`NotebookDirectory[_] := dir;
    System`NotebookFileName[] := path;
    System`NotebookFileName[_] := path;
    Protect[System`NotebookDirectory, System`NotebookFileName];
  ];
  dir
];

(* ------------------------------------------------------------------------ *)
(* Sessions                                                                  *)
(* ------------------------------------------------------------------------ *)

MCPOpen[id_String, path_String] := Module[{nb, abs, dir},
  abs = ExpandFileName[path];
  If[!FileExistsQ[abs], Return[err["File not found", <|"path" -> abs|>]]];
  (* Get, not Import: a .nb file IS a Notebook[...] expression, and Get returns
     it verbatim with BoxData intact. Import normalises some of that away. *)
  nb = Quiet[Check[Get[abs], $Failed]];
  If[!MatchQ[nb, _Notebook],
    Return[err[
      "File did not parse as a Notebook expression (Get returns Null on a truncated or malformed file)",
      <|"path" -> abs, "head" -> ToString[Head[nb]]|>
    ]]
  ];
  dir = DirectoryName[abs];
  $Sessions[id] = <|"path" -> abs, "dir" -> dir, "nb" -> nb, "dirty" -> False,
                    "posCache" -> None, "ordCache" -> None|>;
  ok[<|
    "id" -> id, "path" -> abs, "directory" -> dir,
    "cell_count" -> Length[leafPositions[nb]],
    "code_cells" -> Count[Extract[nb, #] & /@ leafPositions[nb], _?executableQ],
    "headless" -> True
  |>]
];

MCPCreate[id_String, path_String, title_String] := Module[{nb, cells},
  cells = If[title === "", {}, {Cell[title, "Title"]}];
  nb = Notebook[cells];
  $Sessions[id] = <|
    "path" -> If[path === "", "", ExpandFileName[path]],
    "dir" -> If[path === "", Directory[], DirectoryName[ExpandFileName[path]]],
    "nb" -> nb, "dirty" -> True, "posCache" -> None, "ordCache" -> None
  |>;
  ok[<|"id" -> id, "path" -> $Sessions[id, "path"], "cell_count" -> Length[cells], "headless" -> True|>]
];

MCPClose[id_String] := sessionOr[id, ($Sessions = KeyDrop[$Sessions, id]; ok[<|"id" -> id, "closed" -> True|>])];

MCPList[] := ok[<|
  "notebooks" -> KeyValueMap[
    <|"id" -> #1, "path" -> #2["path"], "cell_count" -> Length[leafPositions[#2["nb"]]], "dirty" -> #2["dirty"]|> &,
    $Sessions
  ],
  "headless" -> True
|>];

(* Kept so a kernel still holding an older caller keeps working. *)
MCPCells[id_String, offset_Integer, limit_Integer, includeContent : (True | False)] :=
  MCPCells[id, offset, limit, includeContent, ""];

MCPCells[id_String, offset_Integer, limit_Integer, includeContent : (True | False), style_String] :=
  sessionOr[id, Module[{nb, pos, keep, cells, slice, upper},
    nb = $Sessions[id, "nb"];
    pos = leafPositions[nb];
    (* Filter BEFORE slicing: filtering a page would silently drop matches that
       fall outside it. Indices stay notebook-wide, because callers evaluate by
       them - renumbering to the filtered order would break that. *)
    keep = Range[Length[pos]];
    If[style =!= "", keep = Select[keep, cellStyle[Extract[nb, pos[[#]]]] === style &]];
    upper = If[limit <= 0, Length[keep], Min[Length[keep], offset + limit]];
    slice = If[upper >= offset + 1, Take[keep, {offset + 1, upper}], {}];
    cells = Table[
      Module[{c = Extract[nb, pos[[i]]], txt},
        txt = cellText[c];
        Join[
          <|
            "index" -> i - 1,
            "style" -> cellStyle[c],
            "executable" -> executableQ[c],
            "chars" -> StringLength[txt]
          |>,
          If[includeContent, <|"content" -> truncate[txt]|>, <|"preview" -> StringTake[txt, Min[80, StringLength[txt]]]|>]
        ]
      ],
      {i, slice}
    ];
    ok[<|"id" -> id, "total" -> Length[keep], "offset" -> offset,
        "style" -> style, "cells" -> cells|>]
  ]];

(* ------------------------------------------------------------------------ *)
(* Evaluation                                                                *)
(* ------------------------------------------------------------------------ *)

(* Evaluate one cell the way Shift+Enter would, capturing value, Print output,
   messages and timing. Print is redirected to a temp file rather than left on
   stdout: package-heavy notebooks print constantly, and on the cold transport
   that text lands in the middle of the JSON the caller has to parse. *)
evalCell[c_, dir_String, path_String, timeout_] := Module[
  {boxes, res, msgs, t0, printed = "", stream, tmp, aborted = False, cellAborted = False},

  If[!executableQ[c],
    Return[<|"success" -> True, "skipped" -> True, "reason" -> "not an Input/Code cell"|>]
  ];

  MCPBindNotebookDirectory[dir, path];
  boxes = First[c] /. BoxData[b_] :> b;
  tmp = FileNameJoin[{$TemporaryDirectory, "mcp-print-" <> ToString[$ProcessID] <> "-" <> ToString[RandomInteger[10^9]] <> ".txt"}];
  t0 = AbsoluteTime[];

  (* OutputForm, not the OpenWrite default of InputForm: with the default a
     cell that printed one was recorded as "one", quoted, so the replay's
     account of what a cell printed differed from the same Print seen
     through evaluate(). The record has to match what happened. *)
  stream = Quiet[Check[OpenWrite[tmp, FormatType -> OutputForm], $Failed]];
  (* CheckAbort, not just TimeConstrained. TimeConstrained catches a cell that
     runs too long; it does NOT catch a cell that calls Abort[] itself, and an
     abort raised inside one cell propagates out of the whole Module and kills
     the entire range evaluation. The caller then gets no payload at all, the
     transport reads a bare symbol where a ByteArray should be, and the link
     desyncs. Any Abort[] does this -- a package guard refusing to load twice, a
     failed assertion, an error handler -- and the cause is irrelevant to the
     containment: one cell aborting must cost that cell, not the replay.
     Measured: an aborting setup cell took 0.3s and the whole session with it. *)
  Block[{$MessageList = {}},
    res = CheckAbort[
      If[stream === $Failed,
        TimeConstrained[ToExpression[boxes, StandardForm], timeout, $MCPAborted],
        Block[{$Output = {stream}}, TimeConstrained[ToExpression[boxes, StandardForm], timeout, $MCPAborted]]
      ],
      $MCPCellAborted
    ];
    msgs = $MessageList;
  ];
  If[stream =!= $Failed,
    Quiet[Close[stream]];
    printed = Quiet[Check[Import[tmp, "Text"], ""]];
    Quiet[DeleteFile[tmp]];
  ];
  If[!StringQ[printed], printed = ""];
  aborted = (res === $MCPAborted);
  cellAborted = (res === $MCPCellAborted);
  (* Kept out of the returned association on purpose: the assoc is JSON-encoded
     and an arbitrary expression is not encodable. The caller reads this
     immediately after, only when it is writing outputs back. *)
  $lastResult = If[aborted || cellAborted, Null, res];

  <|
    "success" -> !(aborted || cellAborted),
    "timed_out" -> aborted,
    "aborted" -> cellAborted,
    (* A plain string, never Nothing: Nothing is a Symbol, RawJSON cannot encode
       a symbol, and the export would fail for every cell in the range. *)
    (* Provisional. evalCell cannot tell WHOSE abort this was: a user's abort
       and a cell's own Abort[] are indistinguishable here, and only the caller
       holding the sentinel knows. The span loop rewrites this when it finds the
       sentinel set. Saying "the cell called Abort[]" unconditionally was a claim
       about the science that the kernel had no way to support. *)
    "reason" -> If[cellAborted,
      "aborted; the source of the abort has not been established here", ""],
    "output" -> If[aborted || cellAborted, "", truncate[ToString[res, InputForm]]],
    "printed" -> truncate[printed],
    "messages" -> (ToString[#, InputForm] & /@ msgs),
    "timing_ms" -> Round[(AbsoluteTime[] - t0) * 1000]
  |>
];

MCPEvaluateCell[id_String, index_Integer, timeout_] :=
  sessionOr[id, Module[{nb, pos, c},
    nb = $Sessions[id, "nb"];
    pos = leafPositions[nb];
    If[index < 0 || index >= Length[pos],
      Return[err["Cell index out of range", <|"index" -> index, "total" -> Length[pos]|>]]
    ];
    c = Extract[nb, pos[[index + 1]]];
    ok[Join[<|"id" -> id, "index" -> index, "style" -> cellStyle[c]|>,
            evalCell[c, $Sessions[id, "dir"], $Sessions[id, "path"], timeout]]]
  ]];

(* Did this abort come from the client rather than from the cell?

   The client touches the sentinel immediately before signalling the abort. The
   check consumes it, so one abort stops one span and a later self-aborting cell
   is not mistaken for a second user abort. *)
userAbortedQ[""] := False;
userAbortedQ[path_String] := Module[{hit},
  hit = TrueQ[Quiet[Check[FileExistsQ[path], False]]];
  If[hit, Quiet[DeleteFile[path]]];
  hit
];

(* Which input is this, counting from the top?

   The raw cell index is not usable as an identity across calls: writing outputs
   inserts cells, so index 229 before a call and index 229 after it can be
   different cells. The input ORDINAL is stable, because inserting an Output
   never changes which input a cell is. That makes it the right key for noticing
   that a span has already labelled a cell. *)
(* The document is traversed once per change, not once per cell.

   leafPositions is Position over the whole notebook expression: 115 ms on a
   994-cell document, and a per-cell replay was paying it at least twice per
   cell, which is most of the 400 ms per cell that Phase D measured.

   The cache is only safe if it cannot outlive the document it describes, and
   the way it would go wrong is precisely the bug ordinal addressing exists to
   avoid: writing an output inserts a cell, every later position shifts, and a
   stale cache would then evaluate the wrong cell. So there is exactly one way
   to put a notebook into a session -- setNotebook -- and it clears the cache.
   Nothing assigns "nb" directly. *)
setNotebook[id_String, nb_] := (
  $Sessions[id, "nb"] = nb;
  $Sessions[id, "posCache"] = None;
  $Sessions[id, "ordCache"] = None;
  nb);

sessionPos[id_String] := Module[{cached},
  cached = Lookup[$Sessions[id], "posCache", None];
  If[cached === None,
    cached = leafPositions[$Sessions[id, "nb"]];
    $Sessions[id, "posCache"] = cached];
  cached];

sessionOrds[id_String] := Module[{cached},
  cached = Lookup[$Sessions[id], "ordCache", None];
  If[cached === None,
    cached = inputOrdinals[$Sessions[id, "nb"], sessionPos[id]];
    $Sessions[id, "ordCache"] = cached];
  cached];

inputOrdinals[nb_, pos_] := Module[{k = 0},
  Table[If[MemberQ[{"Input", "Code"}, cellStyle[Extract[nb, pos[[q]]]]], ++k, 0],
    {q, Length[pos]}]
];

(* Replay a span in document order. stopOnError halts at the first cell that
   times out, so a long notebook does not keep burning kernel time after the
   state it depends on has already failed to materialise.

   A cell aborted BY THE USER halts the span, regardless of stopOnError. evalCell
   contains aborts so the span still returns a payload and the link stays in
   sync -- but containment must not mean "carry on". The kernel cannot tell a
   user's abort() from a cell calling Abort[] itself: both arrive as
   user-initiated aborts and CheckAbort absorbs both identically. Continuing
   would make abort() useless on exactly the long replays it exists for --
   measured before this: abort() during an 8-cell span aborted one cell and ran
   the remaining seven to completion. So the client touches a sentinel file just
   before it signals, and userAbortedQ reads it: marked means stop, unmarked
   means the cell aborted itself and the span continues. *)
(* Write a computed value back into the session document as an Output cell.
   Returns {position, cell, "replaced"|"inserted"} for deferred application --
   applying edits during the loop would invalidate the position list the loop is
   iterating over. *)
(* In[n]:= / Out[n]= come from a cell's CellLabel option, not from anything the
   kernel emits. A replay drives ToExpression directly rather than going through
   the main loop, so $Line never advances and nothing is labelled -- the exported
   record then shows inputs and outputs with no way to tell which produced which.
   We number them ourselves, in replay order, and stamp both halves. *)
withLabel[c_Cell, lbl_String] :=
  Append[DeleteCases[c, (CellLabel -> _), {1}], CellLabel -> lbl];

(* What a multi-statement cell should show.

   A cell holding several statements is stored as BoxData[{stmt1, stmt2, ...}]
   and ToExpression returns one value per statement, so the raw result of
   "FAPatch[...]; x = ...; Length[y]" is {Null, Null, 4096, Null, Null}. A
   notebook shows only 4096: the ";"-terminated statements produce nothing.
   Writing the raw list back put strings of Nulls into the record -- 194 of this
   notebook's 276 input cells are multi-statement, so it affected most of them.

   Counting the statements in the boxes is what makes this safe. Only when the
   result is a list of exactly that length is it a per-statement list, so a cell
   whose single statement genuinely evaluates to {1, Null, 3} keeps its Nulls.

   Several surviving values are joined into one output cell; a notebook would
   give each its own Out[]. Rare in practice, and better than dropping them. *)
(* A multi-statement cell stores its statements interleaved with the newline
   boxes that separate them: BoxData[{stmt, "\n", stmt, "\n", stmt}]. Counting
   the list length counts the separators too, which is how a six-statement cell
   came to be scored as one and the next cell was labelled In[2] where the front
   end writes In[7].

   Verified against the source notebook's own stored labels: an 11-element first
   cell (6 statements, 5 separators) is In[1] and is followed by In[7]; a
   5-element cell (3 statements) at In[9] is followed by In[12]. *)
separatorQ[b_String] := StringMatchQ[b, ("\n" | "\[IndentingNewLine]" | "\r\n" | " " | "\t") ..];
separatorQ[_] := False;

(* Indices, within the stored box list, of the boxes that are actually
   statements. ToExpression returns one value per box element -- separators
   evaluate to Null -- so these indices select a statement's value AND give its
   ordinal, which is what numbers its Out[]. *)
statementPositions[c_] := Module[{boxes},
  If[!MatchQ[First[c], BoxData[_List]], Return[{1}]];
  boxes = First[c][[1]];
  Select[Range[Length[boxes]], !separatorQ[boxes[[#]]] &]
];

statementCount[c_] := Length[statementPositions[c]];

(* The list of results this cell should SHOW, one per non-suppressed statement.

   A multi-statement cell produces one Output cell per result, each with its own
   number -- verified against this notebook's own stored labels, written by a
   real front end: an input at In[110] with three statements is followed by
   Out[110] and Out[111] as two separate cells, and one at In[115] with five
   statements by Out[115], Out[116] and Out[117]. Returning them joined into a
   single list instead put "{8, 0, 0}" on the page where the notebook shows
   three separate results, which is not what was computed. *)
resultsForCell[c_, value_] := Module[{boxes, sp},
  If[MatchQ[First[c], BoxData[_List]],
    boxes = First[c][[1]];
    sp = statementPositions[c];
    (* One value per BOX, so the guard compares against the box count, not the
       statement count. A single statement that genuinely evaluates to a list of
       the same length is the case this protects: it takes the branch below and
       keeps its own Nulls. *)
    If[ListQ[value] && Length[value] === Length[boxes] && Length[sp] > 1,
      Return[DeleteCases[MapIndexed[{First[#2], value[[#1]]} &, sp], {_, Null}]]]
  ];
  If[value === Null, {}, {{1, value}}]
];

suppressedResultQ[v_] := v === Null;

(* Match the document's own output form instead of imposing one.

   A notebook's stored Output cells carry BoxData[FormBox[..., form]], and the
   form is a property of that document -- packages that typeset physics commonly
   set TraditionalForm, and this notebook's own outputs are stored that way.
   Writing results in StandardForm produced values that were correct and
   typeset differently from every other output on the page, which is the wrong
   kind of difference to introduce into a record meant for comparison.

   Read it from the document rather than hardcoding: the most common form among
   the existing Output cells, falling back to StandardForm when there are none
   to learn from. *)
documentOutputForm[nb_] := Module[{forms},
  forms = Cases[nb, c_Cell /; cellStyle[c] === "Output" :>
      FirstCase[c, FormBox[_, f_] :> f, StandardForm, Infinity], Infinity];
  If[forms === {}, StandardForm, First[Commonest[forms]]]
];

outputBoxes[value_, form_] := Module[{b},
  If[form === StandardForm, Return[BoxData[ToBoxes[value, StandardForm]]]];
  (* ToBoxes in a non-Standard form already returns a FormBox, so wrapping the
     result again produces FormBox[FormBox[...]] -- which renders, and is not
     what the document's own cells look like. *)
  b = ToBoxes[value, form];
  BoxData[If[MatchQ[b, FormBox[_, form]], b, FormBox[b, form]]]
];

(* Every Output cell this input owns, not just the next one.

   An input owns the outputs that follow it until the next thing that is not an
   output or a Print. Notebooks accumulate more than one: an input at index 195
   was followed by an Output at 196, several Prints, and a SECOND Output at 201
   left over from an earlier run. Replacing only the first put a freshly
   computed number on the page next to a stale one from the file's edit history,
   with nothing to distinguish them -- precisely the confusion write_outputs
   exists to remove. So replace the first and delete the rest. *)
ownedOutputs[nb_, pos_, i_] := Module[{j = i + 1, found = {}, st},
  While[j + 1 <= Length[pos],
    st = cellStyle[Extract[nb, pos[[j + 1]]]];
    If[st =!= "Output" && st =!= "Print", Break[]];
    If[st === "Output", AppendTo[found, pos[[j + 1]]]];
    j++
  ];
  found
];

(* Mark an output cell with the replay child that produced it.

   TaggingRules on a cell survive NotebookSave and travel with the file, which
   is what makes this evidence rather than a note to ourselves. An output cell
   alone proves only that SOMETHING wrote an output there: a person, an earlier
   replay, or this one. The tag says which.

   Deliberately not the request id or the evaluation token: those are known only
   after the evaluation returns, and writing them would need a second pass over
   the document. The tag carries what is known before submission, and the
   manifest carries child -> request/token, so the join is two hops and neither
   record has to be rewritten. *)
withProvenance[cell_, ""] := cell;
withProvenance[cell_Cell, tag_String] := Module[{args, style, opts, tr},
  args = List @@ cell;
  (* A Cell is Cell[content, style, options...] -- the style is positional and
     an option cannot be put in front of it. Notebook[] has no such slot, which
     is why the notebook-level stamp beside this one looks different and why
     copying its shape here silently produced cells whose style was an option:
     they evaluated, they were written, and every one of them read back as
     "Unknown". *)
  If[Length[args] < 2 || !StringQ[args[[2]]], Return[cell]];
  style = args[[2]];
  tr = FirstCase[Drop[args, 2], (TaggingRules -> v_) :> v, {}];
  opts = DeleteCases[Drop[args, 2], TaggingRules -> _];
  tr = Prepend[DeleteCases[Flatten[{tr}], ("MCPReplayChild" -> _)],
               "MCPReplayChild" -> tag];
  Cell @@ Join[{args[[1]], style, TaggingRules -> tr}, opts]
];
withProvenance[cell_, _] := cell;

outputEdit[nb_, pos_, i_, rawValue_, line_Integer, form_, provenance_String : ""] := Module[
  {here, owned, vals, cells, k, m, edits = {}, anchor},
  vals = resultsForCell[Extract[nb, pos[[i + 1]]], rawValue];
  here = pos[[i + 1]];
  owned = ownedOutputs[nb, pos, i];
  (* Numbered by the STATEMENT that produced it, not by position among the
     survivors: a three-statement cell at In[9] whose second statement is the
     only one returning a value shows Out[10], not Out[9]. *)
  cells = Table[
    withProvenance[
      withLabel[Cell[outputBoxes[vals[[j, 2]], form], "Output"],
                "Out[" <> ToString[line + vals[[j, 1]] - 1] <> "]="],
      provenance],
    {j, Length[vals]}];
  k = Length[cells]; m = Length[owned];
  (* Reuse the output cells already there, then add or remove to match. Anything
     left over is stale: a number from an earlier run that this one did not
     reproduce, and leaving it puts fresh and stale values side by side. *)
  Do[AppendTo[edits, {owned[[j]], cells[[j]], "replaced"}], {j, Min[k, m]}];
  If[k > m,
    anchor = If[m > 0, Last[owned], here];
    AppendTo[edits, {MapAt[# + 1 &, anchor, -1], Take[cells, {m + 1, k}], "inserted"}]];
  If[m > k, Do[AppendTo[edits, {owned[[j]], Null, "deleted"}], {j, k + 1, m}]];
  edits
];

inputLabelEdit[nb_, pos_, i_, line_Integer] :=
  {pos[[i + 1]], withLabel[Extract[nb, pos[[i + 1]]], "In[" <> ToString[line] <> "]:="], "replaced"};

(* Apply deferred edits in reverse document order so that an insertion never
   shifts a position still waiting to be used. *)
(* Two passes, and the split matters. Labelling an input is a replacement at the
   input's own position; writing its output may be an insertion at the position
   immediately after -- which is the NEXT cell's position. So one position can
   carry both an insert and a replace, and if the insert goes first the replace
   lands on the freshly inserted cell instead of the one it was computed for.
   That silently swaps an output for a duplicated input.

   Replacements never move anything, so they are all safe first, in any order.
   Insertions are then applied in reverse document order, so an earlier
   insertion cannot invalidate a later position still waiting to be used. *)
applyOutputEdits[nb_, edits_] := Module[{out = nb, moving},
  Do[out = ReplacePart[out, e[[1]] -> e[[2]]], {e, Cases[edits, {_, _, "replaced"}]}];
  (* Deletions and insertions both move later positions, so they have to be
     applied together in one descending pass -- doing them in separate passes
     would let one invalidate the other's positions. *)
  moving = Reverse[SortBy[Cases[edits, {_, _, "inserted" | "deleted"}], First]];
  Do[out = Which[
      e[[3]] === "deleted", Delete[out, e[[1]]],
      (* several cells at one position: insert in reverse so they land in order *)
      ListQ[e[[2]]], Fold[Insert[#1, #2, e[[1]]] &, out, Reverse[e[[2]]]],
      True, Insert[out, e[[2]], e[[1]]]],
     {e, moving}];
  out
];

(* A content identity for each input cell, so a replay can notice that the
   science under an ordinal has changed.

   The hash is over the cell's source TEXT, recovered structurally from its
   boxes, and over nothing else.

   Not the whole Cell: evaluating a cell writes In[7]:= into its options, so a
   digest over those would change merely because the cell had been run -- which
   is exactly when a reconciling client needs it to have stayed the same.

   Not the boxes themselves either, which was the first attempt and was wrong.
   A cell written from text is stored as BoxData["o1 = 1 + 2"], and saving the
   notebook parses it into BoxData[RowBox[{"o1"," ","=",...}]]. Same source,
   different structure, different hash -- so every child of a saved-and-reopened
   replay reported its science as changed. Measured, not supposed.

   boxText is the project's inert boxes-to-text conversion: it does not go
   through ToExpression, so hashing a notebook cannot re-execute it, and both
   shapes above reduce to the same string. *)
(* --- what a notebook reads and writes ------------------------------------

   A notebook that loads a stored result looks exactly like one that computes
   it: both leave a value in a symbol and both report success. The difference
   is visible only in what the cells DO with the filesystem, and that has to be
   established before running anything, because by the time a `Get` has
   silently returned $Failed the damage is several cells downstream and looks
   like a physics problem.

   Parsed structurally rather than by matching text. A path is as often
   FileNameJoin[{Directory[], "x.wl"}] as a bare string, and a regex that
   handles the one and not the other misses exactly the round-trip pairs that
   matter most.

   Commented cells are included deliberately, and are the whole point: a
   commented computation sitting directly above a live load is what "this
   notebook ships in load mode" looks like. A fully-commented cell parses to
   nothing, so the comment markers are stripped and the inside is parsed. *)

fileOpHeads = Hold[Get, Import, Export, Put, PutAppend, DumpSave, Save,
                   BinaryRead, ReadList, OpenRead, OpenWrite, DeleteFile];

readHeadQ[h_] := MemberQ[{Get, Import, ReadList, BinaryRead, OpenRead}, h];
writeHeadQ[h_] := MemberQ[{Export, Put, PutAppend, DumpSave, Save, OpenWrite,
                           DeleteFile}, h];

(* A literal path where one can be recovered, and an honest description where
   it cannot. Reporting "unresolved" is far better than reporting a guess:
   the caller can look, and a wrong filename silently mis-pairs a read with a
   write. *)
literalPath[s_String] := s;
literalPath[FileNameJoin[parts_List]] :=
  If[AllTrue[parts, StringQ], FileNameJoin[parts],
     "<unresolved: " <> StringTake[ToString[FileNameJoin[parts], InputForm], UpTo[70]] <> ">"];
literalPath[e_] := "<unresolved: " <> StringTake[ToString[e, InputForm], UpTo[70]] <> ">";

strippedSource[text_String] := Module[{t = StringTrim[text]},
  If[StringMatchQ[t, "(*" ~~ ___ ~~ "*)"],
    {StringTrim[StringTake[t, {3, -3}]], True},
    {t, False}]
];

fileOpsIn[text_String] := Module[{src, dead, held, ops},
  {src, dead} = strippedSource[text];
  held = Quiet[Check[ToExpression[src, InputForm, HoldComplete], $Failed]];
  If[held === $Failed || held === Null, Return[{}]];
  ops = Cases[held,
    HoldPattern[h_Symbol[first_, ___]] /; (readHeadQ[h] || writeHeadQ[h]) :>
      <|"head" -> ToString[h],
        "kind" -> If[readHeadQ[h], "read", "write"],
        "path" -> literalPath[Unevaluated[first]],
        "commented" -> dead|>,
    Infinity, Heads -> True];
  DeleteDuplicates[ops]
];

MCPFileDependencies[id_String] :=
  sessionOr[id, Module[{nb, cells, ordinal = 0, found = {}},
    nb = $Sessions[id, "nb"];
    cells = Extract[nb, sessionPos[id]];
    Do[
      If[executableQ[c],
        ordinal++;
        Module[{text = boxText[First[c] /. BoxData[b_] :> b], ops},
          ops = fileOpsIn[text];
          If[ops =!= {},
            found = Join[found,
              Map[Append[#, "ordinal" -> ordinal] &, ops]]]]],
      {c, cells}];
    ok[<|"id" -> id, "operations" -> found,
        "executable_cells" -> ordinal|>]
  ]];

MCPInputDigests[id_String] :=
  sessionOr[id, Module[{nb, pos, ords, out = {}},
    nb = $Sessions[id, "nb"];
    pos = leafPositions[nb];
    ords = inputOrdinals[nb, pos];
    Do[
      If[ords[[q]] > 0,
        AppendTo[out, <|"ordinal" -> ords[[q]], "index" -> q - 1,
                        "digest" -> Hash[boxText[First[Extract[nb, pos[[q]]]] /.
                                                  BoxData[b_] :> b],
                                         "SHA256", "HexString"]|>]],
      {q, Length[pos]}];
    ok[<|"id" -> id, "inputs" -> Length[out], "digests" -> out|>]
  ]];

(* Which replay child wrote each output cell, according to the document itself.

   Read back from the notebook rather than from anything this process
   remembers, because the question a reconciling client is asking is exactly
   whether the document agrees with its own records. *)
MCPOutputProvenance[id_String] :=
  sessionOr[id, Module[{nb, pos, out = {}, c, tr, tag},
    nb = $Sessions[id, "nb"];
    pos = leafPositions[nb];
    Do[
      c = Extract[nb, pos[[q]]];
      If[cellStyle[c] === "Output",
        tr = FirstCase[Rest[List @@ c], (TaggingRules -> v_) :> v, {}];
        tag = FirstCase[Flatten[{tr}], ("MCPReplayChild" -> v_) :> v, ""];
        AppendTo[out, <|"index" -> q - 1, "child" -> tag|>]],
      {q, Length[pos]}];
    ok[<|"id" -> id, "outputs" -> Length[out], "provenance" -> out|>]
  ]];

(* Evaluate one input cell, named by its ORDINAL rather than its position.

   A caller driving the replay itself -- one request per cell, so that each cell
   has its own execution identity -- cannot use raw indices: writing an output
   inserts a cell, so every index after it shifts, and the caller would have to
   re-list the document between cells to stay correct. The input ordinal does not
   move, which is why the label bookkeeping already uses it.

   Everything else is deliberately NOT reimplemented here. This resolves the
   ordinal to a position and hands the work to MCPEvaluateRange with from == to,
   so labelling, write-back, the $Line counter and the doneInputs set stay in one
   place and cannot drift between the two paths. *)
MCPEvaluateInput[id_String, ordinal_Integer, timeout_, writeOutputs : (True | False) : False,
                 abortSentinel_String : "", provenance_String : ""] :=
  sessionOr[id, Module[{pos, ords, hit},
    pos = sessionPos[id];
    ords = sessionOrds[id];
    hit = FirstPosition[ords, ordinal, None, {1}];
    If[hit === None,
      Return[err["No such input ordinal",
                 <|"ordinal" -> ordinal, "inputs" -> Max[ords]|>]]
    ];
    MCPEvaluateRange[id, First[hit] - 1, First[hit] - 1, timeout, True,
                     writeOutputs, abortSentinel, provenance]
  ]];

MCPEvaluateRange[id_String, from_Integer, to_Integer, timeout_, stopOnError : (True | False),
                 writeOutputs : (True | False) : False, abortSentinel_String : "",
                 provenance_String : ""] :=
  sessionOr[id, Module[{nb, pos, results = {}, upper, c, r, edits = {}, e, inserted = 0, line, outForm,
                       stoppedAt = None, ords, done, relabelled = {}, ord,
                       skippedInputs = {}},
    nb = $Sessions[id, "nb"];
    line = Lookup[$Sessions[id], "line", 0];
    done = Lookup[$Sessions[id], "doneInputs", {}];
    outForm = documentOutputForm[nb];
    (* Same document, so the session's cache answers: see setNotebook. *)
    pos = sessionPos[id];
    ords = sessionOrds[id];
    upper = If[to < 0, Length[pos] - 1, Min[to, Length[pos] - 1]];
    (* Did this span start past an executable cell nothing has run?

       Re-locating a boundary by content after indices_shifted is a manual step,
       and landing one cell too late skips a definition. Nothing downstream then
       fails: an undefined function stays unevaluated and Coefficient of an
       unevaluated head is 0, so several hundred cells report success and record
       zeros. That happened -- a skipped PoleExtractor definition silently
       emptied ~700 results, and the only reason it surfaced was a stray label
       the verifier noticed afterwards. A skip is legitimate sometimes (a cell
       run by hand, a cell deliberately avoided), so this informs rather than
       refuses -- but it must never be silent. *)
    skippedInputs = If[done === {}, {},
      Select[Range[Max[done] + 1, Max[0, If[from <= Length[ords] - 1, ords[[from + 1]], 0] - 1]],
        # > Max[done] &]];
    If[from < 0 || from > upper,
      Return[err["Empty or invalid cell range", <|"from" -> from, "to" -> upper, "total" -> Length[pos]|>]]
    ];
    Do[
      c = Extract[nb, pos[[i + 1]]];
      $lastResult = Null;
      r = evalCell[c, $Sessions[id, "dir"], $Sessions[id, "path"], timeout];
      AppendTo[results, Join[<|"index" -> i, "style" -> cellStyle[c]|>, r]];
      If[writeOutputs && TrueQ[r["success"]] && !TrueQ[r["skipped"]],
        (* Re-running a cell this session already labelled abandons the number it
           had: the cell gets a fresh higher one and the old number belongs to
           nothing. The record then has a hole no cell accounts for -- which is
           indistinguishable, to a reader who cannot re-run it, from a missing
           cell. Harmless to the mathematics, corrosive to an audit, and silent
           until now. Overlapping spans are easy to produce by accident when
           re-locating a boundary after indices_shifted. *)
        ord = ords[[i + 1]];
        If[MemberQ[done, ord],
          AppendTo[relabelled, <|"index" -> i,
            "previous_label" -> labelNumber[c], "new_label" -> line + 1|>],
          AppendTo[done, ord]];
        AppendTo[edits, inputLabelEdit[nb, pos, i, line + 1]];
        edits = Join[edits, outputEdit[nb, pos, i, $lastResult, line + 1, outForm, provenance]];
        (* Each result consumed a line number, exactly as the front end does. *)
        (* Every statement consumes a line number, whether or not it printed
           anything: the front end advances In[] per statement, not per output. *)
        line = line + Max[1, statementCount[Extract[nb, pos[[i + 1]]]]]
      ];
      (* A cell that aborts itself costs that cell; a user abort() costs the span.
         Both reach CheckAbort as user-initiated aborts and are indistinguishable
         in the kernel, so the client marks the sentinel file before signalling
         and we read it here. No file (or no path given) means the abort came
         from the cell, and the replay continues as it always has. *)
      (* Ask once, and use the answer for both decisions: userAbortedQ CONSUMES
         the sentinel, so a second call would report False and the record would
         contradict the halt. *)
      If[TrueQ[r["aborted"]],
        If[userAbortedQ[abortSentinel],
          results[[-1, "reason"]] = "the client aborted this cell; the replay stopped here";
          stoppedAt = i; Break[],
          results[[-1, "reason"]] =
            "the cell called Abort[] (or something it invoked did); the replay continued"]];
      If[stopOnError && TrueQ[r["timed_out"]], stoppedAt = i; Break[]],
      {i, from, upper}
    ];
    If[writeOutputs && edits =!= {},
      inserted = Total[Length /@ Cases[edits, {_, c_List, "inserted"} :> c]];
      setNotebook[id, stampReplay[applyOutputEdits[nb, edits]]];
      $Sessions[id, "dirty"] = True;
      $Sessions[id, "line"] = line;
    ];
    If[writeOutputs, $Sessions[id, "doneInputs"] = done];
    ok[Join[<|
      "id" -> id, "from" -> from, "to" -> upper,
      "evaluated" -> Length[results],
      "results" -> results
    |>,
      If[skippedInputs =!= {},
        <|"executable_cells_skipped" -> Length[skippedInputs],
          "skipped_input_numbers" -> Take[skippedInputs, UpTo[12]],
          "warning_skipped" -> "This span began after " <> ToString[Length[skippedInputs]]
            <> " executable cell(s) that no span in this session has run. If that "
            <> "was not deliberate, the definitions they make are missing and later "
            <> "cells will still report success while computing from unevaluated "
            <> "symbols -- which usually looks like zeros, not like an error."|>,
        <||>],
      If[relabelled =!= {},
        <|"cells_re_evaluated" -> Length[relabelled],
          "renumbered" -> Take[relabelled, UpTo[8]],
          "warning" -> "This span re-evaluated " <> ToString[Length[relabelled]]
            <> " cell(s) that an earlier span in this session had already "
            <> "labelled. Each one was given a new, higher In[] number, so the "
            <> "numbers they held before now belong to no cell and appear as "
            <> "gaps in the record. Values are unaffected. Avoid overlapping "
            <> "spans: after indices_shifted, resume at the cell AFTER the last "
            <> "one the previous call reported, not at the boundary you "
            <> "re-located to."|>,
        <||>],
      If[stoppedAt =!= None,
        <|"stopped_at" -> stoppedAt,
          "stopped_early" -> True,
          "cells_not_attempted" -> (upper - stoppedAt),
          "stopped_because" -> If[TrueQ[Last[results]["aborted"]],
            "abort() was called", "a cell timed out"],
          "note" -> If[TrueQ[Last[results]["aborted"]],
            "abort() interrupted cell " <> ToString[stoppedAt] <> ", so the span stopped "
            <> "there and " <> ToString[upper - stoppedAt] <> " later cell(s) were not "
            <> "attempted. The kernel and everything it had already computed are intact; "
            <> "resume with from_ = " <> ToString[stoppedAt + 1] <> ". (A cell that aborts "
            <> "itself does not stop the span -- only an abort you asked for does.)",
            "Cell " <> ToString[stoppedAt] <> " timed out and stop_on_error was set, so "
            <> ToString[upper - stoppedAt] <> " later cell(s) were not attempted."]|>,
        <||>],
      If[writeOutputs,
        <|"outputs_written" -> Total[Length /@ Cases[edits, {_, c_List, "inserted"} :> c]]
              + Count[edits, {_, c_Cell /; cellStyle[c] === "Output", "replaced"}],
          "stale_outputs_removed" -> Count[edits, {_, _, "deleted"}],
          "cells_inserted" -> inserted,
          "net_cell_change" -> inserted - Count[edits, {_, _, "deleted"}],
          "output_form" -> ToString[outForm],
          "last_line" -> line,
          "indices_shifted" -> (Count[edits, {_, _, "inserted" | "deleted"}] > 0),
          "cell_count" -> Length[leafPositions[$Sessions[id, "nb"]]],
          "dirty" -> True|>,
        <||>]]
    ]
  ]];


(* Which cell assigned this symbol?

   A replay leaves symbols in the kernel, and the kernel can show you their
   VALUES -- but not where they came from. That is a search over the document's
   boxes, not a kernel query, and no amount of asking the kernel will answer it:
   by the time a value exists, the cell that made it is long gone.

   Matching is textual on the cell's own boxes, deliberately. Evaluating the
   cells to find out what they assign would run the notebook, which is the thing
   the caller is trying to understand, and reading DownValues finds definitions
   the session accumulated from anywhere, not the ones this document states.

   Handles a plain symbol ("Result"), an indexed assignment ("Amp[2]"), and both
   = and := . It will not see an assignment built at runtime (Set @@ ..., a name
   assembled with Symbol[...]) -- those are invisible to any textual search, and
   the reply says so rather than implying the list is exhaustive. *)

reEscape[s_String] := StringReplace[s,
  c : ("\\" | "." | "^" | "$" | "|" | "(" | ")" | "[" | "]" | "{" | "}" | "*" | "+" | "?") :> "\\" <> c];

definingCells[nb_, symbol_String] := Module[{pos, out = {}},
  pos = leafPositions[nb];
  Do[
    With[{c = Extract[nb, pos[[i + 1]]]},
      If[MemberQ[{"Input", "Code"}, cellStyle[c]],
        Module[{txt, pat},
          txt = Quiet[Check[boxText[First[c] /. BoxData[b_] :> b], ""]];
          If[!StringQ[txt], txt = ""];
          pat = RegularExpression[
            "(?<![A-Za-z0-9$`])" <> reEscape[symbol] <> "\\s*(\\[[^\\]]*\\])?\\s*(:?=)(?!=)"];
          If[StringContainsQ[txt, pat],
            AppendTo[out, <|
              "index" -> i,
              "style" -> cellStyle[c],
              "delayed" -> (StringCases[txt, pat -> "$2", 1] === {":="}),
              "preview" -> truncate[StringTake[txt, UpTo[200]]]|>]]]]],
    {i, 0, Length[pos] - 1}];
  out
];

MCPFindDefining[id_String, symbol_String] :=
  sessionOr[id, Module[{hits},
    hits = definingCells[$Sessions[id, "nb"], symbol];
    ok[<|"id" -> id, "symbol" -> symbol,
        "count" -> Length[hits],
        "cells" -> hits,
        "note" -> If[hits === {},
          "No cell in this document textually assigns " <> symbol <> ". It may be "
          <> "defined by a package, by a cell that builds the name at runtime, or "
          <> "in the kernel from an earlier session rather than by this notebook.",
          "Textual search of the document's own cells; an assignment constructed at "
          <> "runtime would not appear here."]|>]
  ]];

(* ------------------------------------------------------------------------ *)
(* Mutation and persistence                                                  *)
(* ------------------------------------------------------------------------ *)

MCPWriteCell[id_String, content_String, style_String, position_String, anchor_Integer] :=
  MCPWriteCell[id, content, style, position, anchor, ""];

MCPWriteCell[id_String, content_String, style_String, position_String, anchor_Integer, recordTag_String] :=
  MCPWriteCell[id, content, style, position, anchor, recordTag, ""];

(* The recorder stamps Evaluatable on every cell it writes (True for Input/Code,
   False for narrative) so replay never depends on stylesheet defaults, and so
   read-back can check the flag exactly instead of inferring it from style. *)
MCPWriteCell[id_String, content_String, style_String, position_String, anchor_Integer, recordTag_String, evaluatable_String] :=
  sessionOr[id, Module[{nb, pos, newCell, cells, at, updated, opts},
    nb = $Sessions[id, "nb"];
    pos = leafPositions[nb];
    opts = Join[
      If[recordTag === "", {}, {TaggingRules -> {"MCPRecordTag" -> recordTag}}],
      Switch[evaluatable, "True", {Evaluatable -> True}, "False", {Evaluatable -> False}, _, {}]
    ];
    newCell = Cell[BoxData[content], style, Sequence @@ opts];
    (* Insertion only rewrites the TOP-LEVEL cell list. Splicing into a nested
       CellGroupData would need the group's own position and is deliberately
       not attempted: silently putting a cell in the wrong group is worse than
       appending it where the caller can see it. *)
    cells = First[nb];
    If[!ListQ[cells], cells = {cells}];
    at = Switch[position,
      "Beginning", 0,
      "End", Length[cells],
      "Before", Max[0, Min[anchor, Length[cells]]],
      "After", Max[0, Min[anchor + 1, Length[cells]]],
      _, Length[cells]
    ];
    updated = Insert[cells, newCell, at + 1];
    setNotebook[id, ReplacePart[nb, 1 -> updated]];
    $Sessions[id, "dirty"] = True;
    ok[<|"id" -> id, "inserted_at" -> at,
        "record_tag" -> recordTag,
        "cell_count" -> Length[leafPositions[$Sessions[id, "nb"]]]|>]
  ]];

(* Replacing a cell's CONTENT, leaving its position, style and options alone.

   Insertion cannot reach a nested cell -- MCPWriteCell rewrites only the
   top-level list, because splicing into a CellGroupData needs the group's own
   position -- and in a sectioned notebook almost every cell is nested. That
   left no way to change an existing cell at all: a notebook had to be edited
   as text outside the server, which is exactly the round trip this layer
   exists to avoid.

   Addressing is by the same leaf index `delete` uses, so a cell that can be
   deleted can be replaced. The style and every option are carried across from
   the cell being replaced rather than re-specified, so an edit cannot silently
   restyle a cell or drop its CellLabel. *)
MCPReplaceCell[id_String, index_Integer, content_String] :=
  sessionOr[id, Module[{nb, pos, target, style, options, replacement},
    nb = $Sessions[id, "nb"];
    pos = leafPositions[nb];
    If[index < 0 || index >= Length[pos],
      Return[err["Cell index out of range", <|"index" -> index, "total" -> Length[pos]|>]]
    ];
    target = Extract[nb, pos[[index + 1]]];
    If[Head[target] =!= Cell,
      Return[err["Not a cell", <|"index" -> index, "head" -> ToString[Head[target]]|>]]
    ];
    style = If[Length[target] >= 2, target[[2]], "Input"];
    options = If[Length[target] >= 3, Drop[List @@ target, 2], {}];
    replacement = Cell[BoxData[content], style, Sequence @@ options];
    setNotebook[id, ReplacePart[nb, pos[[index + 1]] -> replacement]];
    $Sessions[id, "dirty"] = True;
    ok[<|"id" -> id, "replaced" -> index, "style" -> ToString[style],
        "options_kept" -> Length[options],
        "cell_count" -> Length[leafPositions[$Sessions[id, "nb"]]]|>]
  ]];

MCPDeleteCell[id_String, index_Integer] :=
  sessionOr[id, Module[{nb, pos},
    nb = $Sessions[id, "nb"];
    pos = leafPositions[nb];
    If[index < 0 || index >= Length[pos],
      Return[err["Cell index out of range", <|"index" -> index, "total" -> Length[pos]|>]]
    ];
    setNotebook[id, Delete[nb, pos[[index + 1]]]];
    $Sessions[id, "dirty"] = True;
    ok[<|"id" -> id, "deleted" -> index, "cell_count" -> Length[leafPositions[$Sessions[id, "nb"]]]|>]
  ]];

(* A notebook FILE is not a Put of a Notebook[...] expression.

   Put writes an InputForm expression dump. A real .nb additionally carries the
   "Content-type: application/vnd.wolfram.mathematica" header, a CacheID, and
   the NotebookDataPosition/NotebookDataLength block the front end uses to
   locate the content. A front end asked to open a bare dump falls back to a
   much less exercised path, and on a large document that path is where front
   ends have been observed to die.

   So: save through the front end, which produces a genuine notebook file and
   stamps the version actually writing it. Put stays only as the fallback for
   when no front end is available, because a dump does still round-trip through
   Get -- it is readable by the kernel, just not a proper document. *)
saveNotebookFile[nb_, target_] := Module[{res},
  res = Quiet[Check[
    UsingFrontEnd[Module[{nbo, r},
      (* Join needs matching heads: nb is Notebook[cells, opts...], so the
         extra option has to arrive as a Notebook too, not a List. *)
      nbo = NotebookPut[Join[nb, Notebook[{}, Visible -> False]]];
      r = NotebookSave[nbo, target];
      NotebookClose[nbo];
      r
    ]],
    $Failed]];
  (* Check the file, not the return value: NotebookSave returns Null on success,
     and the thing that matters is whether a real header landed on disk.
     ReadList line-by-line, because ReadString's second argument is a
     terminator, not a character count. *)
  If[res =!= $Failed && FileExistsQ[target] &&
       StringContainsQ[
         Quiet[Check[Module[{st, ln}, st = OpenRead[target];
             ln = ReadList[st, "String", 3]; Close[st]; StringRiffle[ln, " "]], ""]],
         "Content-type"],
    "frontend",
    (* fall back rather than fail: a dump is worse than a document, not useless *)
    If[Quiet[Check[Put[nb, target]; True, False]], "put", $Failed]]];

MCPSave[id_String, path_String] :=
  sessionOr[id, Module[{target, how},
    target = If[path === "", $Sessions[id, "path"], ExpandFileName[path]];
    If[target === "" || target === None,
      Return[err["No path to save to; pass one explicitly"]]
    ];
    Quiet[If[!DirectoryQ[DirectoryName[target]], CreateDirectory[DirectoryName[target]]]];
    how = saveNotebookFile[$Sessions[id, "nb"], target];
    If[how === $Failed, Return[err["Failed to write notebook", <|"path" -> target|>]]];
    $Sessions[id, "path"] = target;
    $Sessions[id, "dirty"] = False;
    ok[<|"id" -> id, "path" -> target, "saved" -> True, "written_by" -> how,
        "notebook_file" -> (how === "frontend"),
        "note" -> If[how === "frontend", "",
          "No front end was available, so this is a bare expression dump rather "
          <> "than a notebook file. The kernel can read it back; a front end may "
          <> "refuse or struggle to open it."]|>]
  ]];

(* ---------------------------------------------------------------------------
   Front-end rendering.

   The front end is used here as a RENDERING service and nothing else:
   typesetting, rasterisation, and export. It is deliberately never asked to
   evaluate anything.

   The reason is measured. When an external WSTP client owns the kernel's main
   link, a cell dispatched to the front end with SelectionEvaluate never gets
   an evaluator: a job taking 5.1s in the kernel had not completed after 200s,
   and had not started after 40s of total link silence. Trivial expressions
   appear to work because they are serviced inside the UsingFrontEnd block
   itself, which makes the failure look intermittent rather than total.

   Evaluation goes over the kernel link (where abort and liveness both work).
   Rendering comes here. Do not blur the two.

   UsingFrontEnd starts a front end on demand -- ~1.8s cold, -platform
   offscreen, no display required -- and it is torn down with the kernel.
--------------------------------------------------------------------------- *)

MCPFrontEndAvailable[] := json[<|
  "success" -> True,
  "available" -> TrueQ[Quiet[Check[UsingFrontEnd[Head[$FrontEnd] === FrontEndObject], False]]]
|>];

(* These return raw PNG/PDF bytes on success and a JSON error object otherwise.
   The two are never ambiguous: PNG begins with a 0x89 byte, PDF with "%PDF",
   and JSON with "{". *)
MCPRenderCell[id_String, index_Integer, dpi_] :=
  If[!KeyExistsQ[$Sessions, id],
    err["No such headless notebook session: " <> id],
    Module[{nb, pos, c},
      nb = $Sessions[id, "nb"];
      pos = leafPositions[nb];
      If[index < 0 || index >= Length[pos],
        Return[err["Cell index out of range", <|"index" -> index, "total" -> Length[pos]|>]]
      ];
      c = Extract[nb, pos[[index + 1]]];
      Quiet[Check[
        UsingFrontEnd[ExportByteArray[Rasterize[c, "Image", ImageResolution -> dpi], "PNG"]],
        err["Front end could not rasterise this cell"]
      ]]
    ]
  ];

MCPRenderExpression[code_String, dpi_] := Module[{expr},
  expr = Quiet[Check[ToExpression[code, InputForm, Hold], $Failed]];
  If[expr === $Failed, Return[err["Could not parse the expression", <|"code" -> code|>]]];
  Quiet[Check[
    UsingFrontEnd[ExportByteArray[
      Rasterize[ReleaseHold[expr], "Image", ImageResolution -> dpi], "PNG"]],
    err["Front end could not rasterise this expression"]
  ]]
];

(* openGroups: export with every CellGroupData forced Open. Export reflects what
   is VISIBLE, exactly as printing from the GUI would -- a collapsed section
   exports collapsed. That is correct behaviour, but it surprises callers who
   expect a whole-document render, so it is offered as a switch. *)
(* Markdown, generated here rather than by Export[..., "Markdown"].

   The built-in exporter rasterises every Output cell to a PNG in a sibling img/
   directory and takes no options to stop it, so the results -- the only part
   worth reading -- end up outside the document as images that cannot be
   searched, diffed or reviewed. It also emits its own unevaluated internals
   when no front end is attached. Emitting the text ourselves keeps a Markdown
   file that is a single self-contained artifact.

   texMath renders outputs as $$...$$ via TeXForm, which most Markdown viewers
   typeset; otherwise outputs are InputForm inside a fence, which is what you
   want if you intend to diff them or paste them back into a kernel. *)
mdHeading[style_String] := Switch[style,
  "Title", "# ", "Chapter", "## ", "Section", "### ",
  "Subsection", "#### ", "Subsubsection", "##### ", _, Nothing];

(* Boxes to text, structurally. NOT via ToExpression: its HoldComplete argument
   wraps the RESULT of evaluation rather than preventing it, so parsing cells
   that way would silently re-execute the whole notebook during an export.
   Everything here is inert pattern matching.

   StringJoin over the leaf strings is not enough either -- it renders a
   subscripted gamma and a squared Gamma as "gm" and "G2", losing exactly the
   structure a reader needs. *)
boxText[str_String] := str;
boxText[RowBox[l_List]] := StringJoin[boxText /@ l];
boxText[SubscriptBox[a_, b_]] := "Subscript[" <> boxText[a] <> ", " <> boxText[b] <> "]";
boxText[SuperscriptBox[a_, b_]] := boxText[a] <> "^(" <> boxText[b] <> ")";
boxText[SubsuperscriptBox[a_, b_, c_]] :=
  "Subscript[" <> boxText[a] <> ", " <> boxText[b] <> "]^(" <> boxText[c] <> ")";
boxText[FractionBox[a_, b_]] := "((" <> boxText[a] <> ")/(" <> boxText[b] <> "))";
boxText[SqrtBox[a_]] := "Sqrt[" <> boxText[a] <> "]";
boxText[RadicalBox[a_, n_]] := "Surd[" <> boxText[a] <> ", " <> boxText[n] <> "]";
boxText[(StyleBox | TagBox | FormBox | InterpretationBox | PaneBox | AdjustmentBox)[a_, ___]] := boxText[a];
boxText[GridBox[rows_List, ___]] := StringRiffle[Map[StringRiffle[boxText /@ #, "\t"] &, rows], "\n"];
boxText[l_List] := StringJoin[boxText /@ l];
boxText[x_] := ToString[x, InputForm];

mdCellText[c_] := Module[{d = First[c]},
  Which[
    StringQ[d], d,
    MatchQ[d, _TextData], StringJoin[Cases[d, _String, Infinity]],
    MatchQ[d, _BoxData], Quiet[Check[boxText[First[d]], ""]],
    True, ToString[d, InputForm]]
];

(* A picture has no text form. Dumping its boxes produces kilobytes of
   GraphicsBox internals that describe the drawing instructions and tell a
   reader nothing -- on a notebook of diagrams that IS the whole file. Say what
   it is and where to see it instead. *)
graphicsBoxQ[c_] := !FreeQ[First[c], GraphicsBox | Graphics3DBox | RasterBox | GraphicsGridBox];

mdGraphicsNote[c_] := Module[{sz},
  sz = Quiet[Check[FirstCase[First[c], (ImageSize -> v_) :> v, Automatic, Infinity], Automatic]];
  "> *[graphics output" <> If[sz === Automatic, "", ", ImageSize " <> ToString[sz]] <>
  " — not representable as text; export to PDF to see it]*"
];

mdOutput[c_, texMath : (True | False)] := Module[{tex},
  If[graphicsBoxQ[c], Return[mdGraphicsNote[c]]];
  If[!texMath, Return["```\n" <> mdCellText[c] <> "\n```"]];
  (* RawBoxes is inert, so TeXForm typesets the STORED boxes without evaluating
     anything the cell contains. *)
  tex = Quiet[Check[ToString[TeXForm[RawBoxes[First[First[c]]]]], $Failed]];
  If[StringQ[tex] && StringTrim[tex] =!= "",
    "$$\n" <> tex <> "\n$$",
    "```\n" <> mdCellText[c] <> "\n```"]
];

(* Compare a replayed document against the notebook it came from.

   Every defect in this write-back was found by a person opening the PDF and
   recognising that it did not look like a notebook: results collapsed into a
   list where there should have been three, an output cell left over from an
   earlier run, values typeset in the wrong form. An agent has no such instinct,
   and telling it to "look at a few pages" does not give it one -- three
   consecutive runs verified page counts and success flags and missed all of it.

   But the reference is right there on disk. The original's own stored cells say
   how many outputs each input had, what form they were typeset in, and how the
   labels ran. Comparing against that is mechanical, so it belongs here rather
   than in a human's evening. This checks SHAPE, never values: the point is to
   catch a record that is malformed, not to decide whether the physics changed. *)
(* Mark a document this server numbered, so the mark outlives the session.

   Whether In[] gaps are faults depends on how the document was produced, and
   "did the current session write it" stops being answerable the moment the
   file is reopened -- which is precisely when an artefact gets audited. A
   TaggingRules entry survives NotebookSave and travels with the file, so a
   record handed over days ago can still be checked against the standard it was
   written to. A notebook without the mark is somebody's own work and is never
   judged by it. *)
$replayStamp = "MCPLinearReplay";

stampReplay[nb_] := Module[{args, tr, rest},
  If[Head[nb] =!= Notebook || Length[nb] < 1, Return[nb]];
  args = List @@ nb;
  tr = FirstCase[Rest[args], (TaggingRules -> v_) :> v, None];
  rest = DeleteCases[Rest[args], TaggingRules -> _];
  tr = If[MatchQ[tr, {___Rule}],
    Prepend[DeleteCases[tr, ($replayStamp -> _)], $replayStamp -> True],
    {$replayStamp -> True}];
  Notebook @@ Join[{First[args], TaggingRules -> tr}, rest]
];

stampedQ[nb_] := Head[nb] === Notebook &&
  MemberQ[FirstCase[Rest[List @@ nb], (TaggingRules -> v_) :> v, {}],
          $replayStamp -> True];

(* Are the document's own cell labels self-consistent?

   This needs no reference, which is the point: a notebook produced from
   scratch has nothing to compare against, and that is exactly the case where
   the record IS the evidence -- a reader who cannot run it judges the work by
   what the page says. Numbers that drift make correct arithmetic look wrong.

   Three things must hold in anything this server writes:
     - consecutive inputs differ by the first one's statement count, because
       In[] advances per statement (pitfall 12);
     - every Out[] falls inside its own input's range of statement numbers;
     - no number is used twice.

   A notebook written by a person satisfies none of these reliably: they
   re-ran cells, evaluated scratch work in between, and closed the front end,
   which deregisters the labels entirely. So this is a check on OUR output, not
   a judgement of theirs -- for a reference document the same figures are
   reported as context, never as faults. *)

labelNumber[c_] := Module[{s, d},
  s = Lookup[Association[Rest[Rest[List @@ c]]], CellLabel, None];
  If[!StringQ[s], Return[None]];
  d = StringCases[s, DigitCharacter ..];
  If[d === {}, None, ToExpression[First[d]]]
];

labelAudit[nb_] := Module[
  {flat, j, k, n, stmts, m, gaps = {}, stray = {}, dup = {}, seen = <||>,
   prev = None, prevStmts = 0, labelled = 0, unlabelled = 0,
   expected = None, notOurs = {}},
  flat = Cases[nb, c_Cell /; ! MatchQ[c[[1]], _CellGroupData] :> c, Infinity];
  Do[
    If[MemberQ[{"Input", "Code"}, cellStyle[flat[[j]]]],
      n = labelNumber[flat[[j]]];
      stmts = statementCount[flat[[j]]];
      If[n === None,
        unlabelled++,
        labelled++;
        If[KeyExistsQ[seen, n],
          AppendTo[dup, <|"label" -> n, "also_at_input" -> seen[n]|>],
          seen[n] = j];
        k = j + 1;
        While[k <= Length[flat] && MemberQ[{"Output", "Print"}, cellStyle[flat[[k]]]],
          If[cellStyle[flat[[k]]] === "Output",
            m = labelNumber[flat[[k]]];
            If[m =!= None && ! (n <= m <= n + stmts - 1),
              AppendTo[stray, <|"input_label" -> n, "statements" -> stmts,
                "output_label" -> m,
                "expected_between" -> {n, n + stmts - 1}|>]]];
          k++];
        (* Follow a running expectation rather than comparing neighbours.

           A cell this replay did not label -- one run by hand, one deliberately
           left alone -- keeps whatever number the file already had and consumes
           no line numbers. Comparing each pair of neighbours counts such a cell
           twice, once going out of sequence and once coming back, and reports
           two faults where nothing is wrong. Advancing the expectation only for
           cells that are in sequence identifies the untouched cells exactly:
           on a clean replay of a document with two mandated deviations, 272 of
           276 inputs followed the sequence and the 4 flagged reduce to exactly
           those 2 cells. *)
        If[expected === None, expected = n];
        If[n === expected,
          expected = n + stmts,
          AppendTo[notOurs, <|"input_number" -> labelled, "label" -> n,
            "expected_here" -> expected|>]];
        prev = n; prevStmts = stmts]],
    {j, Length[flat]}];
  <|"labelled_inputs" -> labelled,
    "unlabelled_inputs" -> unlabelled,
    "in_sequence" -> labelled - Length[notOurs],
    "not_renumbered" -> notOurs,
    (* Which out-of-sequence labels are faults?

       A label this replay did not write is stale -- left by whoever ran the
       document before -- and sits ABOVE the running counter, because their
       session had gone further. A label BELOW the counter is different: it
       re-enters numbers already issued, which is how duplicates and backward
       jumps appear, and no untouched cell produces it.

       A wrong counting rule puts nearly every cell out of sequence, so a high
       proportion is a fault however the labels fall. Two mandated deviations in
       a 276-input document are not. *)
    "numbering_gaps" -> Select[notOurs,
      #["label"] < #["expected_here"] ||
        Length[notOurs] > Ceiling[0.2 * Max[1, labelled]] &],
    "outputs_outside_their_input" -> stray,
    "duplicate_labels" -> dup,
    "clean" -> (gaps === {} && stray === {} && dup === {})|>
];

MCPVerifySelf[id_String] :=
  sessionOr[id, Module[{audit, issues = {}, ours},
    (* Whose labels are these? A session that has replayed with write_outputs
       has a line counter; one that merely opened a file has not. It matters:
       gaps and reused numbers are FAULTS in a record this server wrote and
       NORMAL in one a person evaluated interactively, where cells get re-run and
       scratch work happens in between. Calling the author's own notebook
       inconsistent would be a false accusation, and a reader who is told their
       good document is broken stops believing the next report. *)
    ours = TrueQ[Lookup[$Sessions[id], "line", 0] > 0] ||
             stampedQ[$Sessions[id, "nb"]];
    audit = labelAudit[$Sessions[id, "nb"]];
    If[audit["numbering_gaps"] =!= {},
      AppendTo[issues, <|"kind" -> "In[] numbering does not follow the statement counts",
        "count" -> Length[audit["numbering_gaps"]],
        "examples" -> Take[audit["numbering_gaps"], UpTo[8]]|>]];
    If[audit["outputs_outside_their_input"] =!= {},
      AppendTo[issues, <|"kind" -> "an Out[] is numbered outside its input's statements",
        "count" -> Length[audit["outputs_outside_their_input"]],
        "examples" -> Take[audit["outputs_outside_their_input"], UpTo[8]]|>]];
    If[audit["duplicate_labels"] =!= {},
      AppendTo[issues, <|"kind" -> "the same line number is used by two inputs",
        "count" -> Length[audit["duplicate_labels"]],
        "examples" -> Take[audit["duplicate_labels"], UpTo[8]]|>]];
    ok[<|"id" -> id,
      "checked" -> "cell label self-consistency",
      "labelled_inputs" -> audit["labelled_inputs"],
      "unlabelled_inputs" -> audit["unlabelled_inputs"],
      "labels_written_by_this_session" -> ours,
      "discrepancies" -> If[ours, issues, {}],
      "discrepancy_count" -> If[ours, Length[issues], 0],
      "observations" -> If[ours, {}, issues],
      "verdict" -> Which[
        issues === {},
          "the record's own cell labels are consistent: In[] advances by each "
            <> "cell's statement count and every Out[] sits inside its input's range",
        ours,
          "LABELS ARE INCONSISTENT -- this is a record this server numbered, and it "
            <> "misnumbers itself; do not rely on it as evidence until resolved",
        True,
          "These labels were not written by this server -- the document is as its "
            <> "author left it, and In[] gaps or a reused number are normal in a "
            <> "notebook evaluated interactively, not faults. Listed under "
            <> "observations, not discrepancies. To audit a record THIS server "
            <> "produced, replay with write_outputs=True and check again."],
      "note" -> "Checks the document against itself, so it needs no reference. It "
        <> "cannot detect a shift of the whole sequence: closing a front end or "
        <> "quitting a kernel deregisters labels, so absolute numbers are only "
        <> "meaningful within one session."|>]
  ]];

MCPVerifyAgainst[id_String, refPath_String] :=
  sessionOr[id, Module[
    {ref, mine, shape, a, b, issues = {}, benign = {}, n, i, formRef, formMine,
     commentedQ, inputsOf, refIns, mineLabels, refNums, mineIns, mineNums,
     gapsCompared, gapsMatched, gi, outLeaves, refLeaves, mineLeaves,
     leafPairs, emptied, vi},
    If[!FileExistsQ[refPath], Return[err["Reference notebook not found", <|"path" -> refPath|>]]];
    ref = Get[refPath];
    mine = $Sessions[id, "nb"];
    (* per input cell: how many Output cells follow it before the next input *)
    shape[nb_] := Module[{f, out = {}, j, k, cnt},
      f = Cases[nb, c_Cell /; ! MatchQ[c[[1]], _CellGroupData] :> c, Infinity];
      Do[
        If[MemberQ[{"Input", "Code"}, cellStyle[f[[j]]]],
          cnt = 0; k = j + 1;
          While[k <= Length[f] && MemberQ[{"Output", "Print"}, cellStyle[f[[k]]]],
            If[cellStyle[f[[k]]] === "Output", cnt++]; k++];
          AppendTo[out, cnt]],
        {j, Length[f]}];
      out];
    a = shape[ref]; b = shape[mine];
    If[Length[a] =!= Length[b],
      AppendTo[issues, <|"kind" -> "input cell count differs",
        "reference" -> Length[a], "replay" -> Length[b]|>]];
    n = Min[Length[a], Length[b]];
    (* A cell that is entirely commented out yields nothing, so an output stored
       against it is left over from when it was live. Removing that is right, and
       calling it a discrepancy on the same footing as a real mismatch would
       train a reader to ignore the report -- a clean replay of this notebook
       produces five of them. Separate the benign case explicitly. *)
    commentedQ[c_] := MatchQ[First[c] /. BoxData[x_] :> x,
        RowBox[{"(*", ___, "*)"}] | {RowBox[{"(*", ___, "*)"}] ..}];
    inputsOf[nb_] := Cases[nb, c_Cell /; ! MatchQ[c[[1]], _CellGroupData] &&
        MemberQ[{"Input", "Code"}, cellStyle[c]] :> c, Infinity];
    refIns = inputsOf[ref];
    Do[If[a[[i]] =!= b[[i]] && a[[i]] > 0,
       If[b[[i]] === 0 && i <= Length[refIns] && commentedQ[refIns[[i]]],
         AppendTo[benign, <|"kind" -> "stale output dropped for a commented-out cell",
           "input_number" -> i, "reference_outputs" -> a[[i]]|>],
         AppendTo[issues, <|"kind" -> "output count differs", "input_number" -> i,
           "reference_outputs" -> a[[i]], "replay_outputs" -> b[[i]]|>]]], {i, n}];
    (* Shape says the right number of outputs sit in the right places; it says
       nothing about the numbers ON them. The label bug that prompted this was
       invisible to every shape check: correct count, correct position, wrong
       number. Audit the replay against itself -- the reference's own labels are
       a record of someone's interactive session and prove nothing either way. *)
    mineLabels = labelAudit[mine];
    (* Only when this session wrote them -- see MCPVerifySelf. *)
    If[TrueQ[Lookup[$Sessions[id], "line", 0] > 0] || stampedQ[mine],
    If[mineLabels["numbering_gaps"] =!= {},
      AppendTo[issues, <|"kind" -> "In[] numbering does not follow the statement counts",
        "count" -> Length[mineLabels["numbering_gaps"]],
        "examples" -> Take[mineLabels["numbering_gaps"], UpTo[5]]|>]];
    If[mineLabels["outputs_outside_their_input"] =!= {},
      AppendTo[issues, <|"kind" -> "an Out[] is numbered outside its input's statements",
        "count" -> Length[mineLabels["outputs_outside_their_input"]],
        "examples" -> Take[mineLabels["outputs_outside_their_input"], UpTo[5]]|>]];
    If[mineLabels["duplicate_labels"] =!= {},
      AppendTo[issues, <|"kind" -> "the same line number is used by two inputs",
        "count" -> Length[mineLabels["duplicate_labels"]],
        "examples" -> Take[mineLabels["duplicate_labels"], UpTo[5]]|>]];
    ];
    (* Increments against the reference's own labels.

       Self-consistency cannot catch a wrong counting RULE: the writer and the
       checker both call statementCount, so when that is wrong they agree with
       each other and the record passes while being uniformly misnumbered. The
       reference's labels are independent ground truth -- a real front end wrote
       them -- so comparing gap-for-gap tests the rule rather than the arithmetic.

       Compared as a rate, not pair-by-pair: the author evaluated other things in
       between, which shows up as gaps wider than any rule predicts. Measured on
       a real document, the correct rule agrees on 248 of 256 consecutive pairs
       and counting cells instead of statements agrees on 57 -- so a rate this
       low is a broken rule, not an author's working habits. *)
    refNums = labelNumber /@ refIns; mineIns = inputsOf[mine];
    mineNums = labelNumber /@ mineIns;
    gapsCompared = 0; gapsMatched = 0;
    Do[
      If[refNums[[gi]] =!= None && refNums[[gi + 1]] =!= None &&
         refNums[[gi + 1]] > refNums[[gi]] &&
         mineNums[[gi]] =!= None && mineNums[[gi + 1]] =!= None,
        gapsCompared++;
        If[(mineNums[[gi + 1]] - mineNums[[gi]]) === (refNums[[gi + 1]] - refNums[[gi]]),
          gapsMatched++]],
      {gi, Min[Length[refNums], Length[mineNums]] - 1}];
    If[gapsCompared >= 20 && gapsMatched < Floor[0.8 * gapsCompared],
      AppendTo[issues, <|"kind" ->
        "line numbers advance differently from the reference: the counting rule is wrong",
        "pairs_compared" -> gapsCompared, "pairs_matching" -> gapsMatched,
        "hint" -> "In[] advances once per statement; a cell's stored boxes "
          <> "interleave statements with the newlines between them, so the box "
          <> "count is not the statement count (pitfall 12)."|>]];
    (* Shape says an output is present; it says nothing about what is in it.

       The failure this catches: a definition that never ran leaves a symbol
       unevaluated, Coefficient of an unevaluated head is 0, and every dependent
       cell reports success while recording a zero. Counts match, positions
       match, form matches -- and the record is empty. One real replay put ~700
       such zeros on the page and the only reason it surfaced was an unrelated
       stray label.

       LeafCount, not the expression: comparing values would demand they be
       equal, and a legitimate rerun can differ in ordering or dummy names. A
       result that was hundreds of leaves and is now one has not been reordered,
       it has been emptied. *)
    outLeaves[nb_] := Module[{f, res = {}, j, k, acc},
      f = Cases[nb, c_Cell /; ! MatchQ[c[[1]], _CellGroupData] :> c, Infinity];
      Do[If[MemberQ[{"Input", "Code"}, cellStyle[f[[j]]]],
         acc = {}; k = j + 1;
         While[k <= Length[f] && MemberQ[{"Output", "Print"}, cellStyle[f[[k]]]],
           If[cellStyle[f[[k]]] === "Output", AppendTo[acc, LeafCount[First[f[[k]]]]]]; k++];
         AppendTo[res, acc]], {j, Length[f]}];
      res];
    refLeaves = outLeaves[ref]; mineLeaves = outLeaves[mine];
    leafPairs = Flatten[Table[
      If[Length[refLeaves[[vi]]] === Length[mineLeaves[[vi]]],
        Transpose[{ConstantArray[vi, Length[refLeaves[[vi]]]],
                   refLeaves[[vi]], mineLeaves[[vi]]}], {}],
      {vi, Min[Length[refLeaves], Length[mineLeaves]]}], 1];
    emptied = Select[leafPairs, #[[2]] > 12 && #[[3]] <= 3 &];
    If[emptied =!= {},
      AppendTo[issues, <|"kind" ->
        "an output the reference computed is now essentially empty",
        "count" -> Length[emptied],
        "examples" -> (<|"input_number" -> #[[1]], "reference_leaves" -> #[[2]],
                         "replay_leaves" -> #[[3]]|> & /@ Take[emptied, UpTo[8]]),
        "hint" -> "This is what a skipped definition looks like: the symbol stays "
          <> "unevaluated, arithmetic on it collapses to 0, and every dependent "
          <> "cell still reports success. Check whether a span began past a cell "
          <> "that defines something (pitfall 16)."|>]];
    formRef = documentOutputForm[ref]; formMine = documentOutputForm[mine];
    If[formRef =!= formMine,
      AppendTo[issues, <|"kind" -> "output form differs",
        "reference" -> ToString[formRef], "replay" -> ToString[formMine]|>]];
    ok[<|"id" -> id, "reference" -> refPath,
      "inputs_compared" -> n,
      "reference_output_form" -> ToString[formRef],
      "replay_output_form" -> ToString[formMine],
      "discrepancies" -> Take[issues, UpTo[40]],
      "discrepancy_count" -> Length[issues],
      "expected_differences" -> Take[benign, UpTo[20]],
      "expected_difference_count" -> Length[benign],
      "labels" -> <|"labelled_inputs" -> mineLabels["labelled_inputs"],
                    "unlabelled_inputs" -> mineLabels["unlabelled_inputs"],
                    "self_consistent" -> mineLabels["clean"],
                    "increments_vs_reference" ->
                      <|"compared" -> gapsCompared, "matching" -> gapsMatched|>|>,
      "values" -> <|"outputs_compared" -> Length[leafPairs],
                    "same_size" -> Count[leafPairs, {_, x_, y_} /; x === y],
                    "emptied" -> Length[emptied],
                    "note" -> "Compared by LeafCount, which catches a result "
                      <> "replaced by nothing without demanding two runs agree "
                      <> "exactly."|>,
      "verdict" -> Which[
        issues =!= {},
          "SHAPE DIFFERS from the reference -- inspect before trusting the record",
        benign =!= {},
          "matches the reference, apart from " <> ToString[Length[benign]] <>
          " stale output(s) dropped for commented-out cells, which is expected",
        True, "matches the reference notebook's shape"]|>]
  ]];

MCPExportMarkdown[id_String, path_String, texMath : (True | False) : False] :=
  sessionOr[id, Module[{nb, pos, parts = {}, c, style, head, txt},
    nb = $Sessions[id, "nb"];
    pos = leafPositions[nb];
    Do[
      c = Extract[nb, p];
      style = cellStyle[c];
      head = mdHeading[style];
      Which[
        head =!= Nothing, AppendTo[parts, head <> mdCellText[c]],
        style === "Text" || style === "Item", AppendTo[parts, mdCellText[c]],
        style === "Input" || style === "Code",
          txt = mdCellText[c];
          If[StringTrim[txt] =!= "", AppendTo[parts, "```wl\n" <> txt <> "\n```"]],
        style === "Output", AppendTo[parts, mdOutput[c, texMath]],
        style === "Print", AppendTo[parts, "```text\n" <> mdCellText[c] <> "\n```"],
        True, Null
      ],
      {p, pos}
    ];
    Export[path, StringRiffle[parts, "\n\n"] <> "\n", "Text"];
    ok[<|"path" -> path, "cells" -> Length[pos], "blocks" -> Length[parts],
        "bytes" -> FileByteCount[path], "tex_math" -> texMath, "images" -> 0|>]
  ]];

(* Content with an EXPLICIT ImageSize wider than the printable area is clipped,
   not scaled, and nothing says so -- measured: a row of 12 graphics forced to
   2400 pt lost 8 of them on A4 portrait, 7 on A4 landscape, 1 on A2. The same
   row with no explicit size fits at every paper size, because the front end is
   then free to scale it. Packages that lay out diagrams commonly set a size.

   So: measure before exporting, and either say so or neutralise it. *)
(* Read the INPUT cells' source, not the output boxes.

   Every rendered graphic carries an ImageSize in its box structure, but most of
   those were computed by the front end -- and a computed size is rescaled to
   fit the page, so flagging them reports clipping that will not happen. A row
   of 12 graphics with no authored size measures ~1960 pt in its boxes and still
   prints complete on A4.

   What actually clips is a size the author WROTE, because the front end honours
   it. So look for ImageSize in the source of Input/Code cells, which is where an
   authored size lives, and ignore whatever the renderer computed. *)
authoredWidths[nb_, printable_] := Module[{srcs, nums},
  srcs = mdCellText /@ Cases[nb, c_Cell /; MemberQ[{"Input", "Code"}, cellStyle[c]], Infinity];
  nums = Flatten[StringCases[srcs,
    "ImageSize" ~~ Whitespace... ~~ ("->" | "\[Rule]") ~~ Whitespace... ~~
      ("{" ~~ Whitespace... ~~ n : NumberString ~~ ___ ~~ "}" | n : NumberString) :>
      ToExpression[n]]];
  Select[nums, NumericQ[#] && # > printable &]
];

oversizedWidths[nb_, printable_] := authoredWidths[nb, printable];

(* Widen the PAGE, do not touch the content.

   Three narrower fixes were tried and rejected, and the reasons are worth
   keeping. Setting an oversized ImageSize to Automatic leaves a GraphicsBox of
   absolutely positioned Insets with nothing to size itself from, and the front
   end draws a pink error placeholder where the picture should be. Rewriting the
   ImageSize to a smaller number does the same. Magnification avoids the
   placeholder but does not help at all -- it is a view scale, and printing
   still clips at the same content width (measured: 4 of 12 graphics survived,
   exactly as without it).

   So leave every box alone and make the paper big enough. Nothing can fail to
   render, because nothing changed. *)
fittedPaper[widest_, printable_, paperW_, paperH_] := Module[{margin = 36},
  If[widest <= printable, Return[{paperW, paperH}]];
  (* Widen only. Scaling the height to match would give a page several times
     taller than anything on it -- 2436 x 3448 pt for one row of graphics --
     which is mostly blank paper. The document simply paginates down the long
     axis as usual. *)
  {Ceiling[widest + margin], If[paperH > 0, paperH, 842]}
];

MCPExportNotebook[id_String, path_String, openGroups_: True,
                  paperW_: 0, paperH_: 0, fitWidth_: False] :=
  If[!KeyExistsQ[$Sessions, id],
    err["No such headless notebook session: " <> id],
    Module[{out, opts, doc, printable, over, pw = paperW, ph = paperH},
      (* openGroups defaults True. A notebook is normally saved with its groups
         collapsed, and exporting it that way silently omits everything inside
         them -- a record of a run that is missing most of the run. Pass False
         only when you deliberately want the collapsed view.

         Paper size matters for the same reason: content wider than the page is
         CLIPPED, not wrapped or scaled, so a row of wide graphics loses its
         right-hand end with nothing to say so. Give paperW/paperH in printer's
         points (A4 landscape is 842 x 595) when the content is wide. *)
      (* ShowCellLabel -> True is not cosmetic. Without it the front end decides
         these labels did not come from its own session and renders them in the
         placeholder form In[*]:= / Out[*]=, dropping the numbers -- which is
         exactly the pairing information the labels exist to carry. Measured:
         the same notebook exports as "In[3]:=" with this set and "In[]:=" (a
         bullet on screen) without it. *)
      (* Export the notebook as a DOCUMENT, not as an expression. Handing
         Export a raw Notebook[...] expression produces a single near-empty
         page regardless of length -- measured: a 994-cell notebook exported to
         one 5 KB page. It has to become a real NotebookObject in the front end
         first, which is what paginates it. *)
      printable = If[paperW > 0, paperW, 595] - 36;
      (* ReplaceRepeated, not ReplaceAll. ReplaceAll does not descend into what
         it has just replaced, so an outer group that matches is rewritten and
         every group NESTED inside it is carried through untouched. On a real
         document -- chapters inside a title group -- that opens the outermost
         one and leaves the rest shut: measured 2 of 158 closed groups opened,
         and an export showing chapter headings with no contents under them.
         The guard on the state is what makes the repeat terminate. *)
      doc = If[TrueQ[openGroups],
        $Sessions[id, "nb"] //. CellGroupData[c_, st_] /; st =!= Open :> CellGroupData[c, Open],
        $Sessions[id, "nb"]];
      over = oversizedWidths[doc, printable];
      If[TrueQ[fitWidth] && over =!= {},
        {pw, ph} = fittedPaper[Max[over], printable, paperW, paperH]];
      opts = Join[
        {ShowCellLabel -> True},
        If[pw > 0 && ph > 0,
          {PrintingOptions -> {"PaperSize" -> {pw, ph},
                               "PrintingMargins" -> 18,
                               "PaperOrientation" -> If[pw > ph, "Landscape", "Portrait"]}},
          {}]];
      out = Quiet[Check[
        UsingFrontEnd[Module[{nbo, res},
          (* Join needs matching heads: the notebook is Notebook[cells, opts...],
             so the extra options have to arrive as a Notebook too, not a List. *)
          nbo = NotebookPut[Join[doc, Notebook @@ Join[{Visible -> False}, opts]]];
          res = Export[ExpandFileName[path], nbo];
          NotebookClose[nbo];
          res
        ]],
        $Failed]];
      If[out === $Failed || out === Null,
        err["Front end could not export this notebook", <|"path" -> path|>],
        ok[Join[<|"path" -> out, "id" -> id, "groups_opened" -> TrueQ[openGroups],
             "paper" -> If[paperW > 0 && paperH > 0, {paperW, paperH}, "default"],
             "printable_width" -> printable|>,
          Which[
            over === {}, <|"clipping" -> "none detected"|>,
            TrueQ[fitWidth], <|"clipping" -> "avoided",
              "oversized_items" -> Length[over],
              "paper_used" -> {pw, ph},
              "note" -> "The page was widened to " <> ToString[pw] <> " pt so the widest "
                        <> "content fits. Nothing in the notebook was modified."|>,
            True, <|"clipping" -> "LIKELY",
              "oversized_items" -> Length[over],
              "widest" -> Round[Max[over]],
              "suggested_paper_width" -> Round[Max[over]] + 36,
              "action_required" -> "ASK THE USER",
              "note" -> ToString[Length[over]] <> " item(s) are wider than the A4 page "
                        <> "and have been CUT OFF -- not scaled, with nothing marking "
                        <> "where. This is a choice only the user can make, so ask "
                        <> "before re-exporting: (a) fit_width=True widens the page to "
                        <> ToString[Round[Max[over]] + 36] <> " pt so everything fits, "
                        <> "at the cost of a non-standard page size -- RECOMMENDED; or "
                        <> "(b) keep A4 and accept the missing content -- NOT "
                        <> "recommended. Do not pick for them."|>]]]
      ]
    ]
  ];


(* -- finalize: NotebookEvaluate with native In/Out labels -------------- *)

MCPFinalize[id_String, path_String] := Module[
  {session, nbExpr, tmpPath, nbo, evalResult, savedPath},

  session = Lookup[$Sessions, id, Missing["KeyAbsent", id]];
  If[MissingQ[session],
    Return[err["No such headless notebook session: " <> id]]];

  (* Save the current state to disk first *)
  nbExpr = session["notebook"];
  If[path === "",
    Return[err["finalize requires a disk path"]]];
  savedPath = path;
  Export[savedPath, nbExpr, "NB"];
  If[!FileExistsQ[savedPath],
    Return[err["failed to write notebook to " <> savedPath]]];

  (* Open in the front end and evaluate *)
  Quiet[Check[
    UsingFrontEnd[Module[{},
      nbo = NotebookOpen[savedPath, Visible -> False];
      If[Head[nbo] =!= NotebookObject,
        Return[err["NotebookOpen failed inside UsingFrontEnd"]]];

      evalResult = NotebookEvaluate[nbo, InsertResults -> True];
      NotebookSave[nbo];
      NotebookClose[nbo];

      (* Reload the finalized notebook back into the session *)
      session["notebook"] = Import[savedPath, "NB"];

      ok[<|
        "id" -> id,
        "path" -> savedPath,
        "finalized" -> True,
        "note" -> "NotebookEvaluate ran all cells with InsertResults->True; the notebook on disk now has native In[n]/Out[n] labels"
      |>]
    ]],
    err["NotebookEvaluate failed: " <> ToString[$MessageList]]
  ], {FrontEndObject::notavail}]
];


(* ------------------------------------------------------------------------ *)
(* Annotation: set Evaluatable and stamp a reason on a cell                 *)
(* ------------------------------------------------------------------------ *)

(* The recorder marks cells non-evaluatable after abort, timeout, or failure
   so that NotebookEvaluate skips them during finalization. The reason is
   stamped in TaggingRules so MCPReadBack can report it. *)

MCPAnnotateCell[id_String, index_Integer, evaluatable : (True | False), reason_String] :=
  sessionOr[id, Module[{nb, pos, target, args, opts, tr, newTr, newOpts, replacement},
    nb = $Sessions[id, "nb"];
    pos = leafPositions[nb];
    If[index < 0 || index >= Length[pos],
      Return[err["Cell index out of range", <|"index" -> index, "total" -> Length[pos]|>]]
    ];
    target = Extract[nb, pos[[index + 1]]];
    If[Head[target] =!= Cell, Return[err["Not a cell"]]];
    args = List @@ target;
    If[Length[args] < 2 || !StringQ[args[[2]]], Return[err["malformed cell"]]];
    opts = Drop[args, 2];
    (* Update TaggingRules: preserve existing, add/replace MCPAnnotationReason *)
    tr = FirstCase[opts, (TaggingRules -> v_) :> v, {}];
    newTr = Prepend[
      DeleteCases[Flatten[{tr}], ("MCPAnnotationReason" -> _)],
      "MCPAnnotationReason" -> reason];
    newOpts = Join[
      DeleteCases[opts, (TaggingRules -> _) | (Evaluatable -> _)],
      {TaggingRules -> newTr, Evaluatable -> evaluatable}];
    replacement = Cell @@ Join[{args[[1]], args[[2]]}, newOpts];
    setNotebook[id, ReplacePart[nb, pos[[index + 1]] -> replacement]];
    $Sessions[id, "dirty"] = True;
    ok[<|"id" -> id, "annotated" -> index, "evaluatable" -> evaluatable,
        "reason" -> reason,
        "cell_count" -> Length[leafPositions[$Sessions[id, "nb"]]]|>]
  ]];

(* ------------------------------------------------------------------------ *)
(* Read-back: full cell metadata for recorder verification                  *)
(* ------------------------------------------------------------------------ *)

(* MCPCells reports style, executable, and a preview. The recorder needs more:
   the source digest (same pipeline as MCPInputDigests / boxText), the
   TaggingRules that carry the record identity, and the Evaluatable option
   that controls whether NotebookEvaluate will skip a cell.

   This is a verification tool, not a display tool. It returns every cell so
   the recorder can detect injections, deletions, and reorderings by comparing
   the full sequence against the durable ledger. *)

MCPReadBack[id_String] :=
  sessionOr[id, Module[{nb, pos, cells},
    nb = $Sessions[id, "nb"];
    pos = leafPositions[nb];
    cells = Table[
      Module[{c = Extract[nb, pos[[q]]], args, opts, tr, ev, ct, src, digest, tag, replayTag, annoReason},
        args = List @@ c;
        opts = If[Length[args] > 2 && StringQ[args[[2]]], Drop[args, 2], {}];
        tr = FirstCase[opts, (TaggingRules -> v_) :> v, {}];
        ev = FirstCase[opts, (Evaluatable -> v_) :> v, Null];
        ct = FirstCase[opts, (CellTags -> v_) :> v, {}];
        tag = FirstCase[Flatten[{tr}], ("MCPRecordTag" -> v_) :> v, ""];
        replayTag = FirstCase[Flatten[{tr}], ("MCPReplayChild" -> v_) :> v, ""];
        annoReason = FirstCase[Flatten[{tr}], ("MCPAnnotationReason" -> v_) :> v, ""];
        src = boxText[First[c] /. BoxData[b_] :> b];
        digest = Hash[src, "SHA256", "HexString"];
        <|
          "index" -> q - 1,
          "style" -> cellStyle[c],
          "executable" -> executableQ[c],
          "source_digest" -> digest,
          "source_chars" -> StringLength[src],
          "source_preview" -> StringTake[src, UpTo[200]],
          "evaluatable" -> ev,
          "record_tag" -> tag,
          "annotation_reason" -> annoReason,
          "replay_tag" -> replayTag,
          "cell_tags" -> Flatten[{ct}]
        |>
      ],
      {q, Length[pos]}
    ];
    ok[<|"id" -> id, "total" -> Length[cells], "cells" -> cells|>]
  ]];

Protect[MCPAnnotateCell, MCPReadBack];

End[];
EndPackage[];
