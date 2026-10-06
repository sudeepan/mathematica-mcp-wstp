# Running the tests

The suites use their own runners and need no test framework. Most of them
start a Wolfram kernel, and some a headless front end, so they need
Mathematica; the rest run anywhere.

## What runs where

| Suites | Needs Mathematica | On GitHub | Locally |
|---|---|---|---|
| `test_recorder_hardening`, and the pure-Python tests of the five other `test_recorder_*` suites | no | yes | yes |
| the tests of those five suites that start a kernel | yes | skipped, with the reason | yes |
| `test_kernel`, `test_recorder_server`, `test_server_mcp`, `test_supervisor` | yes | no | yes |

GitHub runs its part from `.github/workflows/tests.yml` on every push and pull
request, on Python 3.10 and 3.13.

## Before a push

Run every suite on a machine with Mathematica:

```bash
tests/run_all.sh              # every suite, about 12 minutes
tests/run_all.sh --no-kernel  # only what GitHub runs
```

It uses `.venv/bin/python` (see the README's Quick start), or the interpreter
named by `PYTHON`. Each suite's output goes to a log file in `TEST_LOG_DIR`,
or a new temporary directory; the summary prints one line per suite, and on a
failure the end of each failing log.

## Why the full suites do not run on GitHub

GitHub's machines have no Mathematica. Wolfram Engine can be licensed there,
but Wolfram has described it as having no notebook front end, which the
recorder needs to save and finalize. A self-hosted runner, a machine of your
own registered with GitHub, could run everything, but GitHub recommends them
only for private repositories: a pull request from a fork can change the
workflow and run its own code on that machine. It would also run Mathematica
under your own licence, whose terms may not allow it. This repository is
public, so the full suites run locally instead.
