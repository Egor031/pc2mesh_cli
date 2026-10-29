# File safety regression tests (PC2MESH-RELEASE-01A)

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
tested local Windows filesystem require separate verification. The IGES type 106,
damaged-entity/PLY validation, OBJ precision and density/crop findings belong to
later stages and are deliberately unchanged here.
