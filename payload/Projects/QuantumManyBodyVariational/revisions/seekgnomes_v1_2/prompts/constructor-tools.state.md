# Constructor tools

Invoke `benchmark_action` with `tool` and `arguments`.

- `list_files {}` lists private files and readable references.
- `read {path, start_line:1, max_lines:240}` reads a private or reference file.
- `write {path, content, append:false}` writes a private file.
- `server {candidate:"experiment.py", cpus:1, wall_seconds:120, arguments:[]}`
  runs a private exploration script in the networkless numerical sandbox. That
  sandbox allows NumPy, SciPy, TeNPy and QuTiP for physics exploration. Use the
  separate state language for the candidate itself.
- `create_handoff {definition_path:"f.state", notes:"optional proof hints"}`
  freezes the candidate and starts Formalist. The tool reports a specific issue
  or a strict verification receipt with floating-point energy per qubit.
- `finish {reason:"..."}` ends exploration.
- `web_search {query, max_results:5}` and `web_fetch {url, max_chars:20000}`
  consult public references when enabled.
