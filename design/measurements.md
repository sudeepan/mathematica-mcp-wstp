# Measurements

Everything here was measured on this machine on 2026-09-08, against Mathematica
15.0.1, Linux-x86-64, kernel 6.12.76-linuxkit. Scripts are in `experiments/`.
Where a result is inferred rather than measured, it says so.

Two of these results contradict a plausible first reading of the evidence, and
both were caught by re-running with the flaw removed. They are written up with
the flaw included, because the flaw is the interesting part.

---

## 1. WSTP aborts a running evaluation. wolframclient cannot.

`experiments/wstp_abort_test.py`. Launch a kernel over WSTP, set a state marker,
start `Do[qq = k, {k, 1, 10^12}]`, send `WSPutMessage(link, WSAbortMessage)`
after 2s.

```
[1] kernel pid + marker      -> {58049, 424242}      (2.95s: cold launch to first result)
[2] long evaluation, abort after 2s
      -> sent WSAbortMessage at t=2.0s rc=1
      returned after 2.0s: '$Aborted'
[3] same link still usable?  -> {58049, 424242, Integer}
    WSError=0  "No WSTP problem encountered."
```

Four things at once, all of which matter:

- the evaluation **returned**, on the same link, with `$Aborted`;
- it returned **immediately** (t=2.0s, i.e. no measurable delay after the abort);
- the kernel is **the same process** — pid 58049 before and after;
- state survives — `marker` is still `424242`, and `qq` still holds the loop
  variable the aborted iteration left behind.

Contrast with the two workarounds measured in the current fork: wolframclient's
`recv_abortable` stops the *client* waiting and never signals the kernel, and
`os.kill(pid, SIGINT)` terminates the kernel outright, losing all state.

**This is the finding that justifies the rewrite.** Everything else here is
supporting detail.

## 2. WSTP distinguishes a dead kernel from a slow one, in 0.3s.

`experiments/wstp_liveness_test.py`. Same setup, but `SIGKILL` the kernel 2s into
the evaluation instead of aborting it.

```
[2] SIGKILL kernel pid 58201 at t=2.0s
    read returned after 2.3s: <WSError 1: WSTP connection was lost.>
[3] WSReady=-1  WSError=1
```

0.3s to a typed, actionable error. The current fork cannot do this: `Print`-level
and `TimeConstrained`-level bounds both require a *live* kernel to enforce them,
so a crashed kernel hangs the caller indefinitely. That is why the fork carries a
Python-side cell-count-plus-30s bound and discards the session on expiry.

With WSTP that heuristic is unnecessary, and — more importantly — a timeout no
longer has to mean "throw the session away". A timeout can now abort and keep
the state, because §1 says abort works.

## 3. Transport floor: 0.27ms, against ~30ms for the addon socket.

`experiments/perf2.py`, 20 iterations each, warm kernel.

| payload | p50 | min |
|---|---|---|
| `1+1` | 0.27 ms | 0.26 ms |
| `Integrate[1/(1+x^3),x]` | 2.06 ms | 1.80 ms |
| 100 KB string result | 10.85 ms | 6.96 ms |
| 5 MB string result | 43 ms (single sample) | — |

**A first run of this benchmark reported a flat 20.1ms for every payload size,
including the 100 KB one.** That number was an artifact: the client's read loop
polled with `time.sleep(0.02)`, so it was measuring its own poll granularity and
nothing else. Suspicion came from the *flatness* — a 100 KB result costing
exactly what `1+1` costs is not a plausible transport. Shrinking the sleep to
0.2ms produced the table above, which has the payload-dependent shape a real
transport should have. If you re-benchmark, select on the link fd rather than
polling at all.

The 5 MB round trip is worth noting separately: the current fork's JSON-over-TCP
transport caps responses at 20 MB (`MAX_RESPONSE_BYTES`) and pays JSON encode and
decode on both sides. WSTP moved 5 MB in 43ms with neither.

## 4. The front end runs fully headless on this box, and is fast.

The current server's profile text says "this host has no frontend". That is a
statement about how the server is configured, not about the machine. There is a
front end installed, and one was already running when I started
(`WolframNB -server -noSplashScreen -platform offscreen`, pid 30941).

`experiments/fe_scale_test.py`, driving `UsingFrontEnd` over a WSTP link:

```
UsingFrontEnd[$FrontEnd]                    -> -FrontEndObject-   (1.8s cold)
$VersionNumber through the FE               -> 15.
Rasterize[Style[Integrate[1/(1+x^3),x],24]] -> 805x92 px image
CreateDocument + SelectionEvaluate 1+1      -> Out[1]= 2          (1.1s)
Export[".../fe.pdf", nb]                    -> real PDF 1.5, 7181 bytes, 1 page
```

At the scale that matters — a ~1000-cell production notebook:

```
[A] NotebookOpen through the offscreen FE
      {total cells, Input cells} = {994, 255}          in 1.5s
[B] kernel-only Get[] of the same file
      {top-level exprs, Input cells} = {2, 255}        in 0.1s
[C] Rasterize one real cell -> 848x57 px, PNG 17429 bytes, in 0.1s
```

994 matches the cell count recorded for this notebook. `[B]` returning `2` is not
a discrepancy: `Get` yields the notebook expression whose top level is two
`CellGroupData` groups, so the flat document-order view has to be reconstructed
by the parser, whereas the front end has it natively. Input-cell counts agree at
255 across both paths, which is the cross-check that matters. (255 is
`CellStyle -> "Input"` specifically; a "276 code cells" figure presumably counts
other code styles too.)

**No display, no X server, no configuration.** `UsingFrontEnd` spawns
`WolframNB ... -platform offscreen` as a child of the kernel in 1.8s, and it is
reaped when the kernel exits cleanly — I watched our front end (pids 58459/58466)
disappear with its kernel. A front end orphaned by an *abnormal* kernel exit does
leak: pid 30941 has been up 2h28m at 170 MB with its parent long gone.

## 5. The front end is a rendering service, not an evaluator. (Negative result.)

This is the one that changes the design, and it took three attempts to establish
honestly.

**Attempt 1** dispatched a long loop via `SelectionEvaluate` and saw no output
cell after 91s. That looked like a clean negative — but the cell content in the
notebook read back as `Cell[BoxData[BoxData[, Do[...], $CellContext`CELL,
$CellContext`DONE]]]`, which is malformed. My own Python-to-WSTP-to-`ToExpression`
escaping had mangled it through two layers of quoting. **A cell that cannot parse
proves nothing about evaluation**, so the result was discarded.

**Attempt 2** rebuilt the cell with no string literals at all
(`Cell[BoxData[FromCharacterCode[...]], "Input"]`) and tried `1+1`:

```
verify cell text -> Cell[BoxData[RowBox[{1, +, 1}]], Input]      (well-formed)
cells after 1.1s = 2
NotebookGet    -> Cell[BoxData[2], Output, CellLabel -> Out[1]=]
```

So front-end dispatch *does* evaluate, and completion *is* observable. That is
already better than the current addon, which can only ever return
`evaluation_pending` because its socket handler occupies the kernel's main link
while it polls.

**Attempt 3** used a well-formed cell holding real work — a loop measured at
**5.1s** when run directly in the kernel:

```
[0] baseline in-kernel                     5.1s wall
[1] dispatch via SelectionEvaluate         returned at t=0.04s
    our link mid-eval: $ProcessID          answered in 0.02s
    FE eval completed at                   t=200.4s -> NO. cells still 1.
```

A 5-second job, unfinished after 200 seconds. The obvious suspect was my own
polling — each `UsingFrontEnd[Length[Cells[nb]]]` probe is itself a kernel
evaluation and could be starving the queue. So `experiments/fe_starve_test.py`
dispatched the same cell and then went **completely silent on the link for 40s**:

```
dispatched; now sleeping 40s with ZERO link traffic
after quiet 40s, cells = 1        (1 = never ran)
```

Not a polling artifact. When an external WSTP client owns the kernel's main link,
the front end has no evaluator servicing its queue, and dispatched cells beyond
trivial ones simply never run. `1+1` succeeding in attempt 2 is consistent with
it being serviced inside the `UsingFrontEnd[...]` block itself, within a single
slice.

**Design consequence:** do not route evaluation through the front end. Use it for
what it uniquely provides — typesetting, rasterization, box normalization, flat
document-order cell structure, PDF/PNG export — and evaluate over the kernel
link, where §1 and §2 both hold. See `architecture.md` §4.

## 6. Two evaluation queues need two different abort channels.

Continuing `experiments/fe_abort_clean.py` from the state above, with a
front-end-dispatched evaluation outstanding:

```
[2] WSAbortMessage on our kernel link      -> no effect, nb2 cells still 1
[3] UsingFrontEnd[FrontEndTokenExecute["EvaluatorAbort"]]
      -> nb2 cells: 2
      -> nb2 last: Cell[BoxData[$Aborted], Output, ...]
      -> kernel pid unchanged (59746)
```

`WSAbortMessage` addresses the link's own evaluation queue; our link was idle, so
the abort landed on nothing. `EvaluatorAbort` is the front end's own channel and
produced a genuine `$Aborted` **output cell**. Both are needed, and a server that
exposes one `abort` tool has to know which queue the target work is sitting in.

## 7. WSTP link activation can hang forever.

Opening a link to a nonexistent kernel binary
(`-linkname '/nonexistent/WolframKernel -wstp' -linkmode launch`) returns a
non-null link, and then `WSActivate` **blocks indefinitely**. I killed it at
3m37s. Launch and activation must run under a watchdog with a hard deadline; this
is the one place where WSTP is *worse* than a subprocess call, which at least
returns a nonzero exit.

Listen-mode link creation, needed for attaching to an already-running kernel or
front end rather than launching one, works on all three protocols tried:

```
-linkmode listen -linkprotocol TCPIP       -linkname 21345    -> OK
-linkmode listen -linkprotocol SharedMemory -linkname mcptest1 -> OK
-linkmode listen                            -linkname 21346    -> OK
```

## 8. The process leak, measured live.

A `ps` snapshot taken before I had launched anything:

- **37** `WolframKernel` processes
- **7** orphaned to init (ppid 1), including one at **11.0 GB** RSS and one at 1.08 GB
- **1** zombie
- **~23.9 GB** total RSS across all Wolfram processes
- a `wolframscript -code (Do[xLongMarker = k, {k, 1, 10^12}]; "FINISHED")` burning
  CPU for **4h18m**, orphaned to init — a runaway that exists precisely because
  there was no way to abort it

Of those kernels, **19 are `-subkernel` processes at ~400 MB each (~7.8 GB)**
whose parent is pid 82440 — itself an orphan. That is one `LaunchKernels[]`
fan-out stranded by a kernel restart, matching the ~8.2 GB figure recorded for a
single `kernel(action="restart")` call. Reading the fork's restart path confirms
the mechanism: `restart_kernel` calls `close_kernel_session`, which calls
`_kernel_session.terminate()` on the master only. Nothing enumerates or closes
subkernels, and nothing reaps.

**A note on method.** I nearly misattributed those 19 subkernels to my own tests,
because they matched `-wstp` — which is also in my launch command line. They are
not mine; they are `-subkernel -wstp -linkconnect` children of 82440. Pattern
matching on a command line is exactly the trap that makes `pkill -f` dangerous
here. The reaper in `architecture.md` §3.3 keys on a recorded pid registry
instead, and never on a command-line regex.

My own kernels all exited cleanly: after every experiment the only launch-mode
kernels left were zero, and the total moved 37 -> 38, fully accounted for.

## 9. Results need no deserialization. Let the kernel format.

`experiments/nopython.py`. An earlier draft of the architecture recommended
keeping wolframclient as a WXF-decoding library. That was wrong, and this is the
measurement that settles it.

```
ToString[expr, InputForm]           -> {1, 2.5, "abc", Sin[x], <|"k" -> {1, 2, 3}|>, 1/3}
ToExpression[ToString[...]] === expr -> True
```

The whole result comes off the link as one string and parses back to the
identical expression. Nothing needs to become a Python object on the way, because
nothing downstream is Python: results travel to a language model as text through
JSON-RPC. Decoding to Python objects only to re-serialize them to text is a
detour with a lossy step in the middle.

Precision is the trap, and it is in the form, not the transport:

```
Pi/3 // N   InputForm:  1.0471975511965976    round-trips
Pi/3 // N   default:    1.0472                does NOT compare equal to itself
```

The first version of this note used `N[Pi,17]` as the example, and that was a
bad choice that made the problem look harmless: an arbitrary-precision number
keeps its digits under `OutputForm` and still compares equal, so the round trip
appears to succeed. It was a test asserting exactly that which failed and
prompted the recheck.

The damage is on **machine** reals, which is what almost every numeric result
actually is — six significant digits, silently, and `ToExpression[ToString[x]] == x`
comes back `False`. Arbitrary-precision values are hurt more quietly: the value
survives but the precision annotation degrades, `30.` becoming `29.497...`.
Both failure modes are covered by tests.

Structured data the server branches on comes back as kernel-side JSON, parsed
with stdlib:

```
ExportString[<|...|>, "RawJSON", "Compact"->True] -> {"ok":true,"aborted":false,"cells":994,"msgs":[]}
```

The one payload shape where a binary path wins is bulk numerics — 200,000
machine reals:

| | size | time |
|---|---|---|
| `ToString[..., InputForm]` | 3.56 MB | 0.22s |
| `BinarySerialize` (WXF) | 1.80 MB | 0.09s |

~2x smaller, ~2.4x faster. Real, but on a payload this server does not carry, and
`WSGetReal64Array` covers it natively if that ever changes.

For completeness, the hybrid *does* work — `experiments/wxf_hybrid.py` pulls WXF
bytes off a WSTP link and decodes them with `binary_deserialize` into
`(1, 2.5, 'abc', Sin[Global`x], {'k': (1, 2, 3)}, Rational[1, 3])`. It works and
it is still the wrong choice. Feasibility was never the question.

## 10. Notebook export: my "first-page-only" claim was WRONG.

This section previously reported that headless export renders only the first
page. That conclusion was wrong, and the way it was wrong is the useful part: it
generalised a whole-capability claim from a single notebook.

**Export paginates correctly and scales.** Synthetic notebooks:

| cells | pages | last cell rendered |
|---|---|---|
| 200 | 7 | Line 200 |
| 500 | 16 | Line 500 |
| 994 | 31 | Line 994 |
| 200 heavy `Output` cells | 40 | — |

No cap, no headless limitation, nothing wrong with the front end.

**What actually shortened the export was closed cell groups.** Export renders
what is VISIBLE — exactly as printing from the GUI would — so a collapsed
section exports collapsed. The notebook measured here has **158 of its 335
Opening them multiplies the exported text 27x:

```
as-is                 5246 B   1 page     54 chars
all groups opened    47333 B   2 pages  1479 chars
```

`render(action="export", open_groups=True)` does this.

**Residual, still unexplained.** Even fully opened, that notebook exports to 2
pages, which is fewer than 994 cells implies. Ruled out: group state (only
`Open`/`Closed` exist in the file, and forcing all `Open` changes nothing
further), notebook options (stripping `WindowSize`, `StyleDefinitions` and the
rest changes nothing), and any general pagination cap (the table above). The
cause is specific to that file and I have not found it. `open_groups` is
therefore offered, not promised as a fix.

**How the error happened.** The original four-way test — `Export` of the raw
expression, `NotebookPut`+`Export`, `NotebookOpen`+`Export`, `NotebookPrint` —
gave byte-identical output, which felt like strong evidence. It was strong
evidence for "these four routes agree", and I read it as "the front end cannot
paginate". Every route was reading the same closed groups. The missing control
was a notebook I constructed myself, which takes one line and would have
falsified the claim immediately.

## 11. There is no licence limit here. "Licence starvation" was a misdiagnosis.

The architecture doc carried a hypothesis about how a refused seat would surface
over WSTP. Two brute-force runs failed to trigger one:

```
cap 24 -> 24 kernels held, no refusal, startup 1.6s -> 3.0s
cap 45 -> 45 kernels held, no refusal, startup 1.6s -> 2.7s
```

With ~12 kernels already running that is **57 concurrent kernels**, past the 49
previously blamed for a starvation incident. Asking the kernel directly explains
why nothing was ever going to fail:

```
$LicenseType             "Professional"
$MaxLicenseProcesses     Infinity
$MaxLicenseSubprocesses  Infinity
$ProcessorCount          20
```

**No seat limit exists on this machine, for kernels or for subkernels.**
`LaunchKernels[64]` launched all 64 and `CloseKernels[]` closed all 64.
(`LaunchKernels[]` with no argument gives 20, matching `$ProcessorCount` — which
is where the "20 subkernels" figure comes from; it is a core count, not a
licence.)

So whatever produced the original symptom — a cold `wolframscript` exiting with
**empty output and no error** — it was not licence starvation. The far more
likely cause is **memory**: the same snapshot that showed 49 leaked kernels
showed ~23.9 GB resident with a single 11.0 GB outlier, on a 30 GB machine. A
kernel that cannot get memory at startup is OOM-killed, and an OOM-killed
process produces exactly that signature: no output, no error, nonzero-or-zero
exit depending on the shell.

**The actionable correction: the scarce resource here is RAM, not licences.**
That makes the process leak the whole story rather than half of it, and it means
the reaper is the fix for both symptoms.

Two brute-force runs and ~20 minutes could have been replaced by one query for
`$MaxLicenseProcesses`. Worth remembering: ask the system what its limits are
before trying to reach them.

## 12. Shutdown was waiting on zombies. 10.03s -> 0.27s.

Tearing down the 45 kernels above was far slower than it should have been --
about four kernels closed in ten minutes -- which exposed a real bug.

A single `close()` on a kernel with no subkernels took **10.03s**. Stage timings:

```
CloseKernels eval                  0.34s
Quit[] eval                        0.25s   (raises LinkDead, as expected)
link.close()                       0.00s
wait for pid death                 5.01s   <- full grace, pid still "alive"
proc.wait                          0.00s   <- returns instantly: already exited
```

The last two lines are the contradiction that gives it away. The kernel *had*
exited; it was a **zombie**, because nothing had waited on it. `os.kill(pid, 0)`
succeeds on a zombie, so `pid_alive()` reported a dead kernel as alive, the
grace loop ran to completion, and then the code signalled a corpse — twice over,
5s each.

The first hypothesis was that `Kernels[]` was expensive because it initialises
the parallel subsystem. Measuring killed it: `Length[Kernels[]]` costs 0.35s
once and 0.00s thereafter. The stage timings above are what found the real
cause.

Fixed in two places: `registry.pid_alive` now treats state `Z` as dead, and
`Kernel.close` reaps its own child as it waits. Mean close is now **0.27s**, and
the unit suite went from ~135s to ~28s as a side effect. Both halves have
regression tests.

This also explains the `[WolframKernel] <defunct>` zombie seen in the original
process snapshot (§8): same mechanism, in the server this one replaces.

## 13. Messages and Print were being silently discarded.

Found by asking a question the tests had not: what happens to everything the
kernel says *besides* the answer?

```
Print["HELLO"]; 42   ->  value 42, and nothing else. The Print vanished.
{1,2}[[5]]           ->  value {1, 2}[[5]], with no sign Part::partw had fired.
1/0                  ->  value ComplexInfinity, with no Power::infy.
```

For symbolic physics this is the worst kind of bug: the answer looks plausible,
the warning that explains why it is wrong is gone, and the next hour goes into
debugging the wrong thing.

Dumping the raw packet stream showed the kernel had been sending it all along:

```
Print["HELLO"]; 42   INPUTNAMEPKT, TEXTPKT, RETURNPKT
{1,2}[[5]]           MESSAGEPKT(symbol), TEXTPKT, RETURNPKT
```

The read loop kept `RETURNPKT` and threw the rest away. A message arrives as a
`MESSAGEPKT` naming symbol and tag, followed by a `TEXTPKT` with the rendered
text; `Print` output is a bare `TEXTPKT`. **That adjacency is the only thing
distinguishing them**, so the reader tracks a pending message across packets
rather than classifying each one in isolation.

A second bug surfaced while fixing it: `link.py` had `WSTKSTR = ord("S") = 83`,
guessed from the mnemonic. The real value is **34**; 83 is `WSTKOLDSTR`, the
pre-interface-3 form. Every string token was therefore misclassified, which made
message capture look impossible until the constants were compiled out of
`wstp.h` rather than inferred.

Now surfaced on every `evaluate`, with the name kept separate from the text:

```
{1,2}[[5]]  ->  messages: [{"name": "Part::partw", "symbol": "Part", "tag": "partw",
                            "text": "Part::partw: Part 5 of {1, 2} does not exist."}]
```

Message text is whitespace-collapsed and otherwise untouched. An earlier version
trimmed everything before the `Symbol::tag:` marker, which tidied `1/0` by
discarding the `1` -- part of the expression the message is about. The clean
identifier is already in `name`, so the text stays faithful rather than pretty.

## 14. The acceptance replay passes.

The acceptance test named in architecture §7, finally run rather than described.

```
baseline: 13 kernels, 3527 MB
opened:   994 cells

replay COMPLETED after 248s
  cells seen             : 994 of 994
  CODE cells executed    : 275
  non-code cells skipped : 718
  aborted / failed       : 1 / 0
  subkernels live        : 20
  slowest chunks: cells 925+ 143s, cells 0+ 26s, cells 215+ 9s

after: 13 kernels, 3527 MB
  net kernels vs baseline : +0
  stranded subkernels     : 0
  registry orphans        : []

PROCESS TABLE: CLEAN
VERDICT: PASS
```

275 executed code cells against the 276 the notebook contains, and 20
subkernels live throughout — so this is the real workload, not a reduced one.

**The single abort is the point, not a blemish.** The chunk at cell 925 took
143s against a 120s per-cell limit: a symbolic simplification over a ~129k-leaf
expression that does not terminate in an hour. It was aborted, the kernel kept
its state, and the replay carried on through the remaining cells. On the old
transport that cell ends the session.

**And the process table returned to exactly where it started** — the half the
previous server failed, on precisely this workload, by stranding a 20-way
`LaunchKernels[]` fan-out worth ~8 GB.

248 seconds, against the ~10 minutes recorded for a full explicit replay before.

### Two flaws in the harness, caught before the result was believed

**The first run counted skipped cells as successes.** `MCPEvaluateRange` calls
`evalCell` on every cell in the span, and a Text or Output cell returns
`{"success": True, "skipped": True}`. The harness reported "ran=605 ok=605",
which reads as a triumphant replay and was in fact 429 skipped prose cells. The
run was killed and the accounting split into executed / skipped before any
number was trusted. Only 275 of 994 cells run code; the other 718 are prose and
stored output.

**"Is it still running?" was answered by a grep matching itself.** Several
checks grepped the harness's own script name over a `ps` snapshot and reported
the run
alive when it was not, because the checking command's own line contains the
pattern. The same mistake appeared a third time in the post-run audit, where
`grep -c subkernel` reported 4 stranded subkernels that were all shell commands
containing the word. This is the `pkill -f` hazard wearing different clothes,
and the rule that survives it is: match on the **binary path**
(`SystemFiles/Kernel/Binaries`), never on a word that could appear in your own
command line.

---

## Not measured

Stated for completeness, so nobody mistakes these for verified:

- **Licence starvation under WSTP.** I did not exhaust the licence pool, so I have
  not confirmed what a refused seat looks like over WSTP. The *mechanism* to
  expect is a failed `WSActivate` plus a real `WSErrorMessage`, which would be a
  strict improvement over a cold `wolframscript` exiting with empty output and no
  error. Treat as a hypothesis with a cheap test: hold N links open, open N+1.
- **Abort against a kernel inside an uninterruptible call.** §1 aborts a `Do`
  loop, which checks for aborts often. A kernel blocked inside a long
  `FullSimplify` should also be interruptible, but a kernel blocked in a C library
  call or a `RunProcess` to an external solver may not be. Worth measuring before
  promising abort as unconditional.
- **Whether a front end in `-server` mode with its own kernel evaluates normally.**
  §5 measures the `UsingFrontEnd` topology only. The three-process topology
  (server + own kernel + front end + front end's own kernel) costs a second
  licence seat and was not tested.
