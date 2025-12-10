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

# --- PACKET CLASS v1.2 ---
class PacketV12:
    # Packet Types
    TYPE_DATA     = 0x01  # Intermediate Fragment
    TYPE_ACK      = 0x02  # Acknowledgment
    TYPE_DATA_END = 0x03  # Last Fragment (End of Message)
    
    HEADER_SIZE = 3
    FOOTER_SIZE = 2
    MAX_PAYLOAD = 50 

    def __init__(self, to_addr, from_addr, seq_num, pkt_type, payload=b''):
        self.to_addr = to_addr & 0x0F
        self.from_addr = from_addr & 0x0F
        self.seq_num = seq_num & 0x0F
        self.pkt_type = pkt_type & 0x0F
        self.payload = payload

    def to_bytes(self):
        # Byte 0: [TO (4) | FROM (4)]
        addr_byte = (self.to_addr << 4) | self.from_addr
        # Byte 1: [SEQ (4) | TYPE (4)]
        info_byte = (self.seq_num << 4) | self.pkt_type
        # Byte 2: Length
        len_byte = len(self.payload)
        
        header = bytes([addr_byte, info_byte, len_byte])
        packet_no_crc = header + self.payload
        
        # Footer: CRC16
        checksum = crc16(packet_no_crc)
        crc_bytes = struct.pack('>H', checksum)
        
        return packet_no_crc + crc_bytes

    @staticmethod
    def from_bytes(data):
        if len(data) < (PacketV12.HEADER_SIZE + PacketV12.FOOTER_SIZE):
            return None
            
        # CRC Check
        payload_with_header = data[:-2]
        received_crc = struct.unpack('>H', data[-2:])[0]
        calculated_crc = crc16(payload_with_header)
        
        if received_crc != calculated_crc:
            return None # Corrupt
            
        # Parse
        addr_byte = data[0]
        info_byte = data[1]
        len_byte = data[2]
        
        to_addr = (addr_byte >> 4) & 0x0F
        from_addr = addr_byte & 0x0F
        seq_num = (info_byte >> 4) & 0x0F
        pkt_type = info_byte & 0x0F
        
        if len(payload_with_header) < (3 + len_byte):
            return None
            
        payload = payload_with_header[3 : 3 + len_byte]
        return PacketV12(to_addr, from_addr, seq_num, pkt_type, payload)
