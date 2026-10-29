#include "src/Core/FileSafety.h"
#include "src/export/PlyPointExporter.h"
#include "src/export/XyzPointExporter.h"

#include <cerrno>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>

namespace
{
    int printCalls = 0;

    int FailPrint(std::FILE* file, const char* format, std::va_list arguments)
    {
        if (++printCalls == 2)
        {
            std::fputs("partial", file);
            errno = ENOSPC;
            return -1;
        }
        return std::vfprintf(file, format, arguments);
    }

    int FailFlush(std::FILE* file)
    {
        std::fflush(file);
        errno = EIO;
        return EOF;
    }

    int FailSync(int)
    {
        errno = EIO;
        return -1;
    }

    int FailClose(std::FILE* file)
    {
        std::fclose(file);
        errno = EIO;
        return EOF;
    }

    BOOL WINAPI FailMove(LPCWSTR, LPCWSTR, DWORD)
    {
        SetLastError(ERROR_ACCESS_DENIED);
        return FALSE;
    }
}

int main(int argc, char** argv)
{
    if (argc < 3)
    {
        return 2;
    }
    const std::string mode = argv[1];
    const std::filesystem::path output = argv[2];
    const std::filesystem::path input = argc > 3 ? argv[3] : "";
    try
    {
        if (mode == "check")
        {
            FileSafety::CheckDistinctPaths({input, output});
            return 0;
        }
        if (mode == "ply" || mode == "xyz")
        {
            PointCloud cloud;
            cloud.AddPoint({1.25, -2.5, 3.0});
            cloud.AddPoint({4.0, 5.0, -6.0});
            return (mode == "ply" ? PlyPointExporter().Export(cloud, output.string(), input.string())
                : XyzPointExporter().Export(cloud, output.string(), input.string())) ? 0 : 1;
        }
        FileSafety::FileIO io;
        if (mode == "write_error") io.print = FailPrint;
        else if (mode == "flush_error") io.flush = FailFlush;
        else if (mode == "sync_error") io.sync = FailSync;
        else if (mode == "close_error") io.close = FailClose;
        else if (mode == "publish_error") io.move = FailMove;

        FileSafety::AtomicOutput file(output, input, io);
        file.Print("header\n");
        file.Print("complete payload\n");
        if (mode == "alias_before_publish")
        {
            std::filesystem::create_hard_link(input, output);
        }
        file.Publish();
        std::cout << "Done.\n";
        return 0;
    }
    catch (const std::exception& ex)
    {
        std::cerr << "ERROR: " << ex.what() << '\n';
        return 1;
    }
}
