#include "src/import/IgesPointImporter.h"
#include "src/export/PlyPointExporter.h"
#include "src/Core/FileSafety.h"

#include <Standard_Failure.hxx>

#include <exception>
#include <iostream>
#include <string>

int main(int argc, char** argv)
{
    if (argc < 4)
    {
        std::cout << "Usage:\n";
        std::cout << "  pc2mesh extract input.igs output.ply\n";
        return 1;
    }

    std::string command = argv[1];
    std::string inputPath = argv[2];
    std::string outputPath = argv[3];

    if (command != "extract")
    {
        std::cerr << "Unknown command: " << command << std::endl;
        return 1;
    }

    try
    {
        FileSafety::CheckDistinctPaths({inputPath, outputPath});
        FileSafety::InputFileLock inputLock(inputPath);
        inputLock.CheckPath();

        IgesPointImporter importer;
        PointCloud cloud = importer.Import(inputPath);
        inputLock.CheckPath();

        if (cloud.IsEmpty())
        {
            std::cerr << "No points extracted." << std::endl;
            return 1;
        }

        PlyPointExporter exporter;
        if (!exporter.Export(cloud, outputPath, inputPath))
        {
            std::cerr << "Failed to export PLY: " << outputPath << std::endl;
            return 1;
        }

        std::cout << "Done." << std::endl;
        std::cout << "Output: " << outputPath << std::endl;
        std::cout << "Points: " << cloud.Size() << std::endl;
        return 0;
    }
    catch (const Standard_Failure& ex)
    {
        std::cerr << "ERROR: " << ex.GetMessageString() << std::endl;
    }
    catch (const std::exception& ex)
    {
        std::cerr << "ERROR: " << ex.what() << std::endl;
    }
    return 1;
}
