# mini_protocol.py 

class MiniPacket:
    TYPE_DATA = 0x01
    TYPE_ACK  = 0x02

    def __init__(self, to_addr, from_addr, msg_id, pkt_type, payload=b''):
        self.to_addr = to_addr
        self.from_addr = from_addr
        self.msg_id = msg_id & 0x0F      # Clamp to 4 bits (0-15)
        self.pkt_type = pkt_type & 0x0F  # Clamp to 4 bits (0-15)
        
        # Enforce max payload constraint
        if len(payload) > 50:
            raise ValueError("Payload exceeds 50 bytes")
        self.payload = payload

    def to_bytes(self):
        """Packs the object into a byte array for transmission."""
        # Combine MSG_ID (high nibble) and PKT_TYPE (low nibble)
        info_byte = (self.msg_id << 4) | self.pkt_type
        length_byte = len(self.payload)
        
        header = bytes([self.to_addr, self.from_addr, info_byte, length_byte])
        return header + self.payload

    @staticmethod
    def from_bytes(data):
        """Unpacks received bytes into a MiniPacket object."""
        if len(data) < 4:
            return None # Invalid packet size
        
        to_addr = data[0]
        from_addr = data[1]
        info_byte = data[2]
        length_byte = data[3]
        
        # Extract MSG_ID and PKT_TYPE from combined byte
        msg_id = (info_byte >> 4) & 0x0F
        pkt_type = info_byte & 0x0F
        
        payload = data[4:]
        
        # Basic integrity check on length
        if len(payload) != length_byte:
            print(f"Warning: Payload length mismatch. Expected {length_byte}, got {len(payload)}")
            
        return MiniPacket(to_addr, from_addr, msg_id, pkt_type, payload)