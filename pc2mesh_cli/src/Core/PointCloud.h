#pragma once

#include "Point3D.h"

#include <vector>
#include <cstddef>

class PointCloud
{
public:
    void AddPoint(const Point3D& point)
    {
        myPoints.push_back(point);
    }

    const std::vector<Point3D>& Points() const
    {
        return myPoints;
    }

    std::size_t Size() const
    {
        return myPoints.size();
    }

    bool IsEmpty() const
    {
        return myPoints.empty();
    }

private:
    std::vector<Point3D> myPoints;
};