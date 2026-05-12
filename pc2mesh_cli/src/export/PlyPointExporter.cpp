#include "PlyPointExporter.h"

#include <fstream>
#include <iomanip>

bool PlyPointExporter::Export(const PointCloud& cloud, const std::string& filePath) const
{
    std::ofstream out(filePath);

    if (!out.is_open())
    {
        return false;
    }

    const auto& points = cloud.Points();

    out << "ply\n";
    out << "format ascii 1.0\n";
    out << "element vertex " << points.size() << "\n";
    out << "property double x\n";
    out << "property double y\n";
    out << "property double z\n";
    out << "end_header\n";

    out << std::fixed << std::setprecision(9);

    for (const Point3D& point : points)
    {
        out << point.x << " "
            << point.y << " "
            << point.z << "\n";
    }

    return true;
}