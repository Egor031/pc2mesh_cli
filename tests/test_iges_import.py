"""IGES import oracles independent of OCCT; all generated files are disposable."""

import hashlib
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


EXE = os.environ.get("PC2MESH_TEST_EXE")
FIVE = [(1., 2., 3.), (-2., 4., 6.), (3.25, -6., 9.), (4., 8., -12.), (5., 10., 15.)]
IDENTITY = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0]
ROTATE_TRANSLATE = [0, -1, 0, 10, 1, 0, 0, -20, 0, 0, 1, 30]


def entity(kind, params, form=0, transform=0, status="00000000"):
    return dict(kind=kind, params=list(map(str, params)), form=form, transform=transform, status=status)


def point(xyz, **kwargs):
    return entity(116, [*xyz, 0], **kwargs)


def copious(form, points=FIVE, **kwargs):
    params = [form, len(points)]
    if form == 1:
        params += [points[0][2]]
    for xyz in points:
        params += list(xyz[:2] if form == 1 else xyz)
        if form == 3:
            params += [101, -202, 303]  # vectors must not become points
    return entity(106, params, form=form, **kwargs)


def parameter_records(fields, width, separator, terminator):
    """IGES 2.2.3: numbers and their delimiters cannot cross physical records.

    Hollerith strings may cross them; the length/H prefix must stay together.
    """
    rows, current = [], ""
    for index, field in enumerate(fields):
        segment = field + (terminator if index == len(fields) - 1 else separator)
        prefix = re.match(r"\d+H", field)
        if prefix:
            if width - len(current) < len(prefix.group()):
                rows.append(current.ljust(width))
                current = ""
            while segment:
                length = width - len(current)
                current += segment[:length]
                segment = segment[length:]
                if len(current) == width:
                    rows.append(current)
                    current = ""
        else:
            if len(segment) > width:
                raise ValueError("Numeric fixture field exceeds one physical record")
            if len(current) + len(segment) > width:
                rows.append(current.ljust(width))
                current = ""
            current += segment
    if current:
        rows.append(current.ljust(width))
    return rows


def iges(entities, inches=False, separator=",", terminator=";", scale="1."):
    """Fixed-record IGES fixture writer, not a reader used by the application.

    Expected geometry is specified separately by each test. Numeric fields stay
    on one record; Hollerith data exercises legal cross-record continuation.
    """
    fields = [f"1H{separator}", f"1H{terminator}", "4Htest", "8Htest.igs",
              "4Htest", "4Htest", "32", "308", "15", "308", "15", "4Htest",
              scale, "1" if inches else "2", "2HIN" if inches else "2HMM", "1",
              "0.01", "15H20000101.000000", "0.0001", "", "4HTest", "", "11", "0",
              "15H20000101.000000", ""]
    starts = ["Synthetic IGES import regression".ljust(72) + "S      1"]
    globals_ = [row + f"G{n:7}" for n, row in enumerate(parameter_records(fields, 72, separator, terminator), 1)]
    directories, parameters = [], []
    for index, ent in enumerate(entities):
        de = index * 2 + 1
        chunks = parameter_records([str(ent["kind"]), *ent["params"]], 64, separator, terminator)
        first = [ent["kind"], len(parameters) + 1, 0, ent.get("linefont", 0), 0, 0, ent["transform"], 0]
        directories += ["".join(f"{v:8}" for v in first) + ent["status"] + f"D{de:7}",
                        "".join(f"{v:8}" for v in [ent["kind"], 0, 0, len(chunks), ent["form"]])
                        + " " * 24 + f"{0:8}" + f"D{de + 1:7}"]
        for chunk in chunks:
            parameters.append(chunk.ljust(64) + f"{de:8}P{len(parameters) + 1:7}")
    end = f"S{1:7}G{len(globals_):7}D{len(directories):7}P{len(parameters):7}".ljust(72) + "T      1"
    return ("\n".join(starts + globals_ + directories + parameters + [end]) + "\n").encode("ascii")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ply_points(path):
    lines = path.read_text("ascii").splitlines()
    end = lines.index("end_header")
    count = int(next(line for line in lines[:end] if line.startswith("element vertex ")).split()[-1])
    points = [tuple(map(float, line.split())) for line in lines[end + 1:]]
    if count != len(points) or any(len(p) != 3 or not all(map(math.isfinite, p)) for p in points):
        raise AssertionError("Invalid PLY count, dimensions or finite coordinates")
    return points


@unittest.skipUnless(EXE, "Set PC2MESH_TEST_EXE to a built executable")
class IgesImportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="pc2mesh-01b-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "input data.igs"
        self.output = self.root / "output data.ply"

    def run_cli(self, source=None):
        return subprocess.run([EXE, "extract", str(source or self.source), str(self.output)],
                              cwd=self.root, capture_output=True, timeout=30)

    def success(self, content, expected):
        self.source.write_bytes(content)
        before = digest(self.source)
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        self.assertIn(b"Done.", result.stdout)
        self.assertEqual(digest(self.source), before)
        actual = ply_points(self.output)
        self.assertEqual(len(actual), len(expected))
        for got, want in zip(actual, expected):
            for a, b in zip(got, want):
                self.assertTrue(math.isclose(a, b, abs_tol=5.1e-10, rel_tol=3e-15), (got, want))
        self.assertEqual(list(self.root.glob(".pc2mesh-*")), [])

    def failure(self, content):
        if content is not None:
            self.source.write_bytes(content)
        before = digest(self.source) if self.source.exists() else None
        for old in (False, True):
            with self.subTest(existing_output=old):
                if old:
                    self.output.write_bytes(b"previous PLY sentinel\x00\xff")
                    previous = digest(self.output)
                elif self.output.exists():
                    self.output.unlink()
                result = self.run_cli()
                self.assertEqual(result.returncode, 1, (result.stdout, result.stderr))
                self.assertIn(b"ERROR:", result.stderr)
                self.assertNotIn(b"Done.", result.stdout)
                self.assertNotIn(b"Extracted points:", result.stdout)
                if old:
                    self.assertEqual(digest(self.output), previous)
                else:
                    self.assertFalse(self.output.exists())
                if before:
                    self.assertEqual(digest(self.source), before)
                self.assertEqual(list(self.root.glob(".pc2mesh-*")), [])

    def test_type116_five_points_and_duplicate_occurrences(self):
        self.success(iges([point(p) for p in FIVE + [FIVE[0]]]), FIVE + [FIVE[0]])

    def test_type106_forms_extract_all_five_points(self):
        for points in (FIVE, [(float(i), float(2*i), float(3*i)) for i in range(1, 6)]):
            for form in (1, 2, 3):
                with self.subTest(form=form, points=points):
                    expected = [(x, y, points[0][2]) for x, y, z in points] if form == 1 else points
                    self.success(iges([copious(form, points)]), expected)

    def test_mixed_point_representations_no_double_count(self):
        xy = [(x, y, 7.) for x, y, _ in FIVE]
        self.success(iges([point(FIVE[0]), copious(1, xy), copious(2), copious(3), point(FIVE[0])]),
                     [FIVE[0]] + xy + FIVE + FIVE + [FIVE[0]])

    def test_units_transformations_and_combinations(self):
        for inches in (False, True):
            for form in (0, 1, 2, 3):
                with self.subTest(inches=inches, form=form):
                    data = [point(p, transform=1) for p in FIVE] if form == 0 else [copious(form, transform=1)]
                    local = [(x, y, FIVE[0][2]) for x, y, z in FIVE] if form == 1 else FIVE
                    factor = 25.4 if inches else 1.
                    expected = [((10-y)*factor, (x-20)*factor, (z+30)*factor) for x, y, z in local]
                    self.success(iges([entity(124, ROTATE_TRANSLATE), *data], inches=inches), expected)

    def test_nested_transformations_parent_times_child(self):
        parent = [1, 0, 0, 100, 0, 1, 0, 200, 0, 0, 1, 300]
        self.success(iges([entity(124, parent), entity(124, ROTATE_TRANSLATE, transform=1), point((1, 2, 3), transform=3)]),
                     [(108., 181., 333.)])

    def test_numeric_syntax_delimiters_and_wrapped_parameters(self):
        numeric = [("+1", "-.5", "2D+1"), ("1.d1", ".25e+2", "-3E-1"),
                   ("2147483648", "1." + "0" * 55, ""), (" 2. ", "-0", "+.5D1")]
        expected = [(1., -.5, 20.), (10., 25., -.3), (2147483648., 1., 0.), (2., 0., 5.)]
        for separator, terminator in ((",", ";"), ("^", "!")):
            with self.subTest(separator=separator):
                self.success(iges([point(p) for p in numeric], separator=separator, terminator=terminator), expected)

    def test_group_does_not_duplicate_points(self):
        self.success(iges([point(FIVE[0]), point(FIVE[1]), entity(402, [2, 1, 3], form=7)]), FIVE[:2])

    def test_cad_only_line_preserves_topological_vertices(self):
        self.success(iges([entity(110, [1, 2, 3, -4, -5, -6])]), [(1., 2., 3.), (-4., -5., -6.)])

    def test_units_without_transform_and_uniform_scale(self):
        self.success(iges([point((1, -2, .5))], inches=True), [(25.4, -50.8, 12.7)])
        matrix = [2, 0, 0, 10, 0, 2, 0, 20, 0, 0, 2, 30]
        self.success(iges([entity(124, matrix), copious(2, transform=1)], inches=True),
                     [((2*x+10)*25.4, (2*y+20)*25.4, (2*z+30)*25.4) for x, y, z in FIVE])

    def test_model_space_scale_preserves_model_coordinates(self):
        self.success(iges([point(p) for p in FIVE], scale="0.25"), FIVE)

    def test_properties_hollerith_and_benign_metadata_warning(self):
        name = "Marker,^!;" + "wrapped name " * 8
        decorated = point(FIVE[0])
        decorated["params"] += ["0", "1", "3"]  # no associations, one property
        self.success(iges([decorated, entity(406, [1, f"{len(name)}H{name}"], form=15)],
                          separator="^", terminator="!"), [FIVE[0]])
        warning = point(FIVE[0])
        warning["linefont"] = -3  # wrong presentation type; coordinates unaffected
        self.success(iges([warning, point(FIVE[1])]), FIVE[:2])

    def test_unused_presentation_coordinate_system_is_not_geometry(self):
        self.success(iges([entity(124, IDENTITY, form=11),
                           entity(124, IDENTITY),
                           entity(410, [1, 1, 0, 0, 0, 0, 0, 0], transform=3, status="00000100"),
                           point(FIVE[0])]), [FIVE[0]])
        # OCCT's View checker requires form 0, so using the cylindrical matrix
        # here is invalid presentation data, not a benign warning to suppress.
        self.failure(iges([entity(124, IDENTITY, form=11),
                           entity(410, [1, 1, 0, 0, 0, 0, 0, 0], transform=1, status="00000100"),
                           point(FIVE[0])]))
        self.failure(iges([entity(124, IDENTITY, form=11), point(FIVE[0], transform=1)]))

    def test_cad_only_numeric_corruption_is_not_silently_zero(self):
        for token in ("XX", "3D9999", "999999999999999999999"):
            with self.subTest(token=token):
                self.failure(iges([entity(110, [token, 0, 0, 1, 1, 1])]))

    def test_repeated_success_replaces_output(self):
        self.success(iges([point(FIVE[0])]), [FIVE[0]])
        self.success(iges([copious(2)]), FIVE)

    def test_bad_entity_cannot_publish_partial_result(self):
        self.failure(iges([point(FIVE[0]), point(["XX", 2, 3]), point(FIVE[1])]))

    def test_invalid_numeric_literals_in_points_and_vectors(self):
        for token in ("XX", "3D9999", "NaN", "inf", "0x1p2", "1E+", "1.2.3", "1 2", "3D-9999"):
            for form in (0, 1, 2, 3):
                with self.subTest(token=token, form=form):
                    bad = point([token, 2, 3]) if form == 0 else copious(form)
                    if form:
                        bad["params"][-1] = token  # includes vectors of form 3
                    self.failure(iges([point(FIVE[0]), bad]))

    def test_missing_coordinates_and_extra_parameters(self):
        for params in ([1], [1, 2], [1, 2, 3, 0, 0, 0, 999]):
            with self.subTest(params=params):
                self.failure(iges([point(FIVE[0]), entity(116, params)]))

    def test_invalid_copious_counts_and_interpretation(self):
        for form in (1, 2, 3):
            for count in ("-1", "0", "6", "2147483647", "99999999999999999", "XX", "1.5", "4"):
                with self.subTest(form=form, count=count):
                    bad = copious(form)
                    bad["params"][1] = count
                    self.failure(iges([point(FIVE[0]), bad]))
            bad = copious(form)
            bad["params"][0] = "0"
            self.failure(iges([bad]))

    def test_bad_transform_and_transformed_overflow(self):
        for token in ("XX", "3D9999"):
            matrix = list(IDENTITY)
            matrix[3] = token
            self.failure(iges([entity(124, matrix), point(FIVE[0], transform=1)]))
        self.failure(iges([point(("1D308", 0, 0))], inches=True))
        huge = list(IDENTITY)
        huge[3] = "1D308"
        self.failure(iges([entity(124, huge), point(("1D308", 0, 0), transform=1)]))

    def test_invalid_transform_references_and_cycles(self):
        for pointer in (2, 99, -1, 3):
            with self.subTest(pointer=pointer):
                self.failure(iges([point(FIVE[0], transform=pointer), point(FIVE[1])]))
        self.failure(iges([entity(124, IDENTITY, transform=1), point(FIVE[0], transform=1)]))
        self.failure(iges([entity(124, IDENTITY, transform=3), entity(124, IDENTITY, transform=1), point(FIVE[0], transform=1)]))

    def test_mixed_geometry_and_unknown_entities_rejected(self):
        for extra in (entity(110, [0, 0, 0, 1, 1, 1]), entity(999, [1, 2, 3]),
                      entity(408, [1, 0, 0, 0, 1])):
            with self.subTest(kind=extra["kind"]):
                self.failure(iges([point(FIVE[0]), extra]))
        self.failure(iges([copious(2) | {"form": 12}]))

    def test_empty_missing_and_corrupt_file(self):
        self.failure(None)
        for content in (b"", b"not an IGES file\n", iges([])):
            with self.subTest(content=content[:20]):
                self.failure(content)

    def test_truncated_records_and_missing_sections(self):
        original = iges([point(FIVE[0]), copious(3)])
        lines = original.splitlines(keepends=True)
        for damaged in (b"".join(lines[:-2]), b"".join(lines[:-1]),
                        b"".join(lines[:-2]) + lines[-2][:20],
                        original.replace(b"303;", b"303 ")):
            with self.subTest(tail=damaged[-90:]):
                self.failure(damaged)

    def test_physical_record_line_endings(self):
        content = iges([copious(3)])
        self.success(content.replace(b"\n", b"\r\n"), FIVE)
        self.success(content.replace(b"\n", b"\r"), FIVE)
        self.success(content.rstrip(b"\n"), FIVE)

    def test_split_numeric_field_is_rejected_not_truncated(self):
        # Deliberately bypass the conforming fixture writer to split the number
        # 0.125 at column 64. OCCT alone accepts only its prefix as zero.
        rows = iges([point((0.125, 2, 3))]).splitlines()
        index = next(i for i, row in enumerate(rows) if row[72:73] == b"P")
        rows[index:index+1] = [(b"116," + b" " * 58 + b"0.") + b"       1P      1",
                             b"125,2,3,0;".ljust(64) + b"       1P      2"]
        rows[index-1] = rows[index-1][:24] + b"       2" + rows[index-1][32:]
        rows[-1] = rows[-1][:25] + b"      2" + rows[-1][32:]
        self.failure(b"\n".join(rows) + b"\n")

    def test_real_det4_independent_raw_point_oracle(self):
        filename = os.environ.get("PC2MESH_TEST_IGES")
        if not filename:
            self.skipTest("Set PC2MESH_TEST_IGES to optional det4.igs (not stored in repository)")
        source = Path(filename)
        before = digest(source)
        records = source.read_bytes().splitlines()
        # This oracle intentionally handles the known det4 dialect only. It is
        # independent of OCCT and never serves as the production IGES parser.
        dirs = {int(r[73:80]): r[:72] for r in records if r[72:73] == b"D"}
        params = {}
        for row in records:
            if row[72:73] == b"P":
                params.setdefault(int(row[64:72]), b"")
                params[int(row[64:72])] += row[:64]
        self.assertIn(b"1.0,2,2HMM", b"".join(r[:72] for r in records if r[72:73] == b"G"))
        matrices, expected = {}, []
        for de, row in dirs.items():
            if de % 2 == 1 and int(row[:8]) == 124:
                self.assertEqual(int(row[48:56] or b"0"), 0)
                matrices[de] = list(map(float, params[de].split(b";")[0].split(b",")[1:13]))
        for de, row in dirs.items():
            if de % 2 != 1 or int(row[:8]) != 116:
                continue
            xyz = list(map(float, params[de].split(b";")[0].split(b",")[1:4]))
            matrix = matrices[int(row[48:56])] if row[48:56].strip() and int(row[48:56]) else IDENTITY
            expected.append(tuple(sum(matrix[r*4+c] * xyz[c] for c in range(3)) + matrix[r*4+3] for r in range(3)))
        self.assertEqual(len(expected), 7871)
        result = self.run_cli(source)
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        actual = ply_points(self.output)
        self.assertEqual(len(actual), 7871)
        maximum = max(abs(a-b) for got, want in zip(actual, expected) for a, b in zip(got, want))
        self.assertLessEqual(maximum, 5.1e-10)
        self.assertEqual(digest(source), before)
        print(f"det4: 7871 points, maximum absolute coordinate error {maximum:.3g} mm, SHA-256 unchanged")


if __name__ == "__main__":
    unittest.main()
