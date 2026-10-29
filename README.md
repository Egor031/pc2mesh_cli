# pc2mesh_cli

```text
pc2mesh_cli.exe extract input.igs output.ply
python pc2mesh_cli/scripts/reconstruct_poisson.py input.ply output.obj [log.txt]
```

Outputs replace existing files only after checked writing to a sibling temporary
file. Input/output/log aliases are rejected before writing and checked again
before publication. The input is held against writes/deletion on Windows.
The documented reconstruction output is OBJ; other output extensions are rejected.
OBJ precision and the geometry algorithms are unchanged.

The optional log is staged separately. A processing failure can replace a healthy
log with error diagnostics; a failure writing/publishing the log preserves its old
contents and returns a nonzero exit code. A failed log publication rolls back an
already published mesh where the filesystem permits it.

See [file safety tests and limitations](tests/README.md).
