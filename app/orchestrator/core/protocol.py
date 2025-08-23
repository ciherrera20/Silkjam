import json
import random
import struct
import asyncio
import logging
from collections import namedtuple
from collections.abc import Buffer

logger = logging.getLogger(__name__)

################################################ Minecraft protocol ################################################
# Documentation at: https://minecraft.wiki/w/Minecraft_Wiki:Protocol_documentation

MCVersion = namedtuple("MCVersion", ["name", "protocol"])
type Packet[T: Buffer] = tuple[int, tuple[int, T]]

class MCProtocolError(ValueError):
    """Protocol error.

    Represents an error in the 
    """
    def __init__(self, data: Buffer | Packet, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.data = data

class PacketReader:
    DEFAULT_BUFFER_SIZE = 1 << 16
    MIN_READ_SIZE = 1 << 10

    ############################################# Decode functions #############################################

    @staticmethod
    def decode_legacy_ping(data: Buffer) -> tuple[int, dict]:
        try:
            if len(data) < 3 + 2 + 11 + 2:
                raise asyncio.IncompleteReadError(data, None)

            # Read header
            i = 0
            if data[i:i+3] != b"\xfe\x01\xfa":
                raise MCProtocolError(data, "Bad header")
            i += 3

            # Read string
            str_length = struct.unpack(">H", data[i:i+2])[0]
            i += 2
            string = str(data[i:i+str_length*2], "utf-16-be")
            if string != "MC|PingHost":
                raise MCProtocolError(data, f"Expected string \"MC|PingHost\", received \"{string}\"")
            i += str_length*2

            # Read remaining length
            remaining_length = struct.unpack(">H", data[i:i+2])[0]
            i += 2
            if len(data[i:]) < remaining_length:
                raise asyncio.IncompleteReadError(data, i + remaining_length)

            # Read remaining fields
            protocol_version = struct.unpack(">B", data[i:i+1])[0]
            i += 1

            hostname_length = struct.unpack(">H", data[i:i+2])[0]
            i += 2

            hostname = str(data[i:i+hostname_length*2], "utf-16-be")
            i += hostname_length*2

            port = struct.unpack(">I", data[i:i+4])[0]
            i += 4

            return i+1, {
                "protocol_version": protocol_version,
                "hostname": hostname,
                "port": port
            }
        except (MCProtocolError, struct.error, UnicodeDecodeError) as err:
            raise MCProtocolError(data, f"Malformed legacy ping: {err}") from err

    @staticmethod
    def decode_varint(data: Buffer) -> tuple[int, int]:
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

    @classmethod
    def decode_string(cls, data: Buffer) -> tuple[int, str]:
        try:
            varint_length, str_length = cls.decode_varint(data)
            if len(data) < varint_length + str_length:
                raise asyncio.IncompleteReadError(data, str_length + varint_length)
            return varint_length + str_length, str(data[varint_length:varint_length + str_length], "utf-8")
        except (MCProtocolError, UnicodeDecodeError) as err:
            raise MCProtocolError(data, f"Malformed string: {err}") from err

    @classmethod
    def decode_packet(cls, data: Buffer) -> Packet[Buffer]:
        try:
            varint_length, total_packet_length = cls.decode_varint(data)
            if len(data) < varint_length + total_packet_length:
                raise asyncio.IncompleteReadError(data, total_packet_length)
            packet_id_length, packet_id = cls.decode_varint(data[varint_length:])
            packet_data = data[varint_length + packet_id_length : varint_length + total_packet_length]
            return (varint_length + total_packet_length), (packet_id, packet_data)
        except MCProtocolError as err:
            raise MCProtocolError(data, f"Malformed packet: {err}") from err

    @classmethod
    def decode_handshake_packet(cls, packet: Packet[Buffer]) -> tuple[int, dict]:
        try:
            n, (packet_id, packet_data) = packet
            if packet_id != 0:
                raise MCProtocolError(packet, f"Expected packet id 0 but got {packet_id}")
            i = 0
            n, protocol_version = cls.decode_varint(packet_data)
            i += n
            n, server_address = cls.decode_string(packet_data[i:])
            i += n
            server_port = struct.unpack(">H", packet_data[i:i+2])[0]
            i += 2
            n, next_state = cls.decode_varint(packet_data[i:])
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

    @staticmethod
    def decode_request_packet(packet: Packet[Buffer]) -> tuple[int, None]:
        try:
            n, (packet_id, packet_data) = packet
            if packet_id != 0:
                raise MCProtocolError(packet, f"Expected packet id 0 but got {packet_id}")
            if len(packet_data) != 0:
                raise MCProtocolError(packet, f"Extra data")
            return n, None
        except MCProtocolError as err:
            raise MCProtocolError(packet, f"Malformed request packet: {err}") from err

    @classmethod
    def decode_json_packet(cls, packet: Packet[Buffer]) -> tuple[int, tuple[int, dict]]:
        try:
            n, (packet_id, packet_data) = packet
            _, json_string = cls.decode_string(packet_data)
            return n, (packet_id, json.loads(json_string))
        except (MCProtocolError, asyncio.IncompleteReadError, json.JSONDecodeError, OverflowError) as err:
            raise MCProtocolError(packet, f"Malformed json packet: {err}") from err

    @staticmethod
    def decode_pingpong_packet(packet: Packet[Buffer]) -> tuple[int, int]:
        try:
            n, (packet_id, packet_data) = packet
            if packet_id != 1:
                raise MCProtocolError(packet, f"Expected packet id 1 but got {packet_id}")
            if len(packet_data) != 8:
                raise MCProtocolError(packet, f"Expected 8 bytes of payload but received {len(packet_data)}")
            payload = struct.unpack(">q", packet_data)[0]
            return n, payload
        except (MCProtocolError, struct.error) as err:
            raise MCProtocolError(packet, f"Malformed ping/pong packet: {err}") from err

    ############################################################################################################

    @staticmethod
    def _allocate_buffer(data: memoryview, i: int, j: int, n: int, min_buffer_size: int=DEFAULT_BUFFER_SIZE, min_free_space: int=MIN_READ_SIZE) -> tuple[memoryview, int, int, int]:
        # j - i is how much space we still have occupied
        old_data = data
        if j - i < n - (n >> 2):  # Less than 1/4 of the buffer will be occupied with unparsed data
            # Halve buffer, not going below default buffer size
            n = max(n >> 1, min_buffer_size)
        elif j - i > n - min_free_space:  # Buffer is completely occupied with unparsed data
            # Double buffer
            n <<= 1
        logger.debug("Allocating new %s byte buffer", n)
        data = memoryview(bytearray(n))
        data[:j-i] = old_data[i:j]  # Copy unparsed data
        j -= i
        i = 0
        return data, i, j, n

    def __init__(self, reader: asyncio.StreamReader, initial_data: Buffer=b"", timeout: int | None=None):
        self.reader = reader

        # Allocate buffer
        #         0    :    i     i     :     j     j   :    n
        # data = [parsed data] + [unparsed data] + [free space]
        self.i = 0                    # Start of unparsed data
        self.j = len(initial_data)    # Start of unread data
        self.n = self.DEFAULT_BUFFER_SIZE  # Size of buffer
        self.data, self.i, self.j, self.n = self._allocate_buffer(initial_data, self.i, self.j, self.n)
        self.packet_length = None
        self.timeout = timeout
        self._lock = asyncio.Lock()

    @property
    def unparsed(self) -> memoryview:
        return self.data[self.i:self.j]

    async def _read_legacy_ping(self) -> dict:
        async with self._lock:
            data, i, j, n = self.data, self.i, self.j, self.n
            try:
                while True:
                    try:
                        logger.debug("Parsing %s bytes as legacy ping", j-i)
                        total_length, legacy_ping = self.decode_legacy_ping(data[i:j])
                    except asyncio.IncompleteReadError:
                        logger.debug("Incomplete legacy ping")
                        if (n - j) < self.MIN_READ_SIZE:  # Need to allocate a new buffer
                            logger.debug("Buffer state [parsed:unparsed:free](size) = [%s:%s:%s](%s)", i, j-i, n-j, n)
                            data, i, j, n = self._allocate_buffer(data, i, j, n)
                        new_data = await self.reader.read(n - j)
                        logger.debug("Read %s bytes from client", len(new_data))
                        if len(new_data) == 0:
                            raise ConnectionResetError
                        data[j:j+len(new_data)] = new_data
                        j += len(new_data)
                    except MCProtocolError as err:
                        logger.debug("Could not parse %s bytes as legacy ping: %s", j-i, err)
                        raise
                    else:
                        i += total_length  # Update start of unparsed bytes
                        return legacy_ping
                    finally:
                        logger.debug("Buffer state [parsed:unparsed:free](size) = [%s:%s:%s](%s)", i, j-i, n-j, n)
            finally:
                self.data, self.i, self.j, self.n = data, i, j, n

    async def read_legacy_ping(self, timeout: int | None=None) -> dict:
        return await asyncio.wait_for(self._read_legacy_ping(), timeout)

    async def _read_packet(self) -> Packet[memoryview]:
        async with self._lock:
            data, i, j, n, packet_length = self.data, self.i, self.j, self.n, self.packet_length
            try:
                while True:
                    try:
                        if self.packet_length is None:
                            logger.debug("Parsing %s bytes as VarInt", j-i)
                            _, self.packet_length = self.decode_varint(data[i:j])
                    except asyncio.IncompleteReadError:
                        logger.debug("Incomplete VarInt")
                        if (n - j) < self.MIN_READ_SIZE:  # Need to allocate a new buffer
                            logger.debug("Buffer state [parsed:unparsed:free](size) = [%s:%s:%s](%s)", i, j-i, n-j, n)
                            data, i, j, n = self._allocate_buffer(data, i, j, n)
                        new_data = await self.reader.read(n - j)
                        logger.debug("Read %s bytes from client", len(new_data))
                        if len(new_data) == 0:
                            raise ConnectionResetError
                        data[j:j+len(new_data)] = new_data
                        j += len(new_data)
                    except MCProtocolError as err:
                        logger.debug("Could not parse %s bytes as VarInt: %s", j-i, err)
                        raise
                    else:
                        try:
                            logger.debug("Parsing %s bytes as packet", j-i)
                            total_length, (packet_id, packet_data) = self.decode_packet(data[i:j])
                        except asyncio.IncompleteReadError:
                            logger.debug("Incomplete packet")
                            if (n - j) < min(self.MIN_READ_SIZE, packet_length):  # Need to allocate a new buffer
                                logger.debug("Buffer state [parsed:unparsed:free](size) = [%s:%s:%s](%s)", i, j-i, n-j, n)
                                data, i, j, n = self._allocate_buffer(data, i, j, n)
                            new_data = await self.reader.read(n - j)
                            logger.debug("Read %s bytes from client", len(new_data))
                            if len(new_data) == 0:
                                raise ConnectionResetError
                            data[j:j+len(new_data)] = new_data
                            j += len(new_data)
                        except MCProtocolError as err:
                            logger.debug("Could not parse %s bytes as packet: %s", j-i, err)
                            raise
                        else:
                            i += total_length  # Update start of unparsed bytes
                            packet_length = None
                            return total_length, (packet_id, packet_data)
                    finally:
                        logger.debug("Buffer state [parsed:unparsed:free](size) = [%s:%s:%s](%s)", i, j-i, n-j, n)
            finally:
                self.data, self.i, self.j, self.n, self.packet_length = data, i, j, n, packet_length
                

    async def read_packet(self, timeout: int | None=None) -> Packet[memoryview]:
        return await asyncio.wait_for(self._read_packet(), timeout or self.timeout)

    async def read_handshake_packet(self, timeout: int | None=None) -> dict:
        return self.decode_handshake_packet(await self.read_packet(timeout=timeout or self.timeout))[1]

    async def read_request_packet(self, timeout: int | None=None):
        return self.decode_request_packet(await self.read_packet(timeout=timeout or self.timeout))[1]

    async def read_json_packet(self, timeout: int | None=None) -> tuple[int, dict]:
        return self.decode_json_packet(await self.read_packet(timeout=timeout or self.timeout))[1]

    async def read_pingpong_packet(self, timeout: int | None=None) -> int:
        return self.decode_pingpong_packet(await self.read_packet(timeout=timeout or self.timeout))[1]

####################################################################################################################

class PacketWriter:
    @staticmethod
    def random_ping_payload():
        return struct.unpack(">q", random.randbytes(8))[0]

    ############################################# Encode functions #############################################

    @staticmethod
    def encode_legacy_ping_response(protocol_version: int, mc_version: str, motd: str, max_players: int) -> bytes:
        current_player_count = 0
        string = f"§1\x00{protocol_version}\x00{mc_version}\x00{motd}\x00{current_player_count}\x00{max_players}"
        str_length = len(string)
        return b"\xff" + struct.pack(">H", min(str_length, 65535)) + string.encode("utf-16-be")

    @staticmethod
    def encode_varint(value: int) -> bytes:
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

    @classmethod
    def encode_string(cls, value: str) -> bytes:
        str_data = value.encode()
        return cls.encode_varint(len(str_data)) + str_data

    @classmethod
    def encode_packet(cls, packet_id, packet_data: bytes) -> bytes:
        packet_id_data = cls.encode_varint(packet_id)
        n = len(packet_id_data) + len(packet_data)
        return cls.encode_varint(n) + packet_id_data + packet_data

    @classmethod
    def encode_handshake_packet(cls, protocol_version: int, server_address: str, server_port: int, next_state: int=1) -> bytes:
        return cls.encode_packet(0, cls.encode_varint(protocol_version) + cls.encode_string(server_address) + struct.pack(">H", server_port) + cls.encode_varint(next_state))

    @classmethod
    def encode_request_packet(cls):
        return cls.encode_packet(0, b"")

    @classmethod
    def encode_json_packet(cls, packet_id: int, payload: dict) -> bytes:
        return cls.encode_packet(packet_id, cls.encode_string(json.dumps(payload)))

    @classmethod
    def encode_pingpong_packet(cls, payload: int) -> bytes:
        return cls.encode_packet(1, struct.pack(">q", payload))

    ############################################################################################################

    def __init__(self, writer: asyncio.StreamWriter, timeout: int | None=None):
        self.writer = writer
        self.timeout = timeout

    def write_legacy_ping_response(self, protocol_version: int, mc_version: str, motd: str, max_players: int):
        self.writer.write(self.encode_legacy_ping_response(protocol_version, mc_version, motd, max_players))

    def write_packet(self, packet_id, packet_data: bytes):
        self.writer.write(self.encode_packet(packet_id, packet_data))

    def write_handshake_packet(self, protocol_version: int, server_address: str, server_port: int, next_state: int=1):
        self.writer.write(self.encode_handshake_packet(protocol_version, server_address, server_port, next_state=next_state))

    def write_request_packet(self):
        self.writer.write(self.encode_request_packet())

    def write_json_packet(self, packet_id: int, payload: dict):
        self.writer.write(self.encode_json_packet(packet_id, payload))

    def write_pingpong_packet(self, payload: int):
        self.writer.write(self.encode_pingpong_packet(payload))

    async def drain(self, timeout: int | None=None):
        await asyncio.wait_for(self.writer.drain(), timeout or self.timeout)