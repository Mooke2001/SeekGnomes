# Formalist tools

Invoke `benchmark_action` with `tool` and `arguments`.

- `list_files {}`, `read {path, start_line:1, max_lines:240}` and
  `write {path, content, append:false}` operate on private files or read-only
  references. The frozen source is `reference/handoff/f.state`.
- `prepare {}` builds the frozen source. Read `generated_paths.generated_state`
  and `generated_paths.proof_template` from its response and follow the template.
- `run_lean {proof_path:"Candidate.lean"}` checks your proof of the fixed goal
  without evaluating energy. Use returned diagnostics to refine your proof.
- `score {proof_path:"Candidate.lean"}` verifies your proof and, if verified,
  evaluates energy from the same frozen definition. Success returns to Constructor.
- `report_issue {kind, detail}` returns a concrete defect in the frozen
  definition to Constructor. Use `definition_ambiguous`,
  `definition_inconsistent` or `representation_error` and identify the
  affected expression and the evidence. The host handles infrastructure failures.
- `web_search {query, max_results:5}` and `web_fetch {url, max_chars:20000}`
  consult public references for the given proof when enabled.
