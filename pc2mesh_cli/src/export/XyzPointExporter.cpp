#include "XyzPointExporter.h"

#include "../Core/FileSafety.h"

#include <exception>
#include <iostream>

bool XyzPointExporter::Export(const PointCloud& cloud, const std::string& filePath,
    const std::string& inputPath) const
{
    try
    {
        FileSafety::AtomicOutput out(filePath, inputPath);
        for (const Point3D& point : cloud.Points())
        {
            out.Print("%.9f %.9f %.9f\n", point.x, point.y, point.z);
        }
        out.Publish();
        return true;
    }
    catch (const std::exception& ex)
    {
        std::cerr << "XYZ export error: " << ex.what() << std::endl;
        return false;
    }
}
