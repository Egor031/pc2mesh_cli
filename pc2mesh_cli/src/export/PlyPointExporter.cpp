#include "PlyPointExporter.h"

#include "../Core/FileSafety.h"

#include <exception>
#include <iostream>

bool PlyPointExporter::Export(const PointCloud& cloud, const std::string& filePath,
    const std::string& inputPath) const
{
    try
    {
        FileSafety::AtomicOutput out(filePath, inputPath);
        const auto& points = cloud.Points();

        out.Print("ply\nformat ascii 1.0\nelement vertex %zu\n", points.size());
        out.Print("property double x\nproperty double y\nproperty double z\nend_header\n");

        for (const Point3D& point : points)
        {
            out.Print("%.9f %.9f %.9f\n", point.x, point.y, point.z);
        }

        out.Publish();
        return true;
    }
    catch (const std::exception& ex)
    {
        std::cerr << "PLY export error: " << ex.what() << std::endl;
        return false;
    }
}
