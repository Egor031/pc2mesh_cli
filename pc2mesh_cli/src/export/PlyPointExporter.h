#pragma once

#include "../core/PointCloud.h"

#include <string>

class PlyPointExporter
{
public:
    bool Export(const PointCloud& cloud, const std::string& filePath,
        const std::string& inputPath = {}) const;
};
