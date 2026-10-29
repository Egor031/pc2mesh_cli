import contextlib
import hashlib
import importlib.util
import io
import json
import locale
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

import numpy as np
import open3d as o3d

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / 'pc2mesh_cli' / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import reconstruct_poisson as app
from file_safety import AtomicOutput, publish_outputs
from ply_validation import validate_ply
from obj_oracle import assert_exact, geometry, read_obj
from test_iges_import import copious, iges


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sphere(count=1200, center=(0, 0, 0)):
    i = np.arange(count)
    z = 1 - 2 * (i + .5) / count
    angle = i * np.pi * (3 - np.sqrt(5))
    radius = np.sqrt(1 - z*z)
    return np.column_stack((radius*np.cos(angle), radius*np.sin(angle), z)) + center


def ascii_ply(points, count=None, types=('double', 'double', 'double')):
    header = ('ply\nformat ascii 1.0\nelement vertex ' + str(len(points) if count is None else count)
              + '\n' + ''.join(f'property {kind} {name}\n' for kind, name in zip(types, ('x', 'y', 'z')))
              + 'end_header\n')
    return header.encode() + b''.join((' '.join(map(str, point)) + '\n').encode() for point in points)


class TemporaryCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='pc2mesh-01c-')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / 'input cloud.ply'
        self.output = self.root / 'result mesh.obj'

    def validate(self, content):
        self.source.write_bytes(content)
        with self.source.open('rb') as stream:
            return validate_ply(stream)

    def main(self, **kwargs):
        arguments = ['reconstruct_poisson.py', str(self.source), str(self.output)]
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, 'argv', arguments), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = app.main()
        return code, out.getvalue(), err.getvalue()

    def no_staging(self):
        self.assertEqual(list(self.root.rglob('.pc2mesh-*')), [])


class PlyValidationTests(TemporaryCase):
    def test_cpp_ply_and_type_variants(self):
        points = [[1, -2, 3], [-4, 5, -6]]
        for types in (('double',)*3, ('float',)*3, ('float64', 'int16', 'float32')):
            with self.subTest(types=types):
                self.assertEqual(self.validate(ascii_ply(points, types=types)), 2)
                actual = np.asarray(o3d.io.read_point_cloud(str(self.source)).points)
                np.testing.assert_array_equal(actual, points)
        self.assertEqual(self.validate(ascii_ply(points).replace(b'\n', b'\r\n')), 2)
        self.assertEqual(self.validate(ascii_ply(points).replace(b'\n', b'\r')), 2)
        self.assertEqual(self.validate(ascii_ply(points).rstrip(b'\n')), 2)
        header = ascii_ply([[10, 20, 30], [40, 50, 60]], types=('uchar',)*3).split(b'end_header\n')[0] + b'end_header\n'
        for ending in (b'\n', b'\r\n', b'\r'):
            # First binary byte is LF: it belongs to x, not the header ending.
            data = header.replace(b'ascii', b'binary_little_endian').replace(b'\n', ending)
            self.assertEqual(self.validate(data + bytes([10, 20, 30, 40, 50, 60])), 2)
            np.testing.assert_array_equal(np.asarray(o3d.io.read_point_cloud(str(self.source)).points),
                                          [[10, 20, 30], [40, 50, 60]])

    def test_ascii_binary_properties_and_extra_elements(self):
        expected = [[.125, -1.5, 2.25], [-4, 5.5, 6.75], [7.25, 8, -9]]
        for encoding, endian in (('ascii', ''), ('binary_little_endian', '<'), ('binary_big_endian', '>')):
            with self.subTest(encoding=encoding):
                header = (f'ply\nformat {encoding} 1.0\ncomment UTF-8 metadata: точка\nobj_info extra data\n'
                          'element unused 0\nelement edge 1\nproperty int vertex1\nproperty int vertex2\n'
                          'element vertex 3\nproperty ushort id\nproperty double y\nproperty float x\nproperty double z\n'
                          'property float nx\nproperty float ny\nproperty float nz\n'
                          'property uchar green\nproperty uchar red\nproperty uchar blue\n'
                          'element face 1\nproperty list uchar int vertex_indices\nproperty float quality\n'
                          'element sensor 1\nproperty list ushort float samples\nend_header\n').encode('utf-8')
                rows = [[i, y, x, z, 0, 0, 1, 128, 255, 0] for i, (x, y, z) in enumerate(expected)]
                if endian:
                    body = struct.pack(endian+'ii', 0, 1)
                    body += b''.join(struct.pack(endian+'HdfdfffBBB', *row) for row in rows)
                    body += struct.pack(endian+'Biiif', 3, 0, 1, 2, .5)
                    body += struct.pack(endian+'Hff', 2, .25, -.5)
                else:
                    body = b'0 1\n' + b''.join((' '.join(map(str, row))+'\n').encode() for row in rows)
                    body += b'3 0 1 2 .5\n2 .25 -.5\n \t\n'
                self.assertEqual(self.validate(header + body), 3)
                cloud = o3d.io.read_point_cloud(str(self.source))
                np.testing.assert_array_equal(np.asarray(cloud.points), expected)
                np.testing.assert_array_equal(np.asarray(cloud.normals), [[0, 0, 1]]*3)
                np.testing.assert_array_equal(np.asarray(cloud.colors), [[1, 128/255, 0]]*3)

    def bad_contents(self):
        good = ascii_ply([[1, 2, 3], [4, 5, 6]])
        yield '1205 declared, 1200 present', ascii_ply(sphere().tolist(), count=1205)
        for label, content in (
            ('empty', b''), ('magic', good.replace(b'ply\n', b'PLY\n', 1)),
            ('missing end_header', good.replace(b'end_header\n', b'')),
            ('unsupported version', good.replace(b'ascii 1.0', b'ascii 2.0')),
            ('unsupported format', good.replace(b'ascii 1.0', b'binary_unknown 1.0')),
            ('negative count', good.replace(b'vertex 2', b'vertex -1')),
            ('enormous count', good.replace(b'vertex 2', b'vertex 2147483647')),
            ('zero count', good.replace(b'vertex 2', b'vertex 0')),
            ('duplicate vertex', good.replace(b'end_header', b'element vertex 0\nproperty float x\nend_header')),
            ('duplicate property', good.replace(b'property double x', b'property double x\nproperty float x')),
            ('NUL property', good.replace(b'property double x', b'property double x\x00alias\nproperty double x')),
            ('missing z', good.replace(b'property double z\n', b'')),
            ('bad type', good.replace(b'property double x', b'property float128 x')),
            ('z before y', good.replace(b'property double y\nproperty double z', b'property double z\nproperty double y')),
            ('partial normals', good.replace(b'end_header', b'property float nx\nend_header')),
            ('list coordinate', good.replace(b'property double x', b'property list uchar double x')),
            ('list count float', good.replace(b'property double x', b'property list float double quality')),
            ('missing body row', good.rsplit(b'4 5 6\n', 1)[0]),
            ('shifted record fields', good.replace(b'1 2 3\n4 5 6\n', b'1 2\n3 4 5 6\n')),
            ('extra body data', good + b'7 8 9\n'),
            ('invalid real', good.replace(b'1 2 3', b'XX 2 3')),
            ('NaN', good.replace(b'1 2 3', b'nan 2 3')),
            ('Inf', good.replace(b'1 2 3', b'Inf 2 3')),
            ('overflow', good.replace(b'1 2 3', b'1e9999 2 3')),
            ('underflow', good.replace(b'1 2 3', b'1e-9999 2 3')),
            ('float overflow', ascii_ply([['3.5e38', 2, 3]], types=('float',)*3)),
            ('invalid integer', ascii_ply([['1.5', 2, 3]], types=('int',)*3)),
            ('integer overflow', ascii_ply([[256, 2, 3]], types=('uchar',)*3)),
        ):
            yield label, content
        for encoding, endian in (('binary_little_endian', '<'), ('binary_big_endian', '>')):
            header = good[:good.index(b'end_header')+len(b'end_header\n')].replace(b'ascii', encoding.encode())
            for label, data in (('truncated binary', struct.pack(endian+'ddddd', 1, 2, 3, 4, 5)),
                                ('NaN binary', struct.pack(endian+'dddddd', 1, 2, 3, 4, float('nan'), 6)),
                                ('Inf binary', struct.pack(endian+'dddddd', 1, 2, 3, 4, float('inf'), 6)),
                                ('binary extra byte', struct.pack(endian+'dddddd', 1, 2, 3, 4, 5, 6)+b'x')):
                yield encoding+' '+label, header+data
        face = good.replace(b'end_header', b'element face 1\nproperty list uchar int vertex_indices\nend_header')
        for data in (b'3 0 1\n', b'-1\n', b'3 0 1 2\n', b'255\n'):
            yield 'bad face list '+str(data), face+data
        binary = (b'ply\nformat binary_little_endian 1.0\nelement vertex 2\nproperty double x\nproperty double y\nproperty double z\n'
                  b'element samples 1\nproperty list int float values\nend_header\n')
        for data in (struct.pack('<i', -1), struct.pack('<iff', 3, 1, 2),
                     struct.pack('<if', 1, float('nan')), struct.pack('<i', 2**31-1)):
            yield 'invalid binary list '+str(data), binary+struct.pack('<dddddd', 1, 2, 3, 4, 5, 6)+data

    def test_bad_ply_never_reaches_native_reader_or_changes_result(self):
        for label, content in self.bad_contents():
            for existed in (False, True):
                with self.subTest(case=label, existing=existed):
                    self.source.write_bytes(content)
                    source_hash = sha(self.source)
                    if existed:
                        self.output.write_bytes(b'previous OBJ\x00\xff')
                        previous = sha(self.output)
                    else:
                        self.output.unlink(missing_ok=True)
                    with patch.object(o3d.io, 'read_point_cloud', side_effect=AssertionError('native reader called')) as reader:
                        code, out, err = self.main()
                    reader.assert_not_called()
                    self.assertEqual(code, 1, (out, err))
                    self.assertIn('PLY', err)
                    self.assertNotIn('Status: OK', out)
                    self.assertEqual(sha(self.source), source_hash)
                    if existed:
                        self.assertEqual(sha(self.output), previous)
                    else:
                        self.assertFalse(self.output.exists())
                    self.no_staging()

    def test_native_decoder_count_and_finiteness_are_also_checked(self):
        self.source.write_bytes(ascii_ply([[1, 2, 3], [4, 5, 6]]))
        self.output.write_bytes(b'old OBJ')
        before = sha(self.output)
        for points in ([[1, 2, 3]], [[1, 2, 3], [float('nan'), 5, 6]]):
            with patch.object(o3d.io, 'read_point_cloud', return_value=o3d.geometry.PointCloud(o3d.utility.Vector3dVector(points))):
                code, out, err = self.main()
            self.assertEqual(code, 1)
            self.assertIn('Open3D', err)
            self.assertEqual(sha(self.output), before)

    def test_actual_cli_truncated_ply_exit_code(self):
        self.source.write_bytes(ascii_ply(sphere().tolist(), count=1205))
        self.output.write_bytes(b'old OBJ')
        before = sha(self.output)
        result = subprocess.run([sys.executable, '-B', str(SCRIPTS/'reconstruct_poisson.py'),
                                 str(self.source), str(self.output)], cwd=self.root, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(b'PLY', result.stderr)
        self.assertNotIn(b'Status: OK', result.stdout)
        self.assertEqual(sha(self.output), before)
        self.no_staging()

    def test_binary_validation_memory_is_bounded(self):
        count = 1_000_000
        header = f'ply\nformat binary_little_endian 1.0\nelement vertex {count}\nproperty double x\nproperty double y\nproperty double z\nend_header\n'
        block = struct.pack('<ddd', 1, 2, 3) * 1000
        with self.source.open('wb') as stream:
            stream.write(header.encode())
            for _ in range(count//1000):
                stream.write(block)
        tracemalloc.start()
        try:
            with self.source.open('rb') as stream:
                self.assertEqual(validate_ply(stream), count)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 2*1024*1024, peak)
        print(f'PLY preflight: {count} binary vertices, peak traced memory {peak} bytes')

    def test_long_ascii_list_record_is_streamed(self):
        count = 100_000
        header = (b'ply\nformat ascii 1.0\nelement vertex 1\nproperty double x\nproperty double y\nproperty double z\n'
                  b'element samples 1\nproperty list uint uchar values\nend_header\n1 2 3\n')
        self.assertEqual(self.validate(header + str(count).encode() + b' ' + b'1 '*count + b'\n'), 1)


class ObjWriterTests(TemporaryCase):
    def mesh(self):
        vertices = np.array([[1, 2, 3], [np.nextafter(1., 2.), 2, 3],
                             [np.nextafter(1., 0.), 2, 3],
                             [1e-200, -1e200, -0.], [np.finfo(float).max, np.nextafter(0., 1.), -np.nextafter(0., 1.)]])
        mesh = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(vertices),
                                        o3d.utility.Vector3iVector([[0, 1, 3], [1, 2, 4], [0, 0, 2]]))
        mesh.vertex_normals = o3d.utility.Vector3dVector(np.tile([.12345678901234567, -.5, -0.], (len(vertices), 1)))
        mesh.vertex_colors = o3d.utility.Vector3dVector(np.tile([.1, .2, .3], (len(vertices), 1)))
        return mesh

    def test_binary64_roundtrip_coordinates_normals_colors_indices(self):
        mesh = self.mesh()
        stream = io.StringIO()
        app.write_obj(stream, mesh, 'double roundtrip')
        result = read_obj(stream.getvalue())
        for name, values in (('vertices', mesh.vertices), ('normals', mesh.vertex_normals), ('colors', mesh.vertex_colors)):
            assert_exact(self, np.asarray(values), result[name])
        np.testing.assert_array_equal(np.asarray(mesh.triangles), result['triangles'])
        np.testing.assert_array_equal(result['triangles'], result['normal_indices'])
        self.assertEqual(len(np.unique(result['vertices'], axis=0)), len(mesh.vertices))
        # Existing repeated-index/collinear triangles are retained, not repaired.
        self.assertEqual(result['triangles'][-1].tolist(), [0, 0, 2])

    def test_optional_attributes_and_source_mesh_unchanged(self):
        mesh = self.mesh()
        mesh.vertex_normals = o3d.utility.Vector3dVector()
        mesh.vertex_colors = o3d.utility.Vector3dVector()
        before = np.asarray(mesh.vertices).copy()
        output = io.StringIO()
        app.write_obj(output, mesh, 'bare')
        result = read_obj(output.getvalue())
        self.assertEqual(len(result['normals']), 0)
        self.assertEqual(len(result['colors']), 0)
        assert_exact(self, before, np.asarray(mesh.vertices))
        assert_exact(self, before, result['vertices'])

    def test_invalid_mesh_preserves_previous_file(self):
        mutations = [lambda m: np.asarray(m.vertices).__setitem__((0, 0), float('nan')),
                     lambda m: np.asarray(m.vertices).__setitem__((0, 1), float('inf')),
                     lambda m: np.asarray(m.vertex_normals).__setitem__((0, 0), float('nan')),
                     lambda m: np.asarray(m.vertex_colors).__setitem__((0, 0), float('inf')),
                     lambda m: np.asarray(m.triangles).__setitem__((0, 0), -1),
                     lambda m: np.asarray(m.triangles).__setitem__((0, 0), 99),
                     lambda m: setattr(m, 'vertex_normals', o3d.utility.Vector3dVector([[0, 0, 1]]))]
        for mutate in mutations:
            mesh = self.mesh()
            mutate(mesh)
            self.output.write_bytes(b'old OBJ')
            before = sha(self.output)
            with self.assertRaises(ValueError), AtomicOutput(self.output) as staged:
                app.write_obj(staged.stream, mesh, 'invalid')
                publish_outputs([staged], lambda: None)
            self.assertEqual(sha(self.output), before)
            self.no_staging()

    def test_short_write_is_detected(self):
        class ShortStream(io.StringIO):
            def write(self, text):
                return super().write(text[:-1])
        with self.assertRaisesRegex(OSError, 'Short OBJ'):
            app.write_obj(ShortStream(), self.mesh(), 'short')

    def test_decimal_output_is_independent_of_numeric_locale(self):
        previous = locale.setlocale(locale.LC_NUMERIC)
        try:
            for name in ('Russian_Russia.1251', 'German_Germany.1252', 'de_DE.UTF-8'):
                try:
                    locale.setlocale(locale.LC_NUMERIC, name)
                    break
                except locale.Error:
                    continue
            else:
                self.skipTest('No comma-decimal numeric locale is installed')
            self.assertEqual(locale.localeconv()['decimal_point'], ',')
            mesh = self.mesh()
            output = io.StringIO()
            app.write_obj(output, mesh, 'locale')
            assert_exact(self, np.asarray(mesh.vertices), read_obj(output.getvalue())['vertices'])
        finally:
            locale.setlocale(locale.LC_NUMERIC, previous)


class GeometryIntegrationTests(TemporaryCase):
    def snapshot(self, mesh):
        before = dict(vertices=np.asarray(mesh.vertices).copy(),
                      triangles=np.asarray(mesh.triangles).copy(),
                      normals=np.asarray(mesh.vertex_normals).copy())
        baseline_path = os.environ.get('PC2MESH_BASELINE_SCRIPT')
        if baseline_path:
            spec = importlib.util.spec_from_file_location('pc2mesh_01b_baseline', baseline_path)
            baseline = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(baseline)
            old_text = io.StringIO()
            baseline.write_obj(old_text, mesh, 'baseline')
            before['old'] = read_obj(old_text.getvalue())
        return before

    def reconstruct_and_compare(self, label):
        original_writer = app.write_obj
        before = {}
        def capture(stream, mesh, name):
            before.update(self.snapshot(mesh))
            original_writer(stream, mesh, name)

        self.output.write_bytes(b'old OBJ')
        source_hash = sha(self.source)
        with patch.object(app, 'write_obj', capture):
            code, out, err = self.main()
        self.assertEqual(code, 0, (out, err))
        self.assertIn('Status: OK', out)
        self.assertTrue(before)
        self.compare_geometry(before, label)
        self.assertEqual(sha(self.source), source_hash)
        self.no_staging()

    def compare_geometry(self, before, label):
        actual = read_obj(self.output.read_text())
        for name in ('vertices', 'normals'):
            assert_exact(self, before[name], actual[name])
        np.testing.assert_array_equal(before['triangles'], actual['triangles'])
        pre = geometry(before['vertices'], before['triangles'])
        post = geometry(actual['vertices'], actual['triangles'])
        self.assertEqual(pre, post)
        stats = dict(case=label, before=pre, after=post, max_coordinate_error=0.0)
        if 'old' in before:
            old = before['old']
            stats['six_digits'] = geometry(old['vertices'], old['triangles'])
            stats['six_digits_max_error'] = float(np.max(np.abs(old['vertices']-before['vertices'])))
        native = o3d.io.read_triangle_mesh(str(self.output))
        native_vertices, native_triangles = np.asarray(native.vertices), np.asarray(native.triangles)
        stats['open3d_reader'] = geometry(native_vertices, native_triangles)
        stats['open3d_reader_max_coordinate_error'] = (
            float(np.max(np.abs(native_vertices-before['vertices'])))
            if native_vertices.shape == before['vertices'].shape and np.array_equal(native_triangles, before['triangles']) else None)
        print('OBJ geometry:', json.dumps(stats, sort_keys=True))
        report = os.environ.get('PC2MESH_GEOMETRY_REPORT')
        if report:
            path = Path(report)
            reports = json.loads(path.read_text()) if path.exists() else []
            reports.append(stats)
            path.write_text(json.dumps(reports, indent=2), encoding='utf-8')
        self.no_staging()
        return before, actual

    def test_offset_unit_sphere_serialization(self):
        # A known sphere mesh isolates serialization from Qhull's existing
        # failure on cospherical point clouds at large coordinate offsets.
        mesh = o3d.geometry.TriangleMesh.create_sphere(radius=1, resolution=46)
        mesh.translate((1000, 2000, 3000))
        mesh.compute_vertex_normals()
        before = self.snapshot(mesh)
        with AtomicOutput(self.output) as staged:
            app.write_obj(staged.stream, mesh, 'offset sphere')
            publish_outputs([staged], lambda: None)
        self.compare_geometry(before, 'offset_unit_sphere')

    @unittest.skipUnless(os.environ.get('PC2MESH_TEST_EXE'), 'Set PC2MESH_TEST_EXE')
    def test_complete_iges_ply_obj_unit_sphere(self):
        points = sphere()
        source = self.root/'sphere.igs'
        source.write_bytes(iges([copious(2, points)]))
        before = sha(source)
        run = subprocess.run([os.environ['PC2MESH_TEST_EXE'], 'extract', str(source), str(self.source)],
                             capture_output=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        with self.source.open('rb') as stream:
            self.assertEqual(validate_ply(stream), 1200)
        self.reconstruct_and_compare('iges_ply_obj_unit_sphere')
        self.assertEqual(sha(source), before)

    @unittest.skipUnless(os.environ.get('PC2MESH_TEST_IGES') and os.environ.get('PC2MESH_TEST_EXE'), 'Set optional det4 IGES/executable paths')
    def test_real_det4_before_after_obj(self):
        source = Path(os.environ['PC2MESH_TEST_IGES'])
        before = sha(source)
        run = subprocess.run([os.environ['PC2MESH_TEST_EXE'], 'extract', str(source), str(self.source)],
                             capture_output=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        with self.source.open('rb') as stream:
            self.assertEqual(validate_ply(stream), 7871)
        self.reconstruct_and_compare('det4')
        self.assertEqual(sha(source), before)


if __name__ == '__main__':
    unittest.main()
