#pragma once

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>

#include <cstdarg>
#include <cstdio>
#include <filesystem>
#include <initializer_list>

namespace FileSafety
{
    void CheckDistinctPaths(std::initializer_list<std::filesystem::path> paths);

    class InputFileLock
    {
    public:
        explicit InputFileLock(const std::filesystem::path& path);
        ~InputFileLock();
        InputFileLock(const InputFileLock&) = delete;
        InputFileLock& operator=(const InputFileLock&) = delete;
        void CheckPath() const;

    private:
        std::filesystem::path myPath;
        HANDLE myHandle = INVALID_HANDLE_VALUE;
        BY_HANDLE_FILE_INFORMATION myIdentity{};
    };

    // The function table permits deterministic tests of otherwise rare I/O failures.
    struct FileIO
    {
        int (*print)(std::FILE*, const char*, std::va_list) = std::vfprintf;
        int (*flush)(std::FILE*) = std::fflush;
        int (*sync)(int);
        int (*close)(std::FILE*) = std::fclose;
        decltype(&MoveFileExW) move = MoveFileExW;
        FileIO();
    };

    class AtomicOutput
    {
    public:
        explicit AtomicOutput(const std::filesystem::path& path,
            const std::filesystem::path& inputPath = {}, const FileIO& io = DefaultIO());
        ~AtomicOutput();
        AtomicOutput(const AtomicOutput&) = delete;
        AtomicOutput& operator=(const AtomicOutput&) = delete;

        void Print(const char* format, ...);
        void Publish();
        static const FileIO& DefaultIO();

    private:
        std::filesystem::path myRequestedPath;
        std::filesystem::path myTargetPath;
        std::filesystem::path myTemporaryPath;
        std::filesystem::path myInputPath;
        std::FILE* myFile = nullptr;
        const FileIO& myIO;
    };
}
