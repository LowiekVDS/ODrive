# See protocol.hpp for an overview of the protocol

import time
import struct

SYNC_BYTE = ord('$')
CRC8_INIT = 0x42
CRC16_INIT = 0x1337
PROTOCOL_VERSION = 1

CRC8_DEFAULT = 0x37 # this must match the polynomial in the C++ implementation
CRC16_DEFAULT = 0x3d65 # this must match the polynomial in the C++ implementation

#Oskar: There must be a crc library for python already?
def calc_crc(remainder, value, polynomial, bitwidth):
    topbit = (1 << (bitwidth - 1))

    # Bring the next byte into the remainder.
    remainder ^= (value << (bitwidth - 8))
    for bitnumber in range(0,8):
        if (remainder & topbit):
            remainder = (remainder << 1) ^ polynomial
        else:
            remainder = (remainder << 1)

    return remainder & ((1 << bitwidth) - 1)

def calc_crc8(remainder, value):
    if isinstance(value, bytearray) or isinstance(value, bytes) or isinstance(value, list):
        for byte in value:
            remainder = calc_crc(remainder, byte, CRC8_DEFAULT, 8)
    else:
        remainder = calc_crc(remainder, byte, CRC8_DEFAULT, 8)
    return remainder

def calc_crc16(remainder, value):
    if isinstance(value, bytearray) or isinstance(value, bytes) or isinstance(value, list):
        for byte in value:
            remainder = calc_crc(remainder, byte, CRC16_DEFAULT, 16)
    else:
        remainder = calc_crc(remainder, value, CRC16_DEFAULT, 16)
    return remainder

# Can be verified with http://www.sunshine2k.de/coding/javascript/crc/crc_js.html:
#print(hex(calc_crc8(0x12, [1, 2, 3, 4, 5, 0x10, 0x13, 0x37])))
#print(hex(calc_crc16(0xfeef, [1, 2, 3, 4, 5, 0x10, 0x13, 0x37])))


class TimeoutException(Exception):
    pass

class ChannelBrokenException(Exception):
    pass

#Oskar: Do these abstract classes even do anything when empty like this?
# I'm not even too sure how this works in python...
class StreamReader(object):
    pass

class StreamWriter(object):
    pass

class PacketReader(object):
    pass

class PacketWriter(object):
    pass


#Oskar: "StreamWriter" implies that it writes streams, but clearly it takes in ("reads") streams.
# Mabye use the terminology Source and Sink?
# <stuff>Writer -> <stuff>Sink
# <stuff>Reader -> <stuff>Source
# write_<stuff> -> process_<stuff>
# read_<stuff>  -> get_<stuff>
# Same comment for all the uses of the abstract classes.
class StreamToPacketConverter(StreamWriter):
    _header = []
    _packet = []
    _packet_length = 0

    def __init__(self, output):
        self._output = output

#Oskar: process_bytes?
    def write_bytes(self, bytes):
        """
        Processes an arbitrary number of bytes. If one or more full packets are
        are received, they are sent to this instance's output PacketWriter.
        Incomplete packets are buffered between subsequent calls to this function.
        """
        result = None

        for byte in bytes:
            if (len(self._header) < 3):
                # Process header byte
                self._header.append(byte)
                if (len(self._header) == 1) and (self._header[0] != SYNC_BYTE):
                    self._header = []
                elif (len(self._header) == 2) and (self._header[1] & 0x80):
                    self._header = [] # TODO: support packets larger than 128 bytes
                elif (len(self._header) == 3) and calc_crc8(CRC8_INIT, self._header):
                    self._header = []
                elif (len(self._header) == 3):
                    self._packet_length = self._header[1]
            else:
                # Process payload byte
                self._packet.append(byte)

            # If both header and packet are fully received, hand it on to the packet processor
            if (len(self._header) == 3) and (len(self._packet) == self._packet_length):
                try:
                    self._output.write_packet(self._packet)
                except Exception as ex:
                    result = ex
                self._header = []
                self._packet = []
                self._packet_length = 0

        if isinstance(result, Exception):
            #Oskar: why are we removing exception information? Just let the original exception go up?
            raise Exception("something went wrong")


class PacketToStreamConverter(PacketWriter):
    def __init__(self, output):
        self._output = output

    def write_packet(self, packet):
        if (len(packet) >= 128): #Oskar: Use a config variable at top of file or in other file, hardcodes inline in code is hard to maintain.
            raise NotImplementedError("packet larger than 127 currently not supported")

        header = [SYNC_BYTE, len(packet)]
        header.append(calc_crc8(CRC8_INIT, header))

        self._output.write_bytes(header)
        self._output.write_bytes(packet)

class PacketFromStreamConverter(PacketReader, StreamWriter): #Oskar: This shouldn't inherit "StreamWriter", since it doesn't write_bytes.
    def __init__(self, input):
        self._input = input
    
    def read_packet(self, deadline):
        """
        Requests bytes from the underlying input stream until a full packet is
        received or the deadline is reached, in which case None is returned. A
        deadline before the current time corresponds to non-blocking mode.
        """
        while True:
            header = bytes()

            # TODO: sometimes this call hangs, even though the device apparently sent something
            header = header + self._input.read_bytes_or_fail(1, deadline)
            if (header[0] != SYNC_BYTE):
                #print("sync byte mismatch")
                continue

            header = header + self._input.read_bytes_or_fail(1, deadline)
            if (header[1] & 0x80):
                #print("packet too large")
                continue # TODO: support packets larger than 128 bytes

            header = header + self._input.read_bytes_or_fail(1, deadline)
            if calc_crc8(CRC8_INIT, header) != 0:
                #print("crc8 mismatch")
                continue

            packet_length = header[1]
            #print("wait for {} bytes".format(packet_length))
            return self._input.read_bytes_or_fail(packet_length, deadline)


class Channel(PacketWriter):
    _outbound_seq_no = 0
    _interface_definition_crc = bytearray(2)
    _expected_acks = {}

    # Chose these parameters to be sensible for a specific transport layer
    _resend_delay = 5.0     # [s]
    _send_attempts = 5

    def __init__(self, name, input, output):
        """
        Params:
        input: A PacketReader where this channel will source packets from on
               demand. Alternatively packets can be provided to this channel
               directly by calling write_packet on this instance.
        output: A PacketWriter where this channel will put outgoing packets.
        """
        self._name = name
        self._input = input
        self._output = output

    def remote_endpoint_operation(self, endpoint_id, input, expect_ack, output_length):
        if input is None:
            input = bytearray(0)
        if (len(input) >= 128):
            raise Exception("packet larger than 127 currently not supported")

        if (expect_ack):
            endpoint_id |= 0x8000

        self._outbound_seq_no = ((self._outbound_seq_no + 1) & 0x7fff)
        seq_no = self._outbound_seq_no
        packet = struct.pack('<HHH', seq_no, endpoint_id, output_length)
        packet = packet + input

        crc16 = calc_crc16(CRC16_INIT, packet)
        if (endpoint_id & 0x7fff == 0):
            #print("append crc16 for " + str(struct.pack('<H', PROTOCOL_VERSION)))
            crc16 = calc_crc16(crc16, struct.pack('<H', PROTOCOL_VERSION))
        else:
            #print("append crc16 for " + str(self._interface_definition_crc))
            crc16 = calc_crc16(crc16, self._interface_definition_crc)

        # append CRC in big endian
        packet = packet + struct.pack('>H', crc16)

        if (expect_ack):
            self._expected_acks[seq_no] = None
            attempt = 0
            while (attempt < self._send_attempts):
                self._output.write_packet(packet)
                deadline = time.monotonic() + self._resend_delay
                # Read and process packets until we get an ack or need to resend
                # TODO: support I/O driven reception (wait on semaphore)
                while True:
                    try:
                        response = self._input.read_packet(deadline)
                    except TimeoutException:
                        break # resend
                    # process response, which is hopefully our ACK
                    self.write_packet(response)
                    if not self._expected_acks[seq_no] is None:
                        return self._expected_acks.pop(seq_no, None)
                    break
                # TODO: record channel statistics
                attempt += 1
            raise ChannelBrokenException()
        else:
            # fire and forget
            self._output.write_packet(packet)
            return None
    
    def remote_endpoint_read_buffer(self, endpoint_id):
        """
        Handles reads from long endpoints
        """
        # TODO: handle device that could (maliciously) send infinite stream
        buffer = bytes()
        while True:
            chunk_length = 64
            chunk = self.remote_endpoint_operation(0, struct.pack("<I", len(buffer)), True, chunk_length)
            if (len(chunk) == 0):
                break
            buffer += chunk
        return buffer

    def write_packet(self, packet):
        #print("process packet")
        if (len(packet) < 4):
            raise Exception("packet too short")

        # calculate CRC for later validation
        crc16 = calc_crc16(CRC16_INIT, packet[:-2])

        seq_no = struct.unpack('<H', packet[0:2])[0]

        if (seq_no & 0x8000):
            if (calc_crc16(crc16, struct.pack('<HBB', PROTOCOL_VERSION, packet[-2], packet[-1]))):
                raise Exception("CRC16 mismatch")

            seq_no &= 0x7fff
            self._expected_acks[seq_no] = packet[2:-2]

        else:
            print("endpoint requested")
            # TODO: handle local endpoint operation
