#!/usr/bin/env python3
"""Apply a patch file to a Leica M11-D firmware file.

Usage:
  python3 apply.py m11-d_2.6.4.patch LEICA_M11-D_2.6.4.FW

It writes two files next to the firmware file:
  LEICA_M11-D_2.6.4_aelock.FW          the firmware with the fix
  LEICA_M11-D_2.6.4_back_to_stock.FW   Leica's firmware, in a form that a camera with the fix accepts

README.md explains the patch and the firmware file format.
"""
import hashlib
import os
import re
import struct
import sys

PART_ENTRY_SIZE = 0x38


def read_patch(path):
    """Return the settings and the list of (address, stock word, fixed word)."""
    settings, words = {}, []
    for line in open(path):
        fields = line.split()
        if not fields or fields[0].startswith("#"):
            continue
        if re.fullmatch("[0-9a-f]{8}", fields[0]):
            words.append(tuple(int(field, 16) for field in fields[:3]))
        else:
            settings[fields[0]] = fields[1]
    return settings, words


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def md5(data):
    return hashlib.md5(data).digest()


# The packed data is a list of commands:
#   marker, 0                    one plain byte that has the marker's value
#   marker, length, distance     copy `length` bytes, starting `distance` bytes back in the output
#   any other byte               a plain byte
# The first byte of the packed data says which byte value is the marker.

def read_number(packed, pos):
    """Read a length or a distance. It is stored 7 bits per byte, high bits first.
    A byte of 0x80 or more means that one more byte follows."""
    value = 0
    while True:
        byte = packed[pos]
        pos += 1
        value = value << 7 | byte & 0x7F
        if byte < 0x80:
            return value, pos


def commands(packed):
    """Yield (start, end, length, distance) for each command.

    The command is packed[start:end]. It gives `length` bytes of output.
    A distance of 0 means plain bytes.
    """
    marker = packed[0]
    pos = 1
    while pos < len(packed):
        if packed[pos] != marker:               # plain bytes, up to the next marker
            end = packed.find(marker, pos)
            if end < 0:
                end = len(packed)
            yield pos, end, end - pos, 0
        elif packed[pos + 1] == 0:              # marker, 0
            end = pos + 2
            yield pos, end, 1, 0
        else:                                   # marker, length, distance
            length, end = read_number(packed, pos + 1)
            distance, end = read_number(packed, end)
            yield pos, end, length, distance
        pos = end


def copy_result(data, at, distance, length):
    """The bytes that a copy command gives when the output is `at` bytes long."""
    source = bytes(data[at - distance:at - distance + min(distance, length)])
    # A copy can be longer than its distance. It then reads bytes that it wrote itself.
    return (source * (length // distance + 1))[:length]


def unpack(packed):
    out = bytearray()
    for start, end, length, distance in commands(packed):
        if distance:
            out += copy_result(out, len(out), distance, length)
        else:
            # For "marker, 0" this takes the first byte only, which is the marker's value.
            out += packed[start:start + length]
    return out


def repack(packed, body):
    """Pack `body` with the commands of the stock file.

    A stock copy command is kept if it still gives the right bytes.
    If it does not, the bytes are written as plain bytes.
    """
    marker = packed[:1]
    out = bytearray(marker)
    at = 0
    for start, end, length, distance in commands(packed):
        wanted = bytes(body[at:at + length])
        if distance and copy_result(body, at, distance, length) == wanted:
            out += packed[start:end]
        else:
            out += wanted.replace(marker, marker + b"\0")
        at += length
    return bytes(out)


def patch_body(body, part, base, words):
    """Write the fixed words into one part. Then write the two MD5 sums that cover them."""
    table, part_count, table_size = struct.unpack_from("<III", body, 8)
    entry = table + part * PART_ENTRY_SIZE
    offset, size = struct.unpack_from("<II", body, entry + 0x18)
    start = table + table_size + offset         # part offsets count from the end of the table
    for address, stock, fixed in words:
        at = start + address - base
        if struct.unpack_from("<I", body, at)[0] != stock:
            sys.exit("The firmware does not hold the expected word at %08x." % address)
        struct.pack_into("<I", body, at, fixed)
    body[entry + 0x20:entry + 0x30] = md5(body[start:start + size])
    body[0x14:0x24] = md5(body[table:table + table_size])


def make_header(stock_header, packed, label=None):
    header = bytearray(stock_header)
    struct.pack_into("<I", header, 0x14, len(packed))
    header[0x18:0x28] = md5(packed)
    # +0x30 is the version of this firmware. Stock has 0 at +0x34 and +0x38.
    # The camera then accepts the file only if the version is higher than its own.
    # With the version copied to +0x34 and +0x38, the camera accepts the file
    # if it runs exactly this version.
    header[0x34:0x38] = header[0x38:0x3C] = header[0x30:0x34]
    if label:
        if len(label) > 7:
            sys.exit("The label can have at most 7 characters.")
        header[0x3C:0x44] = (label.encode() + b"\0").ljust(8, b"\xaa")
    return bytes(header)


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    settings, words = read_patch(sys.argv[1])
    firmware_path = sys.argv[2]
    firmware = open(firmware_path, "rb").read()
    if sha256(firmware) != settings["stock-sha256"]:
        sys.exit("This is not the firmware file that the patch was made for.\n"
                 "  expected SHA-256 %s\n  this file        %s" % (settings["stock-sha256"], sha256(firmware)))

    header_size = struct.unpack_from("<I", firmware, 4)[0]
    stock_header, packed = firmware[:header_size], firmware[header_size:]

    print("Unpacking.", flush=True)
    body = unpack(packed)
    patch_body(body, int(settings["part"]), int(settings["base"], 16), words)
    print("Packing.", flush=True)
    fixed_packed = repack(packed, body)

    outputs = [
        ("_aelock.FW", make_header(stock_header, fixed_packed, settings["label"]) + fixed_packed,
         settings["fixed-sha256"]),
        ("_back_to_stock.FW", make_header(stock_header, packed) + packed,
         settings["back-to-stock-sha256"]),
    ]
    for suffix, data, expected in outputs:
        if sha256(data) != expected:
            sys.exit("The result for %s is not the expected file. Nothing was written.\n"
                     "  expected SHA-256 %s\n  result           %s" % (suffix, expected, sha256(data)))
    for suffix, data, expected in outputs:
        path = os.path.splitext(firmware_path)[0] + suffix
        open(path, "wb").write(data)
        print("Wrote %s (%d bytes, SHA-256 %s)" % (path, len(data), expected))


if __name__ == "__main__":
    main()
