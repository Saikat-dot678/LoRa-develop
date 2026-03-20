import struct

# --- CONSTANTS ---
TYPE_BEACON     = 0x10
TYPE_JOIN_REQ   = 0x20
TYPE_DATA_REQ   = 0x30 
TYPE_CONTROL    = 0x40 
TYPE_HUB_SCHED  = 0x50
TYPE_ACK        = 0x01
TYPE_MSG_CHUNK  = 0x02
TYPE_MSG_END    = 0x06
TYPE_FILE_START = 0x03
TYPE_FILE_CHUNK = 0x04
TYPE_FILE_END   = 0x05

# --- CRC Helper ---
def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= (byte << 8)
        for _ in range(8):
            if crc & 0x8000: crc = (crc << 1) ^ 0x1021
            else: crc <<= 1
        crc &= 0xFFFF
    return crc

# --- 1. BEACON PACKET ---
class BeaconPacket:
    """
    [0] Type (0x10) | [1] Hub_ID 
    [2-9] Net_Time (8B) | [10-17] Frame_Start (8B)
    [18] Term_Remaining | [19] Node_Count | [20...] Active Nodes
    """
    def __init__(self, hub_id, net_time, frame_start, term, active_nodes=None):
        self.hub_id = hub_id
        self.net_time = net_time
        self.frame_start = frame_start
        self.term = term
        self.active_nodes = active_nodes if active_nodes else [] 

    def to_bytes(self):
        count = len(self.active_nodes)
        # >BBQQBB = 1 byte, 1 byte, 8 bytes, 8 bytes, 1 byte, 1 byte
        header = struct.pack('>BBQQBB', TYPE_BEACON, self.hub_id, self.net_time, self.frame_start, self.term, count)
        payload = bytearray(self.active_nodes) 
        return header + payload

    @classmethod
    def from_bytes(cls, data):
        if len(data) < 20 or data[0] != TYPE_BEACON: return None
        try:
            _, hid, ntime, fstart, term, count = struct.unpack('>BBQQBB', data[:20])
            active_nodes = list(data[20:20+count])
            return cls(hid, ntime, fstart, term, active_nodes)
        except: return None

# --- 2. CONTROL PACKET (Now with GPS) ---
class ControlPacket:
    """
    Sent during Phase 2 Control Window.
    Header: [Type, Src] (2B) + [Lat, Lon] (8B) + CRC (2B)
    """
    def __init__(self, src, lat=0.0, lon=0.0):
        self.src = src
        self.lat = float(lat)
        self.lon = float(lon)

    def to_bytes(self):
        # >BBff = Type (1B), Src (1B), Lat (4B Float), Lon (4B Float)
        pkt = struct.pack('>BBff', TYPE_CONTROL, self.src, self.lat, self.lon)
        return pkt + struct.pack('>H', crc16(pkt))

    @classmethod
    def from_bytes(cls, data):
        if len(data) < 12 or data[0] != TYPE_CONTROL: return None
        payload = data[:-2]
        if crc16(payload) != struct.unpack('>H', data[-2:])[0]: return None
        _, src, lat, lon = struct.unpack('>BBff', payload)
        return cls(src, lat, lon)

# --- 3. DATA PACKET ---
class DataPacket:
    """
    Used for File Chunks, Text, ACKs.
    Header: [Type, To, From, Seq] (4B) + Payload + CRC(2B)
    """
    HEADER_FMT = '>BBBB'
    HEADER_SIZE = 4
    FOOTER_SIZE = 2

    def __init__(self, to_addr, from_addr, seq_num, pkt_type, payload=b''):
        self.to_addr = to_addr
        self.from_addr = from_addr
        self.seq_num = seq_num
        self.pkt_type = pkt_type
        self.payload = payload

    def to_bytes(self):
        header = struct.pack(self.HEADER_FMT, self.pkt_type, self.to_addr, self.from_addr, self.seq_num)
        pkt = header + self.payload
        checksum = crc16(pkt)
        return pkt + struct.pack('>H', checksum)

    @classmethod
    def from_bytes(cls, data):
        if len(data) < (cls.HEADER_SIZE + cls.FOOTER_SIZE): return None
        payload_part = data[:-cls.FOOTER_SIZE]
        received_crc = struct.unpack('>H', data[-cls.FOOTER_SIZE:])[0]
        if crc16(payload_part) != received_crc: return None
        
        h = struct.unpack(cls.HEADER_FMT, payload_part[:cls.HEADER_SIZE])
        return cls(h[1], h[2], h[3], h[0], payload_part[cls.HEADER_SIZE:])    

# --- 4. JOIN REQ PACKET (Now with GPS) ---
class JoinReqPacket:
    """
    Stranger asking to join the network.
    Header: [Type, Addr] (2B) + [Lat, Lon] (8B) + CRC (2B)
    """
    def __init__(self, node_addr, lat=0.0, lon=0.0):
        self.node_addr = node_addr
        self.lat = float(lat)
        self.lon = float(lon)

    def to_bytes(self):
        pkt = struct.pack('>BBff', TYPE_JOIN_REQ, self.node_addr, self.lat, self.lon)
        return pkt + struct.pack('>H', crc16(pkt))

    @classmethod
    def from_bytes(cls, data):
        if len(data) < 12 or data[0] != TYPE_JOIN_REQ: return None
        payload = data[:-2]
        if crc16(payload) != struct.unpack('>H', data[-2:])[0]: return None
        _, addr, lat, lon = struct.unpack('>BBff', payload)
        return cls(addr, lat, lon)

# --- 5. HUB SCHEDULING (DATA_REQ_REP) ---
class HubSchedPacket:
    """
    Hub granting bandwidth/frequency pairs to nodes.
    [0] Type (0x50) | [1] Count | [2...] Assignments (Addr, Pair)
    """
    def __init__(self, assignments=None):
        self.assignments = assignments if assignments else []

    def to_bytes(self):
        count = len(self.assignments)
        payload = struct.pack('>BB', TYPE_HUB_SCHED, count)
        for asm in self.assignments:
            payload += struct.pack('>BB', asm[0], asm[1])
        return payload

    @classmethod
    def from_bytes(cls, data):
        if len(data) < 2 or data[0] != TYPE_HUB_SCHED: return None
        count = data[1]
        assignments = []
        ptr = 2
        for _ in range(count):
            if ptr + 2 > len(data): break
            addr, pair = struct.unpack('>BB', data[ptr:ptr+2])
            assignments.append((addr, pair))
            ptr += 2
        return cls(assignments)
