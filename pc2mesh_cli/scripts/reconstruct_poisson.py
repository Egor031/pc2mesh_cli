import sys
from pathlib import Path
from datetime import datetime

import numpy as np
import open3d as o3d


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

    output_path.parent.mkdir(parents=True, exist_ok=True)

    log_file = None

    try:
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = open(log_path, "w", encoding="utf-8")

        log_write(log_file, "PC2Mesh Poisson reconstruction")
        log_write(log_file, f"Time: {datetime.now()}")
        log_write(log_file, f"Input: {input_path}")
        log_write(log_file, f"Output: {output_path}")
        log_write(log_file, "")

        if not input_path.exists():
            raise RuntimeError(f"Input file does not exist: {input_path}")

        log_write(log_file, "Reading point cloud...")
        cloud = o3d.io.read_point_cloud(str(input_path))

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
        ok = o3d.io.write_triangle_mesh(
            str(output_path),
            mesh,
            write_ascii=True
        )

        if not ok:
            raise RuntimeError("Failed to write output mesh.")

        log_write(log_file, "")
        log_write(log_file, "Status: OK")
        log_write(log_file, "")
        log_write(log_file, "Notes:")
        log_write(log_file, "- Result is a triangle mesh, not a parametric NX model.")
        log_write(log_file, "- Missing scan areas cannot be restored reliably.")
        log_write(log_file, "- Some real holes may be closed by Poisson reconstruction.")
        log_write(log_file, "- Foreign scanned objects may appear in the output mesh.")

        return 0

    except Exception as ex:
        if log_file is not None:
            log_write(log_file, "")
            log_write(log_file, f"Status: ERROR")
            log_write(log_file, f"Error: {ex}")
        else:
            print(f"ERROR: {ex}")

        return 1

    finally:
        if log_file is not None:
            log_file.close()


if __name__ == "__main__":
    raise SystemExit(main())