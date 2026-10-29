#include "FileSafety.h"

#include <cerrno>
#include <fcntl.h>
#include <io.h>
#include <iostream>
#include <random>
#include <stdexcept>
#include <string>
#include <system_error>
#include <utility>
#include <vector>

#pragma comment(lib, "Advapi32.lib")

namespace fs = std::filesystem;

namespace
{
    [[noreturn]] void WinError(const char* operation)
    {
        throw std::system_error(static_cast<int>(GetLastError()), std::system_category(), operation);
    }

    [[noreturn]] void StreamError(const char* operation)
    {
        throw std::system_error(errno ? errno : EIO, std::generic_category(), operation);
    }

    bool SameName(const fs::path& a, const fs::path& b)
    {
        return CompareStringOrdinal(a.c_str(), -1, b.c_str(), -1, TRUE) == CSTR_EQUAL;
    }

    fs::path Resolve(const fs::path& path)
    {
        if (path.empty() || path.native().find(L'\0') != std::wstring::npos
            || path.relative_path().native().find(L':') != std::wstring::npos
            || path.native().starts_with(L"\\\\.\\"))
        {
            throw std::runtime_error("Expected a regular file path (devices and alternate streams are not supported).");
        }
        return fs::weakly_canonical(fs::absolute(path));
    }

    bool Exists(const fs::path& path)
    {
        // Permission and sharing errors must not be mistaken for a missing file.
        return fs::exists(fs::status(path));
    }

    BY_HANDLE_FILE_INFORMATION Identity(HANDLE handle)
    {
        BY_HANDLE_FILE_INFORMATION info{};
        if (!GetFileInformationByHandle(handle, &info))
        {
            WinError("Query file identity");
        }
        return info;
    }

    void PreservePermissions(const fs::path& target, const fs::path& temporary)
    {
        if (!Exists(target))
        {
            return;
        }
        HANDLE handle = CreateFileW(target.c_str(), DELETE | READ_CONTROL,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, nullptr,
            OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (handle == INVALID_HANDLE_VALUE)
        {
            WinError("Check output replacement access");
        }
        try
        {
            DWORD needed = 0;
            if (!GetKernelObjectSecurity(handle, DACL_SECURITY_INFORMATION, nullptr, 0, &needed)
                && GetLastError() != ERROR_INSUFFICIENT_BUFFER)
            {
                WinError("Read output permissions");
            }
            std::vector<char> descriptor(needed);
            if (!GetKernelObjectSecurity(handle, DACL_SECURITY_INFORMATION, descriptor.data(), needed, &needed))
            {
                WinError("Read output permissions");
            }
            SECURITY_DESCRIPTOR_CONTROL control = 0;
            DWORD revision = 0;
            if (!GetSecurityDescriptorControl(descriptor.data(), &control, &revision))
            {
                WinError("Read output permission inheritance");
            }
            const DWORD flags = DACL_SECURITY_INFORMATION | ((control & SE_DACL_PROTECTED)
                ? PROTECTED_DACL_SECURITY_INFORMATION : UNPROTECTED_DACL_SECURITY_INFORMATION);
            if (!SetFileSecurityW(temporary.c_str(), flags, descriptor.data()))
            {
                WinError("Preserve output permissions");
            }
        }
        catch (...)
        {
            CloseHandle(handle);
            throw;
        }
        CloseHandle(handle);
    }
}

void FileSafety::CheckDistinctPaths(std::initializer_list<fs::path> paths)
{
    std::vector<fs::path> names;
    for (const auto& path : paths)
    {
        if (path.empty())
        {
            continue;
        }
        const auto resolved = Resolve(path);
        for (const auto& previous : names)
        {
            if ((Exists(path) && Exists(previous) && fs::equivalent(path, previous))
                || SameName(resolved, Resolve(previous)))
            {
                throw std::runtime_error("Input and output paths refer to the same file.");
            }
        }
        names.push_back(path);
    }
}

FileSafety::InputFileLock::InputFileLock(const fs::path& path)
    : myPath(fs::absolute(path))
{
    Resolve(myPath);
    myHandle = CreateFileW(myPath.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr,
        OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (myHandle == INVALID_HANDLE_VALUE)
    {
        WinError("Open input file");
    }
    try
    {
        myIdentity = Identity(myHandle);
        if (GetFileType(myHandle) != FILE_TYPE_DISK || (myIdentity.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY))
        {
            throw std::runtime_error("Input is not a regular file.");
        }
    }
    catch (...)
    {
        CloseHandle(myHandle);
        throw;
    }
}

FileSafety::InputFileLock::~InputFileLock()
{
    if (myHandle != INVALID_HANDLE_VALUE)
    {
        CloseHandle(myHandle);
    }
}

void FileSafety::InputFileLock::CheckPath() const
{
    HANDLE handle = CreateFileW(myPath.c_str(), 0, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (handle == INVALID_HANDLE_VALUE)
    {
        WinError("Recheck input file");
    }
    BY_HANDLE_FILE_INFORMATION current{};
    try
    {
        current = Identity(handle);
    }
    catch (...)
    {
        CloseHandle(handle);
        throw;
    }
    CloseHandle(handle);
    if (current.dwVolumeSerialNumber != myIdentity.dwVolumeSerialNumber
        || current.nFileIndexHigh != myIdentity.nFileIndexHigh || current.nFileIndexLow != myIdentity.nFileIndexLow)
    {
        throw std::runtime_error("Input path changed while processing.");
    }
}

FileSafety::FileIO::FileIO() : sync(_commit)
{
}

const FileSafety::FileIO& FileSafety::AtomicOutput::DefaultIO()
{
    static const FileIO io;
    return io;
}

FileSafety::AtomicOutput::AtomicOutput(const fs::path& path, const fs::path& inputPath, const FileIO& io)
    : myRequestedPath(fs::absolute(path)), myTargetPath(Resolve(path)), myInputPath(inputPath), myIO(io)
{
    CheckDistinctPaths({myRequestedPath, myInputPath});
    if (Exists(myTargetPath) && !fs::is_regular_file(myTargetPath))
    {
        throw std::runtime_error("Output is not a regular file.");
    }

    std::random_device random;
    HANDLE handle = INVALID_HANDLE_VALUE;
    for (int attempt = 0; attempt < 32; ++attempt)
    {
        myTemporaryPath = myTargetPath.parent_path()
            / (L".pc2mesh-" + std::to_wstring(random()) + L"-" + std::to_wstring(random()) + L".tmp");
        handle = CreateFileW(myTemporaryPath.c_str(), GENERIC_WRITE, 0, nullptr,
            CREATE_NEW, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (handle != INVALID_HANDLE_VALUE)
        {
            break;
        }
        if (GetLastError() != ERROR_FILE_EXISTS && GetLastError() != ERROR_ALREADY_EXISTS)
        {
            WinError("Create temporary output");
        }
    }
    if (handle == INVALID_HANDLE_VALUE)
    {
        WinError("Create unique temporary output");
    }
    const int descriptor = _open_osfhandle(reinterpret_cast<intptr_t>(handle), _O_WRONLY | _O_TEXT);
    if (descriptor == -1)
    {
        CloseHandle(handle);
        DeleteFileW(myTemporaryPath.c_str());
        StreamError("Attach temporary output");
    }
    myFile = _fdopen(descriptor, "w");
    if (!myFile)
    {
        _close(descriptor);
        DeleteFileW(myTemporaryPath.c_str());
        StreamError("Open temporary output stream");
    }
}

FileSafety::AtomicOutput::~AtomicOutput()
{
    if (myFile)
    {
        std::fclose(myFile);
    }
    if (!myTemporaryPath.empty())
    {
        if (!DeleteFileW(myTemporaryPath.c_str()) && GetLastError() != ERROR_FILE_NOT_FOUND)
        {
            const auto error = GetLastError();
            std::wcerr << L"WARNING: Could not remove temporary output: " << myTemporaryPath.native()
                << L" (Windows error " << error << L")\n";
        }
    }
}

void FileSafety::AtomicOutput::Print(const char* format, ...)
{
    if (!myFile)
    {
        throw std::runtime_error("Output stream is already closed.");
    }
    std::va_list arguments;
    va_start(arguments, format);
    const int written = myIO.print(myFile, format, arguments);
    va_end(arguments);
    if (written < 0)
    {
        StreamError("Write temporary output");
    }
}

void FileSafety::AtomicOutput::Publish()
{
    if (!myFile)
    {
        throw std::runtime_error("Output stream is already closed.");
    }
    if (std::ferror(myFile) || myIO.flush(myFile) != 0 || myIO.sync(_fileno(myFile)) != 0)
    {
        StreamError("Flush temporary output");
    }
    auto* file = std::exchange(myFile, nullptr);
    if (myIO.close(file) != 0)
    {
        StreamError("Close temporary output");
    }
    CheckDistinctPaths({myRequestedPath, myInputPath});
    if (!SameName(Resolve(myRequestedPath), myTargetPath))
    {
        throw std::runtime_error("Output path changed while processing.");
    }
    PreservePermissions(myTargetPath, myTemporaryPath);
    if (!myIO.move(myTemporaryPath.c_str(), myTargetPath.c_str(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH))
    {
        WinError("Publish output");
    }
    myTemporaryPath.clear();
}
