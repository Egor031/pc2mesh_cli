# Regression tests (PC2MESH-RELEASE-01A / 01B)

The tests use `unittest`, the application's existing NumPy/Open3D environment,
and a small C++ driver. They create their own temporary files; user IGES/PLY/OBJ
files and existing build directories are not used as write targets.

## Build and run

Use an x64 Developer PowerShell for the installed Visual Studio/C++20 toolset.
The application's existing OCCT include/library settings must be available.
Put the required OCCT/third-party runtime DLL directories on this shell's PATH
or copy the DLLs into the temporary application's directory.

```powershell
$repo = 'D:\ZherlitsynEE\pc2mesh_cli'
$work = Join-Path $env:TEMP ('pc2mesh-01a-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work | Out-Null
msbuild "$repo\pc2mesh_cli\pc2mesh_cli.vcxproj" /t:Build /p:Configuration=Release /p:Platform=x64 "/p:OutDir=$work\app\" "/p:IntDir=$work\obj\"

Push-Location $work
try {
    cl /nologo /std:c++20 /EHsc /W4 /MD "/I$repo\pc2mesh_cli" "$repo\tests\file_safety_driver.cpp" "$repo\pc2mesh_cli\src\Core\FileSafety.cpp" "$repo\pc2mesh_cli\src\export\PlyPointExporter.cpp" "$repo\pc2mesh_cli\src\export\XyzPointExporter.cpp" "/Fe:$work\file_safety_driver.exe"
} finally {
    Pop-Location
}

$env:PC2MESH_TEST_EXE = "$work\app\pc2mesh_cli.exe"
$env:PC2MESH_TEST_DRIVER = "$work\file_safety_driver.exe"
# Optional read-only real-data oracle; omit if this file is unavailable.
$env:PC2MESH_TEST_IGES = 'D:\ZherlitsynEE\1\det4.igs'
$env:PYTHONDONTWRITEBYTECODE = '1'
& "$repo\pc2mesh_cli\scripts\.venv\Scripts\python.exe" -B -m unittest discover -s "$repo\tests" -v
```

C++ test groups explicitly skip if their executable variable is absent. Symlink
tests skip on Windows error 1314 (privilege unavailable); they do not enable
Developer Mode or request administrator access. Junctions, hard links and short
names are tested independently. ACL tests deny writes only in a newly created
test directory and restore the ACL in `finally`.

## Coverage

- All input/output/log pair collisions, relative components, Windows case,
  hard links, symlinks, junctions and available 8.3 aliases.
- SHA-256 preservation of input, old result and old log on rejected collisions.
- Open/write/flush/sync/close/publication failures. Rare write/close failures
  use deterministic injection into the same I/O path as production; no disk
  filling or forced system failures are used.
- Actual Windows sharing violations, read-only output and directory ACL denial.
- Repeated successful replacement, sibling temporary-file cleanup, spaces and
  another working directory; standalone CLI exit codes and diagnostics.
- An IGES fixture with five known points and real Poisson reconstruction of a
  generated sphere; byte-for-byte OBJ serialization comparison with Open3D 0.19
  for the attributes produced by reconstruction, retaining six significant digits.
- Rollback after log publication failure; recoverable backup retention if
  rollback itself is blocked; warning if an unused backup cannot be removed.

## File safety policy and limits

Existing paths are checked by file identity, in addition to normalized/resolved
names. Windows input handles deny writes and deletion while processing. Path
checks repeat before publication. Device paths and alternate data streams are
not output files and are rejected.

PLY/XYZ use exclusive sibling temporary files, checked formatted writes,
`fflush`, `_commit` and `fclose`. OBJ uses a checked Python text stream, flush,
`fsync` and close. Windows publication uses `MoveFileExW` with replacement and
write-through, without cross-volume copy. Existing output DACLs/inheritance are
copied before replacement; ownership, timestamps, file identity and other
metadata are not promised to be preserved. Existing symlinks resolve to their
referents; a changed binding is rejected on recheck.

The optional journal is buffered and staged. Mesh and log are both finished
before either is published. The mesh is published first, the log last; a backup
allows rollback of the previous mesh if log publication fails. Error diagnostics
can be published to a healthy log while retaining the previous mesh. A log's own
write/publication failure preserves its old contents and reports to stderr.
Success is printed only after required publications complete.

Two publications are not an atomic filesystem transaction. A concurrent process,
directory/junction rebinding, a network filesystem or a system failure can still
interfere after a check or prevent rollback/cleanup. A blocked rollback returns
failure and retains the old bytes in a `.pc2mesh-*.bak` file whose path is reported.
If publication succeeded but unused-backup deletion is blocked, the committed
results remain successful and a warning reports the retained backup. A process
crash can leave staging/backup files or a mesh/log from different runs. Flush and
write-through are not a promise of power-loss atomicity or durability.

Real disk exhaustion, power loss, hostile races, and filesystems other than the
tested local Windows filesystem require separate verification. PLY input
validation, OBJ precision and density/crop are outside stage 01B.

## IGES import (PC2MESH-RELEASE-01B)

Run just the import tests with the same executable environment:

```powershell
& "$repo\pc2mesh_cli\scripts\.venv\Scripts\python.exe" -B -m unittest discover -s "$repo\tests" -p test_iges_import.py -v
```

`test_iges_import.py` writes synthetic ASCII IGES fixtures in temporary
directories. Expected coordinates are specified independently of OCCT. Coverage
includes all five points of each copious-data form, mixed point representations,
duplicate occurrences, grouping, millimeters/inches, rotations, translations,
uniform scale and nested matrices. Decimal integers/fractions, E/D exponents,
default zero fields, large integer-looking coordinates, long numeric parameters,
record wrapping, custom delimiters, Hollerith metadata and benign presentation
warnings exercise the native lexer and supplemental checks.

Error cases include one damaged entity among good ones, `XX`, `3D9999`, malformed
numbers, incomplete coordinates/records, invalid counts/references, matrix cycles,
transformed overflow, empty/missing/corrupt input and unsupported mixed geometry.
Every failure case runs with absent and existing output: exit 1, no success
message, no new PLY, unchanged SHA-256 of the old PLY and input, and no staging
files. The optional `det4.igs` oracle independently reads that known file's fixed
records/116 coordinates/124 matrices in Python and compares all 7871 output
points, not just the count. The user file is only read and its hash is checked.

### Import policy and limits

- Direct point mode supports 116 form 0 and 106 forms 1/2/3. All listed points
  are extracted in entity/parameter order, with duplicates preserved. Form 3's
  vectors are validated but are not exported as positions. Topological transfer
  is never combined with direct extraction.
- Explicit affine Cartesian matrices (124 forms 0/1) and their parent chains
  are composed before unit conversion; cycles, invalid references and non-finite
  results fail. Unused coordinate-system matrices are presentation data.
- Output is in millimeters in IGES model space. Global model-space scale is
  preserved (matching the previous OCCT path), with a diagnostic when not 1;
  there is no implicit rescaling to real-world dimensions. The distinct model/
  real-world interpretation is described in
  [NIST RP 17](https://nvlpubs.nist.gov/nistpubs/Legacy/IR/nistir4600.pdf).
- Recognized service data comprises 124 matrices, 314 colors, 406 properties,
  410 views and their presentation dependencies (108 clipping planes, matrices,
  colors and properties). Groups 402 forms 1/7/14/15 do not create occurrences.
  Unknown significant entities, other copious-data forms, subfigure/instance
  placements (308/408/430), and mixed point/CAD geometry are rejected. Type 116
  display symbols requiring subfigure definitions are currently unsupported.
- CAD-only models retain OCCT's existing topological-vertex extraction. Each
  root must transfer successfully, model/transfer failures are rejected and
  output coordinates must be finite. Nested CAD matrix chains are rejected;
  the direct point path supports them. CAD-only regression covers a line; this
  is not certification of arbitrary solids, trimming or topology fidelity.
- OCCT 7.9.3 performs IGES record/token parsing and typed entity/model checks.
  A native file recognizer retains raw tokens before typed loading. Supplemental
  decimal validation closes confirmed silent-zero/overflow paths, validates
  point-array lengths before allocation, and canonicalizes reals to avoid
  OCCT's integer conversion and short numeric buffer. Fixed 80-column record
  framing checks catch missing point/matrix record delimiters that OCCT accepts
  silently and numeric fields split across physical records (prohibited by
  [IGES free-format rules](https://www.govinfo.gov/content/pkg/GOVPUB-C13-33f9e9f969ac6d3d7a258f73cd8acf19/pdf/GOVPUB-C13-33f9e9f969ac6d3d7a258f73cd8acf19.pdf), section 2.2.2).
  Tokenization and Hollerith interpretation remain with OCCT.
- ASCII numeric syntax is locale-independent. Finite representable `double`
  values are accepted; out-of-range values, including underflow beyond double
  range, fail. Other integer parameters must fit OCCT's signed integer type.
  Original and final coordinate finiteness is checked. PLY's existing nine
  decimal places are unchanged; tests account for that output rounding.
- Model errors and file-recovery diagnostics fail the entire import. Ordinary
  header/presentation warnings alone do not fail it. No PLY is staged until a
  complete nonempty cloud returns to the existing 01A publication mechanism.

Tests establish the listed examples on the installed Windows/OCCT toolchain.
They do not establish arbitrary-file resource bounds, exhaustive malformed-IGES
resilience, support for other OCCT versions or a portable Windows distribution.
