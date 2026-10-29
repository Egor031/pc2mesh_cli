"""Independent OBJ text reader used only by regression tests (no Open3D reader)."""

import numpy as np


def read_obj(text):
    vertices, colors, normals, faces, normal_faces = [], [], [], [], []
    for line in text.splitlines():
        fields = line.split()
        if not fields or fields[0].startswith('#'):
            continue
        if fields[0] == 'v':
            if len(fields) not in (4, 7):
                raise AssertionError('Invalid OBJ vertex record')
            vertices.append(list(map(float, fields[1:4])))
            if len(fields) == 7:
                colors.append(list(map(float, fields[4:])))
        elif fields[0] == 'vn':
            if len(fields) != 4:
                raise AssertionError('Invalid OBJ normal record')
            normals.append(list(map(float, fields[1:])))
        elif fields[0] == 'f':
            if len(fields) != 4:
                raise AssertionError('Expected triangular OBJ face')
            face, normal_face = [], []
            for reference in fields[1:]:
                indices = reference.split('//')
                face.append(int(indices[0]) - 1)
                if len(indices) == 2:
                    normal_face.append(int(indices[1]) - 1)
                elif len(indices) != 1:
                    raise AssertionError('Invalid OBJ reference')
            faces.append(face)
            if normal_face:
                normal_faces.append(normal_face)
        else:
            raise AssertionError(f'Unexpected OBJ statement {fields[0]}')
    result = dict(vertices=np.asarray(vertices, dtype=np.float64).reshape(-1, 3),
                  normals=np.asarray(normals, dtype=np.float64).reshape(-1, 3),
                  colors=np.asarray(colors, dtype=np.float64).reshape(-1, 3),
                  triangles=np.asarray(faces, dtype=np.int64).reshape(-1, 3),
                  normal_indices=np.asarray(normal_faces, dtype=np.int64).reshape(-1, 3))
    for key, count in (('triangles', len(vertices)), ('normal_indices', len(normals))):
        if result[key].size and (result[key].min() < 0 or result[key].max() >= count):
            raise AssertionError('OBJ index outside array')
    return result


def geometry(vertices, triangles):
    points = vertices[triangles]
    cross = np.cross(points[:, 1] - points[:, 0], points[:, 2] - points[:, 0])
    return dict(vertices=len(vertices), unique_vertices=len(np.unique(vertices, axis=0)),
                triangles=len(triangles), degenerate_triangles=int(np.all(cross == 0, axis=1).sum()))


def assert_exact(test, expected, actual):
    # Compare binary64 bits, including signed zero; no scale-dependent epsilon.
    test.assertEqual(expected.shape, actual.shape)
    np.testing.assert_array_equal(expected.view(np.uint64), actual.view(np.uint64))
