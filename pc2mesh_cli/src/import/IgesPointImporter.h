#pragma once

#include "../core/PointCloud.h"

#include <string>

class IgesPointImporter
{
public:
    PointCloud Import(const std::string& filePath) const;
};