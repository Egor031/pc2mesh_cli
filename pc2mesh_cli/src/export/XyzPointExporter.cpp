#include "XyzPointExporter.h"

#include <fstream>
#include <iomanip>

bool XyzPointExporter::Export(const PointCloud& cloud, const std::string& filePath) const
{
    std::ofstream out(filePath);

    if (!out.is_open())
    {
        return false;
    }

    out << std::fixed << std::setprecision(9);

    for (const Point3D& point : cloud.Points())
    {
        out << point.x << " "
            << point.y << " "
            << point.z << "\n";
    }

    return true;
}