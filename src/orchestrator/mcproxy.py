import json
import struct
import asyncio
import logging
from contextlib import asynccontextmanager
logger = logging.getLogger(__name__)

MINECRAFT_PORT = 25565
SERVER_VERSION = "1.21.4"

@asynccontextmanager
async def mc_proxy_server(mc_server):
    logger.info(f"Starting minecraft proxy server for {mc_server.name}:{mc_server.port} on port {MINECRAFT_PORT}")
    proxy_server = await asyncio.start_server(handle_client(mc_server), "0.0.0.0", MINECRAFT_PORT)
    async with proxy_server:
        yield proxy_server

def handle_client(mc_server):
    async def inner(reader, writer):
        try:
            # Forward traffic to actual minecraft server
            backend_reader, backend_writer = await asyncio.open_connection("0.0.0.0", mc_server.port)
            try:
                async def forward(src_reader, dst_writer):
                    try:
                        while True:
                            data = await src_reader.read(4096)
                            if not data:
                                break
                            dst_writer.write(data)
                            await dst_writer.drain()
                    except Exception:
                        pass
                    finally:
                        dst_writer.close()

                await asyncio.gather(
                    forward(reader, backend_writer),
                    forward(backend_reader, writer)
                )
            except Exception as err:
                logger.exception(f"{mc_server.name} proxy error: {err}")
            finally:
                writer.close()
        except ConnectionRefusedError as err:
            data = await reader.read(1024)

            # Handle legacy pings
            try:
                _, legacy_ping = decode_legacy_ping(data)
                legacy_ping_response = encode_legacy_ping_response(legacy_ping["protocol_version"], SERVER_VERSION, mc_server.properties["motd"].data, mc_server.max_players)
                writer.write(legacy_ping_response)
                await writer.drain()
                writer.close()
                return
            except ValueError as err:
                pass

            # Handle modern handshake
            try:
                # Read initial handshake packet
                _, handshake = decode_handshake_packet(data)
                if handshake["next_state"] == 1:
                    # Read request packet and respond
                    data = await reader.read(1024)
                    decode_request_packet(data)

                    # Status request
                    handshake_response_paylod = {
                        "version": {
                            "name": SERVER_VERSION,
                            "protocol": handshake["protocol_version"]
                        },
                        "players": {
                            "max": mc_server.max_players,
                            "online": 0,
                            "sample": []
                        },	
                        "description": {
                            "text": mc_server.motd
                        }
                    }
                    if mc_server.favicon_data is not None:
                        handshake_response_paylod["favicon"] = mc_server.favicon_data
                    handshake_response = encode_json_packet(0, handshake_response_paylod)
                    writer.write(handshake_response)
                    await writer.drain()

                    # Read ping packet and respond with pong
                    data = await reader.read(1024)
                    _, ping_payload = decode_ping_packet(data)
                    logger.info(f"Ping packet payload: {ping_payload}")
                    pong_packet = encode_pong_packet(ping_payload)
                    writer.write(pong_packet)
                    await writer.drain()

                    # Done with status request
                    writer.close()
                elif handshake["next_state"] == 2:
                    # Login attempt
                    kick_payload = {
                        "text": "§eServer is waking up, try again in 30s"
                    }
                    handshake_response = encode_json_packet(0, kick_payload)
                    writer.write(handshake_response)
                    await writer.drain()
                    writer.close()

                    await mc_server.start()
                else:
                    raise ValueError(f"Unknown next state in handshake: {handshake['next_state']}")
            except ValueError as err:
                logger.info(f"Error during handshake: {err}")
                writer.close()
                return
    return inner

############################################ Minecraft protocol ############################################
############################################# Decode functions #############################################

def decode_legacy_ping(data: bytes) -> tuple[int, dict]:
    try:
        i = 0
        if data[i:i+3] != b"\xfe\x01\xfa":
            raise ValueError("Bad header")
        i += 3
        str_length = struct.unpack(">H", data[i:i+2])[0]
        i += 2
        string = data[i:i+str_length*2].decode("utf-16-be")
        if string != "MC|PingHost":
            raise ValueError(f"Expected string \"MC|PingHost\", received \"{string}\"")
        i += str_length*2
        remaining_length = struct.unpack(">H", data[i:i+2])[0]
        i += 2
        if len(data[i:]) != remaining_length:
            raise ValueError(f"Expected {remaining_length} bytes of ping data, received {len(data[i:])} bytes")
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
    except (ValueError, struct.error) as err:
        raise ValueError(f"Malformed legacy ping: {err}")

def decode_varint(data: bytes) -> tuple[int, int]:
    length = 0
    shift = 0
    num = 0
    for b in data:
        length += 1
        if length > 5:
            raise ValueError("VarInt is too big")

        num |= (b & 0b01111111) << shift
        shift += 7

        if (b & 0b10000000) == 0:
            break

    if num > (1 << 31):
        num -= 1 << 32
    return length, num

def decode_string(data: bytes) -> tuple[int, str]:
    varint_length, str_length = decode_varint(data)
    return str_length + varint_length, data[varint_length:varint_length + str_length].decode("utf-8")

def decode_packet(data: bytes) -> tuple[int, tuple[int, bytes]]:
    i, total_length = decode_varint(data)
    if len(data[i:]) != total_length:
        raise ValueError(f"Expected {total_length} bytes of packet data, received {len(data[i:])} bytes")
    n, packet_id = decode_varint(data[i:])
    packet_data = data[i+n:]
    return (i+total_length), (packet_id, packet_data)

def decode_handshake_packet(data: bytes) -> tuple[int, dict]:
    try:
        n, (packet_id, packet_data) = decode_packet(data)
        if packet_id != 0:
            raise ValueError(f"Expected packet id 0 but got {packet_id}")
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
            raise ValueError("Extra data")
        return n, {
            "protocol_version": protocol_version,
            "server_address": server_address,
            "server_port": server_port,
            "next_state": next_state
        }
    except (ValueError, struct.error) as err:
        raise ValueError(f"Malformed handshake packet: {err}") from err

def decode_request_packet(data: bytes) -> tuple[int, None]:
    try:
        n, (packet_id, packet_data) = decode_packet(data)
        if packet_id != 0:
            raise ValueError(f"Expected packet id 0 but got {packet_id}")
        if len(packet_data) != 0:
            raise ValueError(f"Extra data")
        return n, None
    except (ValueError, struct.error) as err:
        raise ValueError(f"Malformed request packet: {err}") from err

def decode_ping_packet(data: bytes) -> tuple[int, int]:
    try:
        n, (packet_id, packet_data) = decode_packet(data)
        if packet_id != 1:
            raise ValueError(f"Expected packet id 1 but got {packet_id}")
        if len(packet_data) != 8:
            raise ValueError(f"Expected 8 bytes of payload but received {len(packet_data)}")
        payload = struct.unpack(">q", packet_data)[0]
        return n, payload
    except (ValueError, struct.error) as err:
        raise ValueError(f"Malformed ping packet: {err}") from err

############################################# Encode functions #############################################

def encode_legacy_ping_response(protocol_version: int, mc_version: str, motd: str, max_players: int) -> bytes:
    current_player_count = 0
    string = f"§1\x00{protocol_version}\x00{mc_version}\x00{motd}\x00{current_player_count}\x00{max_players}"
    str_length = len(string)
    return b"\xff" + struct.pack(">H", min(str_length, 65535)) + string.encode("utf-16-be")

def encode_varint(value: int) -> bytes:
    if value > (1 << 31) - 1 or value < ((1 << 31) - (1 << 32)):
        raise ValueError("Number out of range of 4 byte int")
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