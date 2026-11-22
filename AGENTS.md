# Repository instructions

- This is a C++/Python FairShip repository using ROOT and FairRoot.
- Use `pixi run build` for compilation when the Pixi environment is available, and pixi cvmfs build if inside the pixi-cvmfs environment.
- Do not modify generated build files.
- Preserve existing formatting and make focused changes only.
- Do not run large simulations or submit batch jobs unless explicitly requested.
- Do not push, force-push, rebase, or amend commits unless explicitly requested.
- Prefer building only the affected target when possible.
- Treat files under `/eos`, experiment data, credentials, tokens, and certificates
  as sensitive.
- Before editing, explain the suspected cause of a bug.
- After editing, show the diff and report exactly which tests or builds were run.
## ROOT / ShipMuDIS diagnostics

- Run ROOT/PyROOT diagnostics sequentially. Never launch another check
  while an earlier ROOT process is still running.
- Use `pixi run --as-is` for diagnostics with the existing environment.
- Load `libShipMuDIS.so` once per diagnostic process. If application
  initialization already loads it, do not add another explicit load.
- Use unbuffered Python output (`python -u`) and print progress before
  and after library loading.
- Bound standalone diagnostics with `timeout 240s`. If loading stalls,
  stop and inspect that attempt; do not launch duplicate checks.
- If the agent environment stalls, try once more, then ask the user to run the same bounded
  command in their terminal and share the output.
- Do not assume a missing PyROOT attribute means the library failed
  to load: check the load result and C++ dictionary exposure separately.