"""Bounded PLY 1.0 preflight for the Open3D 0.19 point-cloud reader.

Open3D's Python API returns a cloud even when its native PLY reader fails.
Validate the complete stream before allowing that reader to allocate a cloud.
This module does not construct geometry or replace Open3D's property decoding.
"""

import math
import os
import re

import numpy as np


_CHUNK = 64 * 1024
_HEADER_LIMIT = 1024 * 1024
_TOKEN_LIMIT = 256
_FLOAT32_HALF_MIN = float(np.nextafter(np.float32(0), np.float32(1))) / 2
_INTEGER = re.compile(rb"[+-]?[0-9]+\Z")
_REAL = re.compile(rb"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
_TYPES = {}
for names, code in ((('char', 'int8'), 'b'), (('uchar', 'uint8'), 'B'),
                    (('short', 'int16'), 'h'), (('ushort', 'uint16'), 'H'),
                    (('int', 'int32'), 'i'), (('uint', 'uint32'), 'I'),
                    (('float', 'float32'), 'f'), (('double', 'float64'), 'd')):
    for name in names:
        _TYPES[name] = np.dtype(code)


def _invalid(message):
    raise ValueError(f"Invalid or unsupported PLY: {message}")


def _type(name, *, integer=False):
    if name not in _TYPES or (integer and _TYPES[name].kind not in 'iu'):
        _invalid(f"unsupported {'list count ' if integer else ''}type {name!r}")
    return _TYPES[name]


def _header(stream):
    total = 0
    cr_only = None

    def line():
        nonlocal total, cr_only
        raw = bytearray()
        while True:
            char = stream.read(1)
            total += len(char)
            if total > _HEADER_LIMIT or len(raw) > _CHUNK:
                _invalid("oversized header")
            if not char:
                _invalid("incomplete header")
            if char == b'\n':
                return bytes(raw)
            if char == b'\r':
                # Remember CR-only headers so a first binary body byte 0x0A
                # is not mistaken for the second half of CRLF.
                if cr_only is not True:
                    following = stream.read(1)
                    if following == b'\n':
                        total += 1
                        cr_only = False
                    else:
                        if following:
                            stream.seek(-1, os.SEEK_CUR)
                        cr_only = True
                return bytes(raw)
            raw.extend(char)

    if line() != b'ply':
        _invalid("expected 'ply' magic")
    encoding, elements = None, []
    names = set()
    property_names = set()
    while True:
        raw = line()
        words = raw.split()
        if not words or words[0] in (b'comment', b'obj_info'):
            continue
        try:
            words = [word.decode('ascii') for word in words]
        except UnicodeDecodeError:
            _invalid("non-ASCII header declaration")
        if any(any(ord(char) < 33 or ord(char) > 126 for char in word) for word in words):
            _invalid("control character in header declaration")
        if words[0] == 'end_header' and len(words) == 1:
            break
        if words[0] == 'format' and len(words) == 3 and not elements and encoding is None:
            if words[1] not in ('ascii', 'binary_little_endian', 'binary_big_endian') or words[2] != '1.0':
                _invalid("only ASCII/binary PLY 1.0 is supported")
            encoding = words[1]
        elif words[0] == 'element' and len(words) == 3 and encoding is not None:
            if not words[2].isascii() or not words[2].isdecimal() or len(words[2]) > 20:
                _invalid("invalid element count")
            count = int(words[2])
            if words[1] in names or count > 2**31 - 1:
                _invalid("duplicate element or count outside Open3D's supported range")
            names.add(words[1])
            elements.append((words[1], count, []))
            property_names = set()
        elif words[0] == 'property' and elements:
            if len(words) == 3:
                prop = (words[2], _type(words[1]), None)
            elif len(words) == 5 and words[1] == 'list':
                prop = (words[4], _type(words[3]), _type(words[2], integer=True))
            else:
                _invalid("malformed property declaration")
            if prop[0] in property_names:
                _invalid("duplicate property")
            property_names.add(prop[0])
            elements[-1][2].append(prop)
        else:
            _invalid(f"malformed header declaration {words[0]!r}")
    if encoding is None or any(count and not props for _, count, props in elements):
        _invalid("missing format or element properties")
    vertices = next((element for element in elements if element[0] == 'vertex'), None)
    if vertices is None or vertices[1] == 0:
        _invalid("no vertices")
    properties = {name: (index, count_type) for index, (name, _, count_type) in enumerate(vertices[2])}
    for group in (('x', 'y', 'z'), ('nx', 'ny', 'nz'), ('red', 'green', 'blue')):
        present = [name in properties for name in group]
        if group[0] == 'x' or any(present):
            if not all(present) or any(properties[name][1] is not None for name in group):
                _invalid(f"incomplete or list-valued vertex properties {group}")
            # Open3D 0.19 advances each group's row index on z/nz/blue, so a
            # different order can corrupt data or address a row past the array.
            if properties[group[-1]][0] != max(properties[name][0] for name in group):
                _invalid(f"Open3D 0.19 requires {group[-1]} after {group[:2]}")
    return encoding, elements, vertices[1]


def _ascii_tokens(stream):
    carry = b''
    populated = False
    while True:
        chunk = stream.read(_CHUNK)
        if not chunk:
            if carry:
                yield carry
                populated = True
            if populated:
                yield None
            return
        data = carry + chunk
        carry = b''
        for match in re.finditer(rb'[^\s]+|[\r\n]', data):
            token = match.group()
            if token in (b'\r', b'\n'):
                if populated:
                    yield None  # one nonempty ASCII element record per line
                    populated = False
                continue
            if len(token) > _TOKEN_LIMIT:
                _invalid("numeric token exceeds 256 bytes")
            if match.end() == len(data):
                carry = token
            else:
                populated = True
                yield token


def _ascii_value(tokens, dtype):
    token = next(tokens, None)
    if token is None:
        _invalid("truncated body or incomplete ASCII element record")
    if dtype.kind in 'iu':
        if not _INTEGER.fullmatch(token):
            _invalid("invalid integer in body")
        value = int(token)
        limits = np.iinfo(dtype)
        if value < limits.min or value > limits.max:
            _invalid("integer outside declared type range")
    else:
        if not _REAL.fullmatch(token):
            _invalid("invalid or non-finite real in body")
        value = float(token)
        if not math.isfinite(value) or abs(value) > float(np.finfo(dtype).max):
            _invalid("real outside finite declared type range")
        mantissa = token.lower().split(b'e')[0]
        if value == 0 and any(c in b'123456789' for c in mantissa):
            _invalid("real underflows double precision")
        if dtype.itemsize == 4 and value != 0 and abs(value) <= _FLOAT32_HALF_MIN:
            _invalid("real underflows declared float type")
    return value


def _binary_values(stream, dtype, count, remaining):
    if count < 0 or count > remaining() // dtype.itemsize:
        _invalid("truncated binary scalar/list data")
    while count:
        length = min(count, max(1, _CHUNK // dtype.itemsize))
        data = stream.read(length * dtype.itemsize)
        if len(data) != length * dtype.itemsize:
            _invalid("truncated binary body")
        values = np.frombuffer(data, dtype=dtype)
        if dtype.kind == 'f' and not np.isfinite(values).all():
            _invalid("non-finite binary value")
        yield values
        count -= length


def validate_ply(stream):
    """Validate the already locked, seekable input; return declared vertex count.

    Additional elements/properties are checked and discarded in bounded chunks.
    No allocation uses a file-declared vertex/list count. A second native read is
    necessary because Open3D's Python API does not expose its reader's status.
    """
    stream.seek(0)
    encoding, elements, vertices = _header(stream)
    size = os.fstat(stream.fileno()).st_size
    remaining = lambda: size - stream.tell()
    binary = encoding != 'ascii'
    # Reject malicious counts before Open3D allocates, including lists' minimum
    # count field. ASCII scalars need at least a one-byte token each.
    minimum = sum(count * sum((count_type if count_type is not None else dtype).itemsize if binary else 1
                             for _, dtype, count_type in props)
                  for _, count, props in elements)
    if minimum > remaining():
        _invalid("declared element counts exceed available body data")
    tokens = _ascii_tokens(stream) if not binary else None
    endian = '<' if encoding == 'binary_little_endian' else '>'
    for name, count, props in elements:
        if not count:
            continue
        if binary and all(count_type is None for _, _, count_type in props):
            dtype = np.dtype([(str(i), dtype.newbyteorder(endian)) for i, (_, dtype, _) in enumerate(props)])
            # Structured records retain property order and never include padding.
            batch = max(1, _CHUNK // dtype.itemsize)
            while count:
                length = min(count, batch)
                data = stream.read(length * dtype.itemsize)
                if len(data) != length * dtype.itemsize:
                    _invalid(f"truncated binary {name} element")
                rows = np.frombuffer(data, dtype=dtype)
                for i, (_, field_type, _) in enumerate(props):
                    if field_type.kind == 'f' and not np.isfinite(rows[str(i)]).all():
                        _invalid("non-finite binary value")
                count -= length
            continue
        for _ in range(count):
            for prop, dtype, count_type in props:
                dtype = dtype.newbyteorder(endian) if binary else dtype
                if count_type is None:
                    if binary:
                        next(_binary_values(stream, dtype, 1, remaining))
                    else:
                        _ascii_value(tokens, dtype)
                    continue
                if binary:
                    count_type = count_type.newbyteorder(endian)
                    data = stream.read(count_type.itemsize)
                    if len(data) != count_type.itemsize:
                        _invalid("truncated binary list count")
                    length = int(np.frombuffer(data, dtype=count_type)[0])
                else:
                    length = _ascii_value(tokens, count_type)
                if length < 0:
                    _invalid("negative list length")
                if dtype.kind not in 'iu' and name == 'face' and prop in ('vertex_indices', 'vertex_index'):
                    _invalid("face vertex indices must be integers")
                if binary:
                    for values in _binary_values(stream, dtype, length, remaining):
                        if name == 'face' and prop in ('vertex_indices', 'vertex_index') and ((values < 0).any() or (values >= vertices).any()):
                            _invalid("face vertex index outside vertex array")
                else:
                    # Bound the loop by actual file size, not a malicious count.
                    if length > size:
                        _invalid("list length exceeds available body data")
                    for _ in range(length):
                        value = _ascii_value(tokens, dtype)
                        if name == 'face' and prop in ('vertex_indices', 'vertex_index') and not 0 <= value < vertices:
                            _invalid("face vertex index outside vertex array")
            if not binary and next(tokens, 'EOF') is not None:
                _invalid("extra values in ASCII element record")
    if (binary and stream.read(1)) or (not binary and next(tokens, None) is not None):
        _invalid("undeclared trailing body data")
    return vertices
