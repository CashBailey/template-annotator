Execute the improvement plan in GOALS.md at the root of /home/gatorhub/areaDef. That file is the single source of truth: 6 phases of checkbox tasks from the 2026-07-28 audit (areaDef.py line numbers pinned to commit 3623b39 — re-grep after edits shift them).

Rules:
1. Work phases strictly in order (1 data-safety → 2 decoupling → 3 CI → 4 performance → 5 architecture → 6 hygiene). Within a phase, take items top to bottom unless a dependency says otherwise.
2. Before touching code, read the cited lines plus enough surrounding context to confirm the finding still holds; if the code has drifted, re-derive the fix from the finding's intent, not its line numbers.
3. Every behavior change lands with a pinning test in the same commit (Phase 1 lists its required tests explicitly). Bug fixes are root-cause fixes — grep all callers before patching one path.
4. Never break the standing contracts: tests/test_areadef.py stays runnable by plain `python -m unittest` with zero non-stdlib test deps; fixtures stay byte-deterministic (`python scripts/gen_fixtures.py --check`); goldens regenerate only via `--update-goldens` when rendering intentionally changes.
5. After each phase, run the verification block at the bottom of GOALS.md (unittest 179 ran/0 fail/0 skip, fast pytest lane, e2e serial, gen_fixtures --check). Only then edit GOALS.md to check the items off, and commit — one commit or a small series per phase, messages prefixed "phase N:".
6. Use the project venv interpreter for everything: /home/gatorhub/areaDef/.venv/bin/python.
7. If a finding proves wrong or a fix is riskier than the audit assumed, do not force it: annotate the item in GOALS.md ("SKIPPED: <reason>") and move on.
8. Work on a branch or worktree; merge to main only when the phase is fully green.

The goal is complete when every checkbox in GOALS.md is either checked or annotated SKIPPED with a reason, and the final verification block passes on main.
