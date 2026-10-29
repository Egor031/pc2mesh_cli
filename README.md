# pc2mesh_cli

```text
pc2mesh_cli.exe extract input.igs output.ply
python pc2mesh_cli/scripts/reconstruct_poisson.py input.ply output.obj [log.txt]
```

Outputs replace existing files only after checked writing to a sibling temporary
file. Input/output/log aliases are rejected before writing and checked again
before publication. The input is held against writes/deletion on Windows.
The documented reconstruction output is OBJ; other output extensions are rejected.
PLY 1.0 data is validated before Open3D reads it. Truncated/inconsistent records,
non-finite values and unsupported declarations fail before reconstruction.
OBJ positions, vertex normals and colors use 17 significant decimal digits for
binary64 round trips. The reconstruction algorithms are unchanged.

The optional log is staged separately. A processing failure can replace a healthy
log with error diagnostics; a failure writing/publishing the log preserves its old
contents and returns a nonzero exit code. A failed log publication rolls back an
already published mesh where the filesystem permits it.

See [file safety tests and limitations](tests/README.md).

IGES point data is extracted directly: type 116 form 0 and type 106 forms 1/2/3
(XY with common Z, XYZ, XYZ with vectors). All point occurrences are retained;
vectors are not points. Explicit Cartesian transformations are applied once,
including matrix chains, followed by conversion to millimeters. Coordinates stay
in IGES model space; a non-unit global model-space scale is reported rather than
converted to real-world coordinates.

CAD-only files retain extraction of OCCT topological vertices, without sampling
curves or surfaces. Mixing point data with CAD geometry is rejected. Unsupported
entities/instance placements, damaged numeric data, model/transfer failures and
non-finite coordinates fail the whole import before PLY publication. Presentation
data such as colors, names and view clipping planes do not become cloud points;
non-placement groups do not duplicate their members. See the
[IGES import policy, tests and limits](tests/README.md#iges-import-pc2mesh-release-01b).

See [PLY validation and OBJ precision](tests/README.md#ply-and-obj-pc2mesh-release-01c)
for supported PLY layouts, resource bounds and Open3D reader limitations.
