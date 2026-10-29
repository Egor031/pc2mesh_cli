import sys
import io
from contextlib import ExitStack
from pathlib import Path
from datetime import datetime

import numpy as np
import open3d as o3d

from file_safety import AtomicOutput, InputFileLock, ensure_distinct_paths, publish_outputs


def write_obj(stream, mesh, object_name):
    # Preserve the current Open3D OBJ layout/precision; precision is a separate change.
    stream.write("# Created by Open3D \n")
    stream.write(f"# object name: {object_name}\n")
    stream.write(f"# number of vertices: {len(mesh.vertices)}\n")
    stream.write(f"# number of triangles: {len(mesh.triangles)}\n")
    normals = np.asarray(mesh.vertex_normals)
    colors = np.asarray(mesh.vertex_colors)
    has_normals = mesh.has_vertex_normals()
    has_colors = mesh.has_vertex_colors()
    for index, vertex in enumerate(np.asarray(mesh.vertices)):
        values = list(vertex)
        if has_colors:
            values.extend(colors[index])
        stream.write("v " + " ".join(format(float(value), ".6g") for value in values) + "\n")
        if has_normals:
            stream.write("vn " + " ".join(format(float(value), ".6g") for value in normals[index]) + "\n")
    for triangle in np.asarray(mesh.triangles):
        indices = [int(index) + 1 for index in triangle]
        face = [f"{index}//{index}" if has_normals else str(index) for index in indices]
        stream.write("f " + " ".join(face) + "\n")


def log_write(log_file, text):
    print(text)
    if log_file is not None:
        log_file.write(text + "\n")
        log_file.flush()


def estimate_average_spacing(cloud):
    distances = cloud.compute_nearest_neighbor_distance()

    if len(distances) == 0:
        return 1.0

    distances = np.asarray(distances)
    distances = distances[distances > 0]

    if len(distances) == 0:
        return 1.0

    return float(np.median(distances))


def main():
    if len(sys.argv) < 3:
        print("Usage:")
        print("  python reconstruct_poisson.py input.ply output.obj [log.txt]")
        return 1

    input_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2])
    log_path = Path(sys.argv[3]) if len(sys.argv) >= 4 else None

    paths = {"input": input_path, "output": output_path, "log": log_path}
    log_file = io.StringIO() if log_path is not None else None
    files = ExitStack()
    input_lock = None
    paths_checked = False
    log_failed = False

    def validate():
        ensure_distinct_paths(paths)
        if input_lock is not None:
            input_lock.check()

    notes = ("\nStatus: OK\n\nNotes:\n"
             "- Result is a triangle mesh, not a parametric NX model.\n"
             "- Missing scan areas cannot be restored reliably.\n"
             "- Some real holes may be closed by Poisson reconstruction.\n"
             "- Foreign scanned objects may appear in the output mesh.\n")

    try:
        validate()
        paths_checked = True
        if output_path.suffix.lower() != ".obj":
            raise ValueError("Output must be an OBJ file (.obj).")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)

        log_write(log_file, "PC2Mesh Poisson reconstruction")
        log_write(log_file, f"Time: {datetime.now()}")
        log_write(log_file, f"Input: {input_path}")
        log_write(log_file, f"Output: {output_path}")
        log_write(log_file, "")

        if not input_path.exists():
            raise RuntimeError(f"Input file does not exist: {input_path}")

        input_lock = files.enter_context(InputFileLock(input_path))
        input_lock.check()
        log_write(log_file, "Reading point cloud...")
        cloud = o3d.io.read_point_cloud(str(input_path))
        input_lock.check()

        if cloud.is_empty():
            raise RuntimeError("Point cloud is empty or could not be read.")

        log_write(log_file, f"Raw points: {len(cloud.points)}")

        log_write(log_file, "Removing duplicated points...")
        cloud.remove_duplicated_points()

        log_write(log_file, "Removing non-finite points...")
        cloud.remove_non_finite_points()

        log_write(log_file, "Removing statistical outliers...")
        cloud, _ = cloud.remove_statistical_outlier(
            nb_neighbors=20,
            std_ratio=2.0
        )

        log_write(log_file, f"Points after cleanup: {len(cloud.points)}")

        spacing = estimate_average_spacing(cloud)
        normal_radius = spacing * 5.0

        log_write(log_file, f"Estimated point spacing: {spacing:.6f}")
        log_write(log_file, f"Normal search radius: {normal_radius:.6f}")

        log_write(log_file, "Estimating normals...")
        cloud.estimate_normals(
            search_param=o3d.geometry.KDTreeSearchParamHybrid(
                radius=normal_radius,
                max_nn=30
            )
        )

        log_write(log_file, "Orienting normals...")
        cloud.orient_normals_consistent_tangent_plane(30)

        depth = 9
        log_write(log_file, f"Running Poisson reconstruction, depth={depth}...")
        mesh, densities = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(
            cloud,
            depth=depth
        )

        log_write(log_file, f"Poisson vertices: {len(mesh.vertices)}")
        log_write(log_file, f"Poisson triangles: {len(mesh.triangles)}")

        log_write(log_file, "Cropping mesh by point cloud bounding box...")
        bbox = cloud.get_axis_aligned_bounding_box()
        bbox = bbox.scale(1.05, bbox.get_center())
        mesh = mesh.crop(bbox)

        log_write(log_file, "Removing low-density vertices...")
        densities = np.asarray(densities)

        if len(densities) == len(mesh.vertices):
            density_quantile = 0.02
            threshold = np.quantile(densities, density_quantile)
            mesh.remove_vertices_by_mask(densities < threshold)
        else:
            log_write(log_file, "Density filtering skipped after crop.")

        log_write(log_file, "Cleaning mesh...")
        mesh.remove_degenerate_triangles()
        mesh.remove_duplicated_triangles()
        mesh.remove_duplicated_vertices()
        mesh.remove_non_manifold_edges()

        log_write(log_file, "Computing mesh normals...")
        mesh.compute_vertex_normals()
        mesh.compute_triangle_normals()

        log_write(log_file, f"Final vertices: {len(mesh.vertices)}")
        log_write(log_file, f"Final triangles: {len(mesh.triangles)}")

        log_write(log_file, f"Writing mesh: {output_path}")
        validate()
        mesh_file = files.enter_context(AtomicOutput(output_path, newline="\n"))
        write_obj(mesh_file.stream, mesh, output_path.stem)
        mesh_file.finish()
        outputs = [mesh_file]
        if log_path is not None:
            try:
                journal = files.enter_context(AtomicOutput(log_path))
                journal.stream.write(log_file.getvalue() + notes)
                journal.finish()
            except Exception:
                log_failed = True
                raise
            outputs.append(journal)
        try:
            publish_outputs(outputs, validate)
        except Exception:
            if log_path is not None and mesh_file.published is not None:
                log_failed = True
            raise

    except Exception as ex:
        print(f"ERROR: {ex}", file=sys.stderr)
        if log_path is not None and paths_checked and not log_failed:
            try:
                validate()
                with AtomicOutput(log_path) as journal:
                    journal.stream.write(log_file.getvalue() + f"\nStatus: ERROR\nError: {ex}\n")
                    journal.finish()
                    publish_outputs([journal], validate)
            except Exception as log_error:
                print(f"ERROR: Could not save log: {log_error}", file=sys.stderr)
        return 1

    finally:
        try:
            files.close()
        except OSError as cleanup_error:
            print(f"ERROR: File cleanup failed: {cleanup_error}", file=sys.stderr)
            return 1

    print(notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
