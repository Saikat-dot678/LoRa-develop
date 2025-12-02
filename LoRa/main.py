# from machine import Pin, Timer
# from sx1262 import SX1262
# import sx126x
# import time
# import _thread
# recv_msg_queue = []
# # LoRa Init 01
# sx01 = SX1262(spi_bus=1, clk=Pin(2), mosi=Pin(3), miso=Pin(4), cs=Pin(1), irq=Pin(18), rst=Pin(5), gpio=Pin(6))

# # sx01.begin(
# #     freq=866, 
# #     bw=125.0, 
# #     sf=7, 
# #     cr=8, 
# #     syncWord=0x3444,
# #     power=17, 
# #     currentLimit=60.0, 
# #     preambleLength=8,
# #     crcOn=True, 
# #     tcxoVoltage=1.7,
# #     blocking=True)

# sx01.begin(
#     freq=866, 
#     bw=125.0, 
#     sf=12, 
#     cr=8, 
#     syncWord=0x3444,
#     power=22, 
#     currentLimit=140.0, 
#     preambleLength=16,
#     crcOn=True, 
#     tcxoVoltage=1.7,
#     blocking=True)

# # LoRa Init 02
# sx02 = SX1262(spi_bus=2, clk=Pin(11), mosi=Pin(10), miso=Pin(9), cs=Pin(12), irq=Pin(13), rst=Pin(8), gpio=Pin(7))

# # sx02.begin(
# #     freq=866, 
# #     bw=125.0, 
# #     sf=7, 
# #     cr=8, 
# #     syncWord=0x3444,
# #     power=17, 
# #     currentLimit=60.0, 
# #     preambleLength=8,
# #     crcOn=True, 
# #     tcxoVoltage=1.7,
# #     blocking=True)

# sx02.begin(
#     freq=866, 
#     bw=125.0, 
#     sf=12, 
#     cr=8, 
#     syncWord=0x3444,
#     power=22, 
#     currentLimit=140.0, 
#     preambleLength=16,
#     crcOn=True, 
#     tcxoVoltage=1.7,
#     blocking=True)


# def tx_mode():
#     msg = "node02"
    
#     while True :
        
#         p = sx01.scanChannel()
#         print(p)
#         if p == -15 :
#             sx01.send(msg.encode('utf-8'))

        

# def rx_mode():
#     global recv_msg_queue
#     while True :
# #         n = sx02.scanChannel()
# #         print(n)
#         recv, err = sx02.recv()
#         recv_msg_queue.append(recv)
#         msg_recv = recv_msg_queue.pop(0)#.decode('utf-8').strip()
#         print(msg_recv)
        

# _thread.start_new_thread(rx_mode, ())

# tx_mode()

from machine import Pin
from sx1262 import SX1262
import sx126x
import time
import random
import _thread
import sys

# --- CONFIGURATION ---
MY_ADDR = 0x0A       # Change to 0x0B for the second device
TARGET_ADDR = 0x0B   # Change to 0x0A for the second device
# ---------------------

# --- LBT CONSTANTS (From PDF Page 8) ---
INITIAL_BACKOFF_MAX = 32  # ms (0-32ms initial delay)
RETRY_BACKOFF_MIN = 50    # ms
RETRY_BACKOFF_MAX = 150   # ms
MAX_LBT_RETRIES = 5

# --- HARDWARE INIT (Based on your main.py) ---
# Module 1: Used for TRANSMISSION (TX) and CAD
sx_tx = SX1262(spi_bus=1, clk=Pin(2), mosi=Pin(3), miso=Pin(4), cs=Pin(1), irq=Pin(18), rst=Pin(5), gpio=Pin(6))

# Module 2: Used for RECEPTION (RX)
sx_rx = SX1262(spi_bus=2, clk=Pin(11), mosi=Pin(10), miso=Pin(9), cs=Pin(12), irq=Pin(13), rst=Pin(8), gpio=Pin(7))

# Configure BOTH modules
for sx in [sx_tx, sx_rx]:
    sx.begin(
        freq=866,          # Same frequency for now
        bw=125.0, 
        sf=9, 
        cr=7, 
        syncWord=0x1424, 
        power=22, 
        currentLimit=140.0, 
        preambleLength=16,
        crcOn=True, 
        tcxoVoltage=1.7,
        blocking=True
    )

# Msg ID Tracker
current_msg_id = 0

# --- PACKET CLASS (Mini Algorithm) ---
class MiniPacket:
    TYPE_DATA = 0x01
    TYPE_ACK  = 0x02

    def __init__(self, to_addr, from_addr, msg_id, pkt_type, payload=b''):
        self.to_addr = to_addr
        self.from_addr = from_addr
        self.msg_id = msg_id & 0x0F
        self.pkt_type = pkt_type & 0x0F
        if len(payload) > 50: payload = payload[:50]
        self.payload = payload

    def to_bytes(self):
        info_byte = (self.msg_id << 4) | self.pkt_type
        header = bytes([self.to_addr, self.from_addr, info_byte, len(self.payload)])
        return header + self.payload

    @staticmethod
    def from_bytes(data):
        if len(data) < 4: return None
        to_addr, from_addr, info_byte, length_byte = data[0], data[1], data[2], data[3]
        if len(data[4:]) < length_byte: return None
        return MiniPacket(to_addr, from_addr, (info_byte >> 4), (info_byte & 0x0F), data[4:4+length_byte])

# --- LBT SEND ALGORITHM (PDF Page 7-8) ---
def lbt_send(packet_obj):
    packet_bytes = packet_obj.to_bytes()
    print(f"[TX] Attempting to send {len(packet_bytes)} bytes...")

    # Step 1: Initial Random Backoff (PDF: "desynchronizes channel access")
    # Wait 0-32ms BEFORE touching the radio
    initial_delay = random.randint(0, INITIAL_BACKOFF_MAX)
    time.sleep_ms(initial_delay)

    for attempt in range(1, MAX_LBT_RETRIES + 1):
        # Step 2: Channel Activity Detection (CAD) on TX Module
        # Returns -15 (Free) or -702 (Detected)
        status = sx_tx.scanChannel()

        if status == sx126x.CHANNEL_FREE:
            # Step 3: Channel Clear -> Transmit
            sx_tx.send(packet_bytes)
            print(f"[TX] Sent (Attempt {attempt}, Waited {initial_delay}ms)")
            return True
        else:
            # Step 4: Channel Busy -> Extended Backoff
            print(f"[TX] Busy (Attempt {attempt}). Backing off...")
            retry_delay = random.randint(RETRY_BACKOFF_MIN, RETRY_BACKOFF_MAX)
            time.sleep_ms(retry_delay)

    print("[TX] Failed: Channel congested.")
    return False

# --- RX THREAD (Using SX_RX Module) ---
def rx_loop():
    print("[RX] Listening on Module 2...")
    while True:
        try:
            # Blocking receive with timeout on Module 2
            # We use timeout so the thread doesn't lock up forever if we need to exit
            data, err = sx_rx.recv(len=0, timeout_en=True, timeout_ms=1000)
            
            if len(data) > 0:
                pkt = MiniPacket.from_bytes(data)
                if pkt and (pkt.to_addr == MY_ADDR or pkt.to_addr == 0xFF):
                    msg = pkt.payload.decode('utf-8', 'ignore')
                    print(f"\n<< [RX from {pkt.from_addr:02X}] {msg}")
                    
        except Exception as e:
            print(f"[RX Error] {e}")

# --- MAIN EXECUTION ---
_thread.start_new_thread(rx_loop, ())

print(f"--- Dual-Module Chat (My Addr: 0x{MY_ADDR:02X}) ---")

while True:
    try:
        user_input = input(">> ")
        if user_input:
            current_msg_id = (current_msg_id + 1) % 16
            pkt = MiniPacket(TARGET_ADDR, MY_ADDR, current_msg_id, MiniPacket.TYPE_DATA, user_input.encode())
            lbt_send(pkt)
    except KeyboardInterrupt:
        break
