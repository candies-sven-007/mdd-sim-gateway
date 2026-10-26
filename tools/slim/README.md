# Temporary: main.py → routers split (stage 3 of docs/SLIM_PLAN.md)

Delete this directory when stage 3 is done. Nothing here ships or runs on the device.

- `main_source.py`: the generator's input. It is `control/app/main.py` as of commit 6f912d4
  (before the split), plus one manual change: `refresh_egress()` was extracted from
  `api_egress_refresh` so the system router does not import the egress router. Make any other
  code change needed for the split here, never in the generated files.
- `split_an.py`: cuts the source into top-level blocks and finds which module-level names each
  block uses, taking scope into account (locals, parameters, comprehensions, classes).
- `split_gen.py`: assigns blocks to modules and writes `control/app/main.py`, `state.py`,
  `tasks.py` and `routers/*.py`. Routes go to their router by URL. Helpers used by only one
  module go with it. Helpers used by more than one go to `state.py`, together with everything
  they need. A name defined in another module is rewritten as `app_state.<name>`,
  `app_tasks.<name>` or `<router>_routes.<name>`, so patching the defining module reaches every
  caller. It also writes `layout.json` (name → module) for `retarget.py`.
- `retarget.py`: rewrites the tests' `main.<name>` and `patch.object(main, "<name>")` to the
  module that now defines `<name>`, and adds the imports. It prints `MANUAL` for what it will
  not decide itself: a patch that rebinds an imported module on `main` (e.g.
  `patch.object(main, "egress")`) must target the module whose code reads that name.

Run from the repository root with Python 3.12 (the control plane's venv). Pass the routers
moved so far, plus the new one:

```sh
timeout 120 python tools/slim/split_gen.py tools/slim/main_source.py \
    auth,esim,egress,logs,system,softphone,engine,lines --list
timeout 60 python -m pyflakes control/app/main.py control/app/state.py control/app/routers/
timeout 120 python tools/slim/retarget.py          # rewrites tests; read its MANUAL lines
timeout 600 python -m unittest discover -s tests
```

Pass `tasks` in the list to move the four background tasks into `tasks.py`.

Each run regenerates every module from `main_source.py`. So never hand-edit generated control
code between steps: put the change in `main_source.py` and regenerate. Tests are edited in
place.
