import json
import struct
import asyncio
from contextlib import suppress

############################################ Minecraft protocol ############################################
# Documentation at: https://minecraft.wiki/w/Minecraft_Wiki:Protocol_documentation

class ProtocolError(ValueError):
    pass

async def read_packets_forever(initial_data, reader):
    data = initial_data
    packet_length = None
    while True:
        if packet_length is None:
            try:
                n, packet_length = decode_varint(data)
            except asyncio.IncompleteReadError:
                data += await reader.readexactly(1)
                continue

        try:
            if len(data) < n + packet_length:
                data += await reader.readexactly(packet_length)
        except asyncio.IncompleteReadError as err:
            data += err.partial
            packet_length -= len(err.partial)
            continue

        n, (packet_id, packet_data) = decode_packet(data)
        data = data[n:]
        packet_length = None
        yield n, (packet_id, packet_data)

############################################# Decode functions #############################################

def decode_legacy_ping(data: bytes) -> tuple[int, dict]:
    try:
        i = 0
        if data[i:i+3] != b"\xfe\x01\xfa":
            raise ProtocolError("Bad header")
        i += 3
        str_length = struct.unpack(">H", data[i:i+2])[0]
        i += 2
        string = data[i:i+str_length*2].decode("utf-16-be")
        if string != "MC|PingHost":
            raise ProtocolError(f"Expected string \"MC|PingHost\", received \"{string}\"")
        i += str_length*2
        remaining_length = struct.unpack(">H", data[i:i+2])[0]
        i += 2
        if len(data[i:]) != remaining_length:
            raise ProtocolError(f"Expected {remaining_length} bytes of ping data, received {len(data[i:])} bytes")
        protocol_version = struct.unpack(">B", data[i:i+1])[0]
        i += 1
        hostname_length = struct.unpack(">H", data[i:i+2])[0]
        i += 2
        hostname = data[i:i+hostname_length*2].decode("utf-16-be")
        i += hostname_length*2
        port = struct.unpack(">I", data[i:i+4])[0]
        i += 4
        return i+1, {
            "protocol_version": protocol_version,
            "hostname": hostname,
            "port": port
        }
    except (ProtocolError, struct.error) as err:
        raise ProtocolError(f"Malformed legacy ping: {err}")

def decode_varint(data: bytes) -> tuple[int, int]:
    i = 0
    shift = 0
    num = 0
    while True:
        if i > len(data) - 1:
            raise asyncio.IncompleteReadError(data, None)
        if i + 1 > 4:
            raise ProtocolError("VarInt is too big")
        b = data[i]

        num |= (b & 0b01111111) << shift
        shift += 7

        if (b & 0b10000000) == 0:
            break
        i += 1

    # Check if negative
    if num > (1 << 31):
        num -= 1 << 32
    return i + 1, num

def decode_string(data: bytes) -> tuple[int, str]:
    varint_length, str_length = decode_varint(data)
    return str_length + varint_length, data[varint_length:varint_length + str_length].decode("utf-8")

def decode_packet(data: bytes) -> tuple[int, tuple[int, bytes]]:
    i, total_length = decode_varint(data)
    if len(data[i:]) < total_length:
        raise ProtocolError(f"Expected {total_length} bytes of packet data, received {len(data[i:])} bytes")
    n, packet_id = decode_varint(data[i:])
    packet_data = data[i+n:i+total_length]
    return (i+total_length), (packet_id, packet_data)

def decode_handshake_packet(data: bytes | tuple[int, tuple[int, bytes]]) -> tuple[int, dict]:
    try:
        if isinstance(data, bytes):
            data = decode_packet(data)
        n, (packet_id, packet_data) = data
        if packet_id != 0:
            raise ProtocolError(f"Expected packet id 0 but got {packet_id}")
        i = 0
        n, protocol_version = decode_varint(packet_data)
        i += n
        n, server_address = decode_string(packet_data[i:])
        i += n
        server_port = struct.unpack(">H", packet_data[i:i+2])[0]
        i += 2
        n, next_state = decode_varint(packet_data[i:])
        i += n
        if i != len(packet_data):
            raise ProtocolError(f"Extra data")
        return n, {
            "protocol_version": protocol_version,
            "server_address": server_address,
            "server_port": server_port,
            "next_state": next_state
        }
    except (ProtocolError, struct.error) as err:
        raise ProtocolError(f"Malformed handshake packet: {err}") from err

def decode_request_packet(data: bytes | tuple[int, tuple[int, bytes]]) -> tuple[int, None]:
    try:
        if isinstance(data, bytes):
            data = decode_packet(data)
        n, (packet_id, packet_data) = data
        if packet_id != 0:
            raise ProtocolError(f"Expected packet id 0 but got {packet_id}")
        if len(packet_data) != 0:
            raise ProtocolError(f"Extra data")
        return n, None
    except (ProtocolError, struct.error) as err:
        raise ProtocolError(f"Malformed request packet: {err}") from err

def decode_ping_packet(data: bytes | tuple[int, tuple[int, bytes]]) -> tuple[int, int]:
    try:
        if isinstance(data, bytes):
            data = decode_packet(data)
        n, (packet_id, packet_data) = data
        if packet_id != 1:
            raise ProtocolError(f"Expected packet id 1 but got {packet_id}")
        if len(packet_data) != 8:
            raise ProtocolError(f"Expected 8 bytes of payload but received {len(packet_data)}")
        payload = struct.unpack(">q", packet_data)[0]
        return n, payload
    except (ProtocolError, struct.error) as err:
        raise ProtocolError(f"Malformed ping packet: {err}") from err

############################################# Encode functions #############################################

def encode_legacy_ping_response(protocol_version: int, mc_version: str, motd: str, max_players: int) -> bytes:
    current_player_count = 0
    string = f"§1\x00{protocol_version}\x00{mc_version}\x00{motd}\x00{current_player_count}\x00{max_players}"
    str_length = len(string)
    return b"\xff" + struct.pack(">H", min(str_length, 65535)) + string.encode("utf-16-be")

def encode_varint(value: int) -> bytes:
    if value > (1 << 31) - 1 or value < ((1 << 31) - (1 << 32)):
        raise ProtocolError("Number out of range of 4 byte int")
    if value < 1:
        value += 1 << 32
    out = b""
    while True:
        temp = value & 0x7F  # Grab last 7 bits only
        value >>= 7  # Check if there is more data
        if value:
            out += struct.pack("B", temp | 0x80)  # Set continuation bit to 1 and pack last 7 bits
        else:
            out += struct.pack("B", temp)  # Pack last 7 bits, continuation bit is set to 0
            break
    return out

def encode_string(value: str) -> bytes:
    str_data = value.encode()
    return encode_varint(len(str_data)) + str_data

def encode_packet(packet_id, packet_data: bytes) -> bytes:
    packet_id_data = encode_varint(packet_id)
    n = len(packet_id_data) + len(packet_data)
    return encode_varint(n) + packet_id_data + packet_data

def encode_json_packet(packet_id: int, payload: dict) -> bytes:
    return encode_packet(packet_id, encode_string(json.dumps(payload)))

def encode_pong_packet(payload: int) -> bytes:
    return encode_packet(1, struct.pack(">q", payload))

############################################################################################################