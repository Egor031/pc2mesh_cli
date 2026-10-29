#include "IgesPointImporter.h"

#include <IGESControl_Reader.hxx>
#include <IGESData_DirPart.hxx>
#include <IGESData_FileRecognizer.hxx>
#include <IGESData_IGESModel.hxx>
#include <IGESData_IGESReaderData.hxx>
#include <IGESData_IGESReaderTool.hxx>
#include <IGESData_ParamReader.hxx>
#include <IGESData_Protocol.hxx>
#include <IGESData_UndefinedEntity.hxx>
#include <IGESFile_Read.hxx>
#include <IGESGeom_CopiousData.hxx>
#include <IGESGeom_Point.hxx>
#include <IGESGeom_TransformationMatrix.hxx>
#include <Interface_Check.hxx>
#include <Interface_CheckIterator.hxx>
#include <Interface_CheckTool.hxx>
#include <Interface_EntityIterator.hxx>
#include <Interface_FileParameter.hxx>
#include <Interface_ShareTool.hxx>
#include <XSControl_WorkSession.hxx>
#include <XSControl_TransferReader.hxx>
#include <TopExp_Explorer.hxx>
#include <TopoDS_Shape.hxx>
#include <TopoDS_Vertex.hxx>
#include <TopoDS.hxx>
#include <TopAbs_ShapeEnum.hxx>
#include <BRep_Tool.hxx>
#include <gp_Pnt.hxx>
#include <gp_GTrsf.hxx>

#include <charconv>
#include <cmath>
#include <fstream>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

namespace
{
    [[noreturn]] void Invalid(int entity, const std::string& reason)
    {
        throw std::runtime_error("Invalid IGES " + (entity ? "DE " + std::to_string(2 * entity - 1) : "file") + ": " + reason);
    }

    // ReadXYZ in OCCT 7.9.3 can silently replace malformed reals with zero.
    // Keep the native lexer's tokens before typed entities allocate their arrays.
    // Original token pointers expire when IGESFile_Read releases lexer storage.
    struct SourceParameters
    {
        Handle(IGESData_IGESReaderData) data;
        std::vector<std::vector<std::string>> values;

        void Capture(const Handle(IGESData_IGESReaderData)& source)
        {
            if (!data.IsNull()) return;
            data = source;
            values.resize(data->NbRecords() + 1);
            for (int i = 1; i <= data->NbRecords(); ++i)
            {
                values[i].resize(data->NbParams(i) + 1);
                for (int j = 1; j <= data->NbParams(i); ++j)
                {
                    auto& parameter = data->ChangeParam(i, j);
                    values[i][j] = parameter.CValue();
                    const auto type = parameter.ParamType();
                    const int reference = parameter.EntityNumber();
                    parameter.Init(values[i][j].c_str(), type);
                    parameter.SetEntityNumber(reference);
                }
            }
        }

        std::string Token(int entity, int parameter) const
        {
            if (parameter >= static_cast<int>(values[entity].size()))
                Invalid(entity, "missing parameter " + std::to_string(parameter - 1));
            const auto& token = values[entity][parameter];
            const auto first = token.find_first_not_of(' ');
            if (first == std::string::npos) return {};
            return token.substr(first, token.find_last_not_of(' ') - first + 1);
        }

        int Integer(int entity, int parameter, bool defaultZero = false) const
        {
            std::string text = Token(entity, parameter);
            if (text.empty() && defaultZero) return 0;
            if (!text.empty() && text.front() == '+') text.erase(0, 1);
            int value = 0;
            const auto result = std::from_chars(text.data(), text.data() + text.size(), value);
            if (text.empty() || result.ec != std::errc{} || result.ptr != text.data() + text.size())
                Invalid(entity, "invalid integer parameter " + std::to_string(parameter - 1));
            return value;
        }

        void Real(int entity, int parameter)
        {
            std::string text = Token(entity, parameter);
            double value = 0;
            if (!text.empty())
            {
                // Decimal IGES syntax, including D exponents; exclude NaN,
                // infinity and hexadecimal C floating-point extensions.
                std::size_t pos = 0;
                if (text[pos] == '+' || text[pos] == '-') ++pos;
                bool digits = false;
                auto readDigits = [&]() {
                    while (pos < text.size() && text[pos] >= '0' && text[pos] <= '9')
                    { digits = true; ++pos; }
                };
                readDigits();
                if (pos < text.size() && text[pos] == '.') { ++pos; readDigits(); }
                bool valid = digits;
                if (pos < text.size() && (text[pos] == 'D' || text[pos] == 'd' || text[pos] == 'E' || text[pos] == 'e'))
                {
                    text[pos++] = 'e';
                    if (pos < text.size() && (text[pos] == '+' || text[pos] == '-')) ++pos;
                    digits = false;
                    readDigits();
                    valid = valid && digits;
                }
                if (!valid || pos != text.size())
                    Invalid(entity, "invalid real parameter " + std::to_string(parameter - 1));
                if (text.front() == '+') text.erase(0, 1);
                const auto result = std::from_chars(text.data(), text.data() + text.size(), value);
                if (result.ec != std::errc{} || result.ptr != text.data() + text.size() || !std::isfinite(value))
                    Invalid(entity, "non-finite or out-of-range real parameter " + std::to_string(parameter - 1));
            }
            // Avoid OCCT's atoi(int) conversion of integer-looking coordinates
            // and its fixed-size real conversion buffer. Feed a short real.
            char buffer[64];
            const auto result = std::to_chars(buffer, buffer + sizeof(buffer), value,
                std::chars_format::scientific, std::numeric_limits<double>::max_digits10 - 1);
            values[entity][parameter].assign(buffer, result.ptr);
            data->ChangeParam(entity, parameter).Init(values[entity][parameter].c_str(), Interface_ParamReal);
        }

        void Tail(int entity, int parameter) const
        {
            const int end = data->NbParams(entity);
            for (int list = 0; list < 2 && parameter <= end; ++list)
            {
                const int count = Integer(entity, parameter++, true);
                if (count < 0 || count > end - parameter + 1) Invalid(entity, "invalid reference-list length");
                for (int j = 0; j < count; ++j)
                {
                    const int pointer = Integer(entity, parameter++);
                    if (pointer <= 0 || pointer % 2 != 1 || (pointer + 1LL) / 2 > data->NbRecords())
                        Invalid(entity, "invalid reference-list pointer");
                }
            }
            if (parameter <= end) Invalid(entity, "unexpected extra parameters");
        }

        void Validate()
        {
            for (int i = 1; i <= data->NbRecords(); ++i)
            {
                const auto kind = data->DirType(i);
                int directory[17] = {};
                char reserved1[10] = {}, reserved2[10] = {}, label[10] = {}, subscript[10] = {};
                data->DirPart(i).Values(directory[0], directory[1], directory[2], directory[3],
                    directory[4], directory[5], directory[6], directory[7], directory[8],
                    directory[9], directory[10], directory[11], directory[12], directory[13],
                    directory[14], directory[15], directory[16], reserved1, reserved2, label, subscript);
                const int transform = directory[6];
                if (transform && (transform < 0 || transform % 2 != 1 ||
                    (transform + 1LL) / 2 > data->NbRecords() ||
                    data->DirType(static_cast<int>((transform + 1LL) / 2)).Type() != 124))
                    Invalid(i, "invalid transformation pointer");
                if (Integer(i, 1) != kind.Type()) Invalid(i, "directory/parameter type mismatch");
                int end = 0;
                if (kind.Type() == 116)
                {
                    if (kind.Form() != 0) Invalid(i, "unsupported point form");
                    for (int p = 2; p <= 4; ++p) Real(i, p);
                    end = 5;
                    if (data->NbParams(i) >= 5)
                    {
                        const int symbol = Integer(i, 5, true);
                        if (symbol < 0 || (symbol && (symbol % 2 != 1 || (symbol + 1LL) / 2 > data->NbRecords())))
                            Invalid(i, "invalid display-symbol pointer");
                        end = 6;
                    }
                }
                else if (kind.Type() == 106)
                {
                    const int form = kind.Form();
                    if (form < 1 || form > 3) Invalid(i, "unsupported copious-data form (supported: 1, 2, 3)");
                    if (Integer(i, 2) != form) Invalid(i, "copious-data interpretation/form mismatch");
                    const int count = Integer(i, 3);
                    const int stride = form == 1 ? 2 : form == 2 ? 3 : 6;
                    const int first = form == 1 ? 5 : 4;
                    if (count <= 0 || count > (data->NbParams(i) - first + 1) / stride)
                        Invalid(i, "invalid point count or incomplete copious data");
                    end = first + count * stride;
                    for (int p = 4; p < end; ++p) Real(i, p);
                }
                else if (kind.Type() == 124)
                {
                    for (int p = 2; p <= 13; ++p) Real(i, p);
                    end = 14;
                }
                if (end) Tail(i, end);
                // IGES non-string parameters are decimal numbers or defaults.
                // Reject lexer recovery tokens throughout the model as well:
                // CAD-only ReadXYZ has the same silent-zero failure mode.
                for (int p = 2; p <= data->NbParams(i); ++p)
                {
                    const auto type = data->Param(i, p).ParamType();
                    if (type == Interface_ParamMisc) Invalid(i, "invalid numeric parameter " + std::to_string(p - 1));
                    if (type == Interface_ParamReal || type == Interface_ParamEnum) Real(i, p);
                    else if (type == Interface_ParamInteger) Integer(i, p);
                }
            }
        }
    };

    class LiteralEntity : public IGESData_UndefinedEntity
    {
    public:
        explicit LiteralEntity(SourceParameters& source) : mySource(source) {}
        Standard_Boolean ReadDir(const Handle(IGESData_IGESReaderData)& data,
            IGESData_DirPart& directory, Handle(Interface_Check)&) override
        {
            mySource.Capture(data);
            const auto kind = directory.Type();
            // OCCT passes a copy: neutral references affect only this disposable
            // recognition entity, not the retained original directory data.
            directory.Init(kind.Type(), 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, kind.Form(), "", "", "", "");
            return Standard_True;
        }
        void ReadOwnParams(const Handle(IGESData_IGESReaderData)&, IGESData_ParamReader& parameters) override
        { parameters.EndAll(); }
    private:
        SourceParameters& mySource;
    };

    class LiteralRecognizer : public IGESData_FileRecognizer
    {
    public:
        explicit LiteralRecognizer(SourceParameters& source) : mySource(source) {}
    private:
        void Eval(const IGESData_IGESType&) override { SetOK(new LiteralEntity(mySource)); }
        SourceParameters& mySource;
    };

    void CheckRecordEnds(const std::string& filePath, const SourceParameters& source)
    {
        // OCCT also accepts a final numeric parameter without the entity record
        // delimiter. The retained tokens cannot distinguish this from a complete
        // record. Check only fixed-record framing here; OCCT still owns token,
        // Hollerith, delimiter and entity interpretation.
        std::ifstream input(filePath, std::ios::binary);
        if (!input) Invalid(0, "cannot verify physical records");
        std::vector<char> ends(source.data->NbRecords() + 1, '\0');
        char row[80];
        while (input.read(row, sizeof(row)) || input.gcount())
        {
            if (input.gcount() != sizeof(row)) Invalid(0, "incomplete physical record");
            if (row[72] == 'P')
            {
                std::string pointer(row + 64, 8);
                const auto first = pointer.find_first_not_of(' ');
                if (first == std::string::npos) Invalid(0, "missing parameter-record pointer");
                pointer = pointer.substr(first, pointer.find_last_not_of(' ') - first + 1);
                int de = 0;
                const auto result = std::from_chars(pointer.data(), pointer.data() + pointer.size(), de);
                if (result.ec != std::errc{} || result.ptr != pointer.data() + pointer.size() ||
                    de <= 0 || de % 2 != 1 || (de + 1LL) / 2 > source.data->NbRecords())
                    Invalid(0, "invalid parameter-record pointer");
                int last = 63;
                while (last >= 0 && row[last] == ' ') --last;
                const int entity = static_cast<int>((de + 1LL) / 2);
                const int type = source.data->DirType(entity).Type();
                if ((type == 116 || type == 106 || type == 124) && last >= 0 &&
                    row[last] != source.data->GlobalSection().Separator() &&
                    row[last] != source.data->GlobalSection().EndMark())
                    Invalid(entity, "numeric parameter/delimiter split across physical records");
                if (last >= 0) ends[entity] = row[last];
            }
            if (input.peek() == '\r') input.get();
            if (input.peek() == '\n') input.get();
        }
        if (input.bad()) Invalid(0, "error reading physical records");
        for (int i = 1; i <= source.data->NbRecords(); ++i)
        {
            const int type = source.data->DirType(i).Type();
            if ((type == 116 || type == 106 || type == 124) && ends[i] != source.data->GlobalSection().EndMark())
                Invalid(i, "missing entity record delimiter");
        }
    }

    void Check(const Handle(Interface_Check)& check, int entity)
    {
        if (!check.IsNull() && check->NbFails()) Invalid(entity, check->CFail(1));
    }

    void Check(Interface_CheckIterator checks)
    {
        for (checks.Start(); checks.More(); checks.Next()) Check(checks.Value(), checks.Number());
    }

    bool IsPointData(const Handle(IGESData_IGESEntity)& entity)
    {
        return entity->TypeNumber() == 116 || (entity->TypeNumber() == 106 && entity->FormNumber() >= 1 && entity->FormNumber() <= 3);
    }

    std::vector<bool> Services(const Handle(IGESData_IGESModel)& model,
        const Handle(IGESData_Protocol)& protocol)
    {
        // View clipping planes (as in det4.igs) are presentation data. Never
        // ignore geometry merely because it is subordinate or belongs to a group.
        std::vector<bool> service(model->NbEntities() + 1, false);
        std::vector<int> pending;
        for (int i = 1; i <= model->NbEntities(); ++i)
        {
            const int type = model->Entity(i)->TypeNumber();
            if (type == 410 || type == 314 || type == 406) { service[i] = true; pending.push_back(i); }
            if (type == 124) service[i] = true;
        }
        Interface_ShareTool shares(model, protocol);
        while (!pending.empty())
        {
            const int i = pending.back(); pending.pop_back();
            for (auto refs = shares.Shareds(model->Entity(i)); refs.More(); refs.Next())
            {
                const int j = model->Number(refs.Value());
                if (j && !service[j])
                {
                    const int type = model->Entity(j)->TypeNumber();
                    if (type == 108 || type == 124 || type == 314 || type == 406)
                    {
                        service[j] = true;
                        pending.push_back(j);
                    }
                }
            }
        }
        return service;
    }

    void Finite(const gp_GTrsf& transform, int entity)
    {
        for (int row = 1; row <= 3; ++row)
            for (int col = 1; col <= 4; ++col)
                if (!std::isfinite(transform.Value(row, col))) Invalid(entity, "non-finite transformation");
    }

    std::vector<gp_GTrsf> Locations(const Handle(IGESData_IGESModel)& model)
    {
        std::vector<gp_GTrsf> locations(model->NbEntities() + 1);
        std::vector<int> state(model->NbEntities() + 1, 0);
        for (int i = 1; i <= model->NbEntities(); ++i)
        {
            if (!model->Entity(i)->HasTransf()) continue;
            std::vector<int> path;
            int j = model->Number(model->Entity(i)->Transf());
            while (j && state[j] != 2)
            {
                if (state[j] == 1) Invalid(j, "cyclic transformation chain");
                state[j] = 1;
                path.push_back(j);
                const auto entity = model->Entity(j);
                const auto matrix = Handle(IGESGeom_TransformationMatrix)::DownCast(entity);
                if (matrix.IsNull()) Invalid(j, "transformation reference is not a matrix");
                if (matrix->FormNumber() != 0 && matrix->FormNumber() != 1)
                    Invalid(j, "unsupported non-Cartesian transformation");
                locations[j] = matrix->Value();
                Finite(locations[j], j);
                j = entity->HasTransf() ? model->Number(entity->Transf()) : 0;
            }
            while (!path.empty())
            {
                j = path.back(); path.pop_back();
                const auto entity = model->Entity(j);
                if (entity->HasTransf()) locations[j] = locations[model->Number(entity->Transf())] * locations[j];
                Finite(locations[j], j);
                state[j] = 2;
            }
        }
        return locations;
    }

    void AddPoint(PointCloud& cloud, gp_Pnt point, const gp_GTrsf& location, double unit, int entity)
    {
        gp_XYZ xyz = point.XYZ();
        location.Transforms(xyz);
        xyz *= unit;
        if (!std::isfinite(xyz.X()) || !std::isfinite(xyz.Y()) || !std::isfinite(xyz.Z()))
            Invalid(entity, "non-finite transformed coordinates");
        cloud.AddPoint({ xyz.X(), xyz.Y(), xyz.Z() });
    }
}

PointCloud IgesPointImporter::Import(const std::string& filePath) const
{
    IGESControl_Reader reader;
    const auto protocol = Handle(IGESData_Protocol)::DownCast(reader.WS()->Protocol());
    SourceParameters source;
    Handle(IGESData_IGESModel) literalModel = new IGESData_IGESModel;
    std::string path = filePath;
    if (IGESFile_Read(path.data(), literalModel, protocol, new LiteralRecognizer(source)) != 0)
        Invalid(0, "failed to read " + filePath);
    Check(literalModel->GlobalCheck(), 0);
    if (source.data.IsNull() || source.data->NbRecords() == 0) Invalid(0, "no entities");
    // Preserve native file-framing failures across the second, typed load.
    // Ordinary header/entity warnings are allowed; lexer recovery is not.
    if (literalModel->GlobalCheck()->NbWarnings() > source.data->GlobalCheck()->NbWarnings())
        Invalid(0, "OCCT reported damaged file structure: " + std::string(literalModel->GlobalCheck()->CWarning(1)));
    CheckRecordEnds(filePath, source);
    source.Validate();

    Handle(IGESData_IGESModel) model = new IGESData_IGESModel;
    IGESData_IGESReaderTool loader(source.data, protocol);
    loader.Prepare(Handle(IGESData_FileRecognizer)());
    loader.SetErrorHandle(Standard_True);
    loader.LoadModel(model);
    model->SetProtocol(protocol);
    Check(model->GlobalCheck(), 0);
    for (int i = 1; i <= model->NbEntities(); ++i) Check(model->Check(i, Standard_False), i);
    const auto locations = Locations(model);
    Interface_CheckTool checker(model, protocol);
    Check(checker.CompleteCheckList());

    const double unit = model->GlobalSection().UnitValue();
    if (!std::isfinite(unit) || unit <= 0 || !std::isfinite(model->GlobalSection().Scale()) || model->GlobalSection().Scale() <= 0)
        Invalid(0, "invalid units or model-space scale");
    if (model->GlobalSection().Scale() != 1.0)
        std::cout << "IGES model-space scale: " << model->GlobalSection().Scale()
                  << "; preserving model-space coordinates, converted to millimeters." << std::endl;
    const auto service = Services(model, protocol);
    bool hasPoints = false, hasGeometry = false;
    for (int i = 1; i <= model->NbEntities(); ++i)
    {
        const auto entity = model->Entity(i);
        if (IsPointData(entity)) hasPoints = true;
        else if (!service[i])
        {
            if (entity->TypeNumber() == 402 && (entity->FormNumber() == 1 || entity->FormNumber() == 7 || entity->FormNumber() == 14 || entity->FormNumber() == 15))
                continue; // non-placement groups do not create extra occurrences
            if (entity->TypeNumber() == 308 || entity->TypeNumber() == 408 || entity->TypeNumber() == 430 ||
                !Handle(IGESData_UndefinedEntity)::DownCast(entity).IsNull())
                Invalid(i, "unsupported geometry or instance placement");
            hasGeometry = true;
        }
    }
    if (hasPoints && hasGeometry) Invalid(0, "mixed point data and CAD geometry would produce an incomplete cloud");

    PointCloud cloud;
    if (hasPoints)
    {
        for (int i = 1; i <= model->NbEntities(); ++i)
        {
            const auto entity = model->Entity(i);
            if (!IsPointData(entity)) continue;
            const gp_GTrsf location = entity->HasTransf() ? locations[model->Number(entity->Transf())] : gp_GTrsf();
            if (const auto point = Handle(IGESGeom_Point)::DownCast(entity); !point.IsNull())
                AddPoint(cloud, point->Value(), location, unit, i);
            else if (const auto points = Handle(IGESGeom_CopiousData)::DownCast(entity); !points.IsNull())
                for (int j = 1; j <= points->NbPoints(); ++j) AddPoint(cloud, points->Point(j), location, unit, i);
            else Invalid(i, "point entity failed to load");
        }
    }
    else if (hasGeometry)
    {
        // Preserve CAD-only extraction of topological vertices without sampling.
        // Never combine it with the direct point-data path.
        for (int i = 1; i <= model->NbEntities(); ++i)
            if (model->Entity(i)->TypeNumber() == 124 && model->Entity(i)->HasTransf())
                Invalid(i, "nested CAD transformations are unsupported");
        reader.WS()->SetModel(model);
        const int roots = reader.NbRootsForTransfer();
        if (!roots) Invalid(0, "no transferable geometry");
        for (int i = 1; i <= roots; ++i)
            if (!reader.TransferOneRoot(i)) Invalid(0, "CAD root transfer failed");
        Check(reader.WS()->TransferReader()->LastCheckList());
        const auto shape = reader.OneShape();
        if (shape.IsNull()) Invalid(0, "CAD transfer produced no shape");
        for (TopExp_Explorer vertices(shape, TopAbs_VERTEX); vertices.More(); vertices.Next())
            AddPoint(cloud, BRep_Tool::Pnt(TopoDS::Vertex(vertices.Current())), gp_GTrsf(), 1.0, 0);
    }
    if (cloud.IsEmpty()) Invalid(0, "no points");
    std::cout << "Extracted points: " << cloud.Size() << std::endl;
    return cloud;
}
