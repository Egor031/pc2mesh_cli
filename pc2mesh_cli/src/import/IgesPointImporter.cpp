#include "IgesPointImporter.h"

#include <IGESControl_Reader.hxx>
#include <IFSelect_ReturnStatus.hxx>
#include <TopExp_Explorer.hxx>
#include <TopoDS_Shape.hxx>
#include <TopoDS_Vertex.hxx>
#include <TopoDS.hxx>
#include <TopAbs_ShapeEnum.hxx>
#include <BRep_Tool.hxx>
#include <gp_Pnt.hxx>

#include <iostream>

PointCloud IgesPointImporter::Import(const std::string& filePath) const
{
    PointCloud cloud;

    IGESControl_Reader reader;

    IFSelect_ReturnStatus status = reader.ReadFile(filePath.c_str());

    if (status != IFSelect_RetDone)
    {
        std::cerr << "Failed to read IGES file: " << filePath << std::endl;
        return cloud;
    }

    reader.TransferRoots();

    TopoDS_Shape shape = reader.OneShape();

    if (shape.IsNull())
    {
        std::cerr << "IGES was read, but resulting shape is null." << std::endl;
        return cloud;
    }

    for (TopExp_Explorer explorer(shape, TopAbs_VERTEX); explorer.More(); explorer.Next())
    {
        TopoDS_Vertex vertex = TopoDS::Vertex(explorer.Current());
        gp_Pnt p = BRep_Tool::Pnt(vertex);

        cloud.AddPoint(Point3D{
            p.X(),
            p.Y(),
            p.Z()
            });
    }

    std::cout << "Extracted points: " << cloud.Size() << std::endl;

    return cloud;
}