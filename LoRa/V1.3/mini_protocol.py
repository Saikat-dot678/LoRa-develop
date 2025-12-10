import struct

# --- CRC16-CCITT Implementation ---
def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= (byte << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
        crc &= 0xFFFF
    return crc

class PacketV13:
    # Packet Types
    TYPE_ACK = 0x01
    TYPE_MSG_CHUNK = 0x02  # Part of a text message
    TYPE_MSG_END   = 0x06  # End of a text message (NEW)
    
    TYPE_FILE_START = 0x03 # Metadata
    TYPE_FILE_CHUNK = 0x04 # Content
    TYPE_FILE_END   = 0x05 # EOF

    # Header: To (1), From (1), Seq (1), Type (1)
    HEADER_FMT = 'BBBB'
    HEADER_SIZE = struct.calcsize(HEADER_FMT)
    FOOTER_SIZE = 2 # CRC16

    def __init__(self, to_addr, from_addr, seq_num, pkt_type, payload=b''):
        self.to_addr = to_addr & 0xFF
        self.from_addr = from_addr & 0xFF
        self.seq_num = seq_num & 0xFF
        self.pkt_type = pkt_type & 0xFF
        self.payload = payload

    def to_bytes(self):
        header = struct.pack(self.HEADER_FMT, self.to_addr, self.from_addr, self.seq_num, self.pkt_type)
        packet_no_crc = header + self.payload
        checksum = crc16(packet_no_crc)
        crc_bytes = struct.pack('>H', checksum)
        return packet_no_crc + crc_bytes

    @classmethod
    def from_bytes(cls, data):
        if len(data) < (cls.HEADER_SIZE + cls.FOOTER_SIZE):
            return None
        
        payload_with_header = data[:-cls.FOOTER_SIZE]
        received_crc = struct.unpack('>H', data[-cls.FOOTER_SIZE:])[0]
        
        if crc16(payload_with_header) != received_crc:
            return None # CRC Fail
        
        header = payload_with_header[:cls.HEADER_SIZE]
        to_addr, from_addr, seq_num, pkt_type = struct.unpack(cls.HEADER_FMT, header)
        payload = payload_with_header[cls.HEADER_SIZE:]
        
        return cls(to_addr, from_addr, seq_num, pkt_type, payload)
