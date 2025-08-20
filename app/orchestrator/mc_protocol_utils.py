import json
import random
import struct
import asyncio
from collections import namedtuple

############################################ Minecraft protocol ############################################
# Documentation at: https://minecraft.wiki/w/Minecraft_Wiki:Protocol_documentation

MCVersion = namedtuple("MCVersion", ["name", "protocol"])

class MCProtocolError(ValueError):
    """Protocol error.

    Represents an error in the 
    """
    def __init__(self, data, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.data = data

async def read_packets_forever(reader: asyncio.StreamReader, initial_data: bytes=b"", timeout: int | None=None):
    data = initial_data
    packet_length = None
    while True:
        # Read bytes until the packet's length can be determined
        if packet_length is None:
            try:
                varint_length, packet_length = decode_varint(data)
            except asyncio.IncompleteReadError:
                try:
                    data += await asyncio.wait_for(reader.readexactly(1), timeout=timeout)
                except asyncio.IncompleteReadError as err:
                    raise ConnectionResetError from err
                continue

        # Read up until the end of the packet
        try:
            if len(data) < varint_length + packet_length:
                data += await asyncio.wait_for(reader.readexactly(varint_length + packet_length - len(data)), timeout=timeout)
        except asyncio.IncompleteReadError as err:
            data += err.partial
            continue

        # Decode packet and yield it
        total_length, (packet_id, packet_data) = decode_packet(data)
        data = data[total_length:]  # Should always yield an empty bytes object
        packet_length = None
        yield total_length, (packet_id, packet_data)

############################################# Decode functions #############################################

def decode_legacy_ping(data: bytes) -> tuple[int, dict]:
    try:
        i = 0
        if data[i:i+3] != b"\xfe\x01\xfa":
            raise MCProtocolError(data, "Bad header")
        i += 3
        str_length = struct.unpack(">H", data[i:i+2])[0]
        i += 2
        string = data[i:i+str_length*2].decode("utf-16-be")
        if string != "MC|PingHost":
            raise MCProtocolError(data, f"Expected string \"MC|PingHost\", received \"{string}\"")
        i += str_length*2
        remaining_length = struct.unpack(">H", data[i:i+2])[0]
        i += 2
        if len(data[i:]) != remaining_length:
            raise MCProtocolError(data, f"Expected {remaining_length} bytes of ping data, received {len(data[i:])} bytes")
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
    except Exception as err:
        raise MCProtocolError(data, f"Malformed legacy ping: {err}") from err

def decode_varint(data: bytes) -> tuple[int, int]:
    num = 0
    shift = 0
    i = 0
    while True:
        if i + 1 > 5:
            raise MCProtocolError(data, "VarInt is too big")
        if i > len(data) - 1:
            raise asyncio.IncompleteReadError(data, None)
        b = data[i]

        num |= ((b & 0b01111111) << shift)
        shift += 7

        if (b & 0b10000000) == 0:
            break
        i += 1
    num &= 0xffffffff  # Convert back to 4 bytes

    # Check if negative
    if num > (1 << 31):
        num -= 1 << 32
    return i + 1, num

def decode_string(data: bytes) -> tuple[int, str]:
    varint_length, str_length = decode_varint(data)
    if len(data) < varint_length + str_length:
        raise asyncio.IncompleteReadError(data, str_length + varint_length)
    return varint_length + str_length, data[varint_length:varint_length + str_length].decode("utf-8")

def decode_packet(data: bytes) -> tuple[int, tuple[int, bytes]]:
    varint_length, total_packet_length = decode_varint(data)
    if len(data) < varint_length + total_packet_length:
        raise asyncio.IncompleteReadError(data, total_packet_length)
    packet_id_length, packet_id = decode_varint(data[varint_length:])
    packet_data = data[varint_length + packet_id_length : varint_length + total_packet_length]
    return (varint_length+total_packet_length), (packet_id, packet_data)

def decode_handshake_packet(packet: tuple[int, tuple[int, bytes]]) -> tuple[int, dict]:
    try:
        n, (packet_id, packet_data) = packet
        if packet_id != 0:
            raise MCProtocolError(packet, f"Expected packet id 0 but got {packet_id}")
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
            raise MCProtocolError(packet, f"Extra data")
        return n, {
            "protocol_version": protocol_version,
            "server_address": server_address,
            "server_port": server_port,
            "next_state": next_state
        }
    except (MCProtocolError, struct.error) as err:
        raise MCProtocolError(packet, f"Malformed handshake packet: {err}") from err

def decode_request_packet(packet: tuple[int, tuple[int, bytes]]) -> tuple[int, None]:
    try:
        n, (packet_id, packet_data) = packet
        if packet_id != 0:
            raise MCProtocolError(packet, f"Expected packet id 0 but got {packet_id}")
        if len(packet_data) != 0:
            raise MCProtocolError(packet, f"Extra data")
        return n, None
    except Exception as err:
        raise MCProtocolError(f"Malformed request packet: {err}") from err

def decode_json_packet(packet: tuple[int, tuple[int, bytes]]) -> tuple[int, tuple[int, dict]]:
    try:
        n, (packet_id, packet_data) = packet
        _, json_string = decode_string(packet_data)
        return n, (packet_id, json.loads(json_string))
    except Exception as err:
        raise MCProtocolError(packet, f"Malformed json packet: {err}") from err

def decode_pingpong_packet(packet: tuple[int, tuple[int, bytes]]) -> tuple[int, int]:
    try:
        n, (packet_id, packet_data) = packet
        if packet_id != 1:
            raise MCProtocolError(packet, f"Expected packet id 1 but got {packet_id}")
        if len(packet_data) != 8:
            raise MCProtocolError(packet, f"Expected 8 bytes of payload but received {len(packet_data)}")
        payload = struct.unpack(">q", packet_data)[0]
        return n, payload
    except Exception as err:
        raise MCProtocolError(packet, f"Malformed ping/pong packet: {err}") from err

############################################# Encode functions #############################################

def encode_legacy_ping_response(protocol_version: int, mc_version: str, motd: str, max_players: int) -> bytes:
    current_player_count = 0
    string = f"§1\x00{protocol_version}\x00{mc_version}\x00{motd}\x00{current_player_count}\x00{max_players}"
    str_length = len(string)
    return b"\xff" + struct.pack(">H", min(str_length, 65535)) + string.encode("utf-16-be")

def encode_varint(value: int) -> bytes:
    # if value > (1 << 31) - 1 or value < ((1 << 31) - (1 << 32)):
    #     raise ValueError("Number out of range of 4 byte int")
    if value < 0:
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

def encode_handshake_packet(protocol_version: int, server_address: str, server_port: int, next_state: int=1) -> bytes:
    return encode_packet(0, encode_varint(protocol_version) + encode_string(server_address) + struct.pack(">H", server_port) + encode_varint(next_state))

def encode_request_packet():
    return encode_packet(0, b'')

def encode_json_packet(packet_id: int, payload: dict) -> bytes:
    return encode_packet(packet_id, encode_string(json.dumps(payload)))

def encode_pingpong_packet(payload: int) -> bytes:
    return encode_packet(1, struct.pack(">q", payload))

############################################################################################################

def random_ping_payload():
    return struct.unpack(">q", random.randbytes(8))[0]