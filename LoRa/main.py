from machine import Pin
from sx1262 import SX1262
import sx126x
import time
import _thread
import random
from mini_protocol import PacketV12

# ==========================================
# CONFIGURATION
# ==========================================
MY_ADDR = 0x0A          # Address of THIS node (0x00 - 0x0F)
TARGET_ADDR = 0x0B      # Address of DESTINATION node
WINDOW_SIZE = 4         # ARQ Sliding Window Size (Low for SF12 to reduce congestion)
TIMEOUT_MS = 10000      # Retransmission Timeout (10s). Packet Time-on-Air at SF12 is ~1.5s-2s.
MAX_LBT_RETRIES = 5     # How many times to retry scanning channel before backing off fully.

# ==========================================
# HARDWARE INITIALIZATION
# ==========================================
print("[System] Initializing LoRa Modules...")

# Module 1 (TX): Connected to SPI Bus 1
sx_tx = SX1262(spi_bus=1, clk=Pin(2), mosi=Pin(3), miso=Pin(4), cs=Pin(1), irq=Pin(18), rst=Pin(5), gpio=Pin(6))

# Module 2 (RX): Connected to SPI Bus 2
sx_rx = SX1262(spi_bus=2, clk=Pin(11), mosi=Pin(10), miso=Pin(9), cs=Pin(12), irq=Pin(13), rst=Pin(8), gpio=Pin(7))

# Configure both radios with identical RF parameters
for sx in [sx_tx, sx_rx]:
    sx.begin(freq=866,          # Frequency in MHz
             bw=125.0,          # Bandwidth
             sf=12,             # Spreading Factor 12 (Max Range, Slow Speed)
             cr=8,              # Coding Rate 4/8 (High Reliability)
             syncWord=0x1424,   # Private Network Sync Word
             power=22,          # Max Power (22dBm)
             currentLimit=140.0, 
             preambleLength=16, 
             crcOn=True, 
             tcxoVoltage=1.7, 
             blocking=True)

print("[System] LoRa Modules Ready.")

# ==========================================
# GLOBAL STATE VARIABLES
# ==========================================
# Sender State (ARQ)
tx_queue = []           # Queue of packets waiting to be sent
window_base = 0         # Sequence number of the oldest unacknowledged packet
next_seq_num = 0        # Sequence number to assign to the next new packet
acked_buffer = {}       # Map: {SeqNum: Bool} - Tracks which packets have been ACKed
tx_timestamps = {}      # Map: {SeqNum: TimeMS} - Tracks when a packet was last sent

# Receiver State (Reassembly)
rx_expected_seq = 0     # The next Sequence Number we expect to receive in order
rx_packet_buffer = {}   # Buffer for Out-of-Order packets (Selective Repeat)
rx_msg_reassembly = b'' # Temporary buffer to build the full message string

# Thread Synchronization
lock = _thread.allocate_lock() # Mutex to prevent race conditions between threads

def millis():
    return time.ticks_ms()

# ==========================================
# APPLICATION LAYER: FRAGMENTATION
# ==========================================
def queue_message(text):
    """
    Splits a long string into 50-byte chunks and queues them for transmission.
    Assigns Sequence Numbers and Packet Types (DATA vs DATA_END).
    """
    global next_seq_num
    data = text.encode('utf-8')
    
    # Split into 50-byte chunks
    chunks = [data[i:i+50] for i in range(0, len(data), 50)]
    
    with lock:
        print(f"[App] Fragmenting message into {len(chunks)} parts...")
        for i, chunk in enumerate(chunks):
            # Mark the last fragment as END so receiver knows when to print
            p_type = PacketV12.TYPE_DATA_END if (i == len(chunks) - 1) else PacketV12.TYPE_DATA
            
            # Create Packet Object
            pkt = PacketV12(TARGET_ADDR, MY_ADDR, next_seq_num, p_type, chunk)
            
            # Add to Queue and Initialize ARQ State
            tx_queue.append(pkt)
            acked_buffer[next_seq_num] = False
            
            print(f"[App] Queued Seq {next_seq_num} (Type: {p_type}, Len: {len(chunk)})")
            
            # Increment Sequence Number (0-15 Rolling)
            next_seq_num = (next_seq_num + 1) % 16
            
    print(f"[App] Queuing complete. Waiting for Sender Thread.")

# ==========================================
# THREAD 1: SENDER (ARQ + LBT)
# ==========================================
def sender_loop():
    """
    Background thread that manages the Sliding Window and Transmission.
    Handles:
    1. Checking if packets in the window need sending (First send or Timeout).
    2. Performing LBT (Listen Before Talk) to avoid collisions.
    3. Sliding the window forward when ACKs are received.
    """
    global window_base
    
    print("[Thread] Sender Loop Started.")
    while True:
        current_time = millis()
        with lock:
            # --- 1. Iterate through the current Window ---
            # We only look at packets from [window_base] to [window_base + WINDOW_SIZE]
            for i in range(WINDOW_SIZE):
                seq = (window_base + i) % 16
                
                # Find the actual packet object in the queue
                packet_to_send = None
                for pkt in tx_queue:
                    if pkt.seq_num == seq:
                        packet_to_send = pkt
                        break
                
                # If packet exists and is NOT yet ACKed, check if we need to transmit
                if packet_to_send and not acked_buffer.get(seq, False):
                    last_sent = tx_timestamps.get(seq, 0)
                    
                    # --- 2. Check for Timeout ---
                    # Condition: Never sent OR Time since last send > TIMEOUT_MS
                    if last_sent == 0 or (time.ticks_diff(current_time, last_sent) > TIMEOUT_MS):
                        
                        reason = "First Send" if last_sent == 0 else "Timeout Retry"
                        print(f"\n[ARQ] Triggering send for Seq {seq}. Reason: {reason}")
                        
                        # --- 3. LBT (Listen Before Talk) ---
                        
                        # A. Initial Random Backoff (Desynchronize multiple nodes)
                        initial_backoff = random.randint(10, 40)
                        time.sleep_ms(initial_backoff)
                        
                        sent = False
                        # B. Scan Loop
                        for attempt in range(MAX_LBT_RETRIES):
                            # scanChannel returns -15 (CHANNEL_FREE) or -702 (DETECTED)
                            scan_result = sx_tx.scanChannel()
                            
                            if scan_result == sx126x.CHANNEL_FREE:
                                # Channel Free: Transmit immediately using TX Module
                                sx_tx.send(packet_to_send.to_bytes())
                                tx_timestamps[seq] = millis() # Reset timer
                                sent = True
                                print(f"[TX] Seq {seq}: Packet Sent Successfully.")
                                break
                            else:
                                # Channel Busy: Wait random time and retry
                                retry_wait = random.randint(20, 50)
                                print(f"[LBT] Seq {seq}: Channel BUSY (Noise detected). Waiting {retry_wait}ms...")
                                time.sleep_ms(retry_wait)
                        
                        if not sent:
                            print(f"[LBT] CRITICAL: Channel Congested for Seq {seq}. Dropping attempt.")

            # --- 4. Slide Window ---
            # If the oldest packet (window_base) has been ACKed, we can move the window forward.
            old_base = window_base
            while acked_buffer.get(window_base, False):
                # Remove packet from queue (we are done with it)
                if tx_queue and tx_queue[0].seq_num == window_base:
                    tx_queue.pop(0)
                
                # Cleanup state maps
                del acked_buffer[window_base]
                if window_base in tx_timestamps: del tx_timestamps[window_base]
                
                # Advance Base
                window_base = (window_base + 1) % 16
            
            if old_base != window_base:
                print(f"[ARQ] Window Slided. Old Base: {old_base} -> New Base: {window_base}")
                
        # Yield CPU to other threads
        time.sleep_ms(20)

# ==========================================
# HELPER: REASSEMBLY
# ==========================================
def process_ordered_packet(pkt):
    """
    Called when a packet is received in the correct order.
    Accumulates payload and prints message if it's the final fragment.
    """
    global rx_msg_reassembly
    
    print(f"[Reassembly] Appending payload from Seq {pkt.seq_num} ({len(pkt.payload)} bytes)")
    rx_msg_reassembly += pkt.payload
    
    # Check if this is the end of the fragmented message
    if pkt.pkt_type == PacketV12.TYPE_DATA_END:
        print(f"[Reassembly] End of Message marker (Seq {pkt.seq_num}) found. Decoding...")
        try:
            full_msg = rx_msg_reassembly.decode('utf-8')
            print(f"\n<< [From 0x{pkt.from_addr:02X}] {full_msg}\n")
        except:
            print(f"\n<< [From 0x{pkt.from_addr:02X}] (Binary Data: {rx_msg_reassembly})\n")
            
        # Reset buffer for the next message
        rx_msg_reassembly = b''

# ==========================================
# THREAD 2: RECEIVER
# ==========================================
def rx_loop():
    """
    Background thread dedicated to Continuous Reception on Module 2.
    Handles:
    1. Listening for packets (Long timeout for SF12).
    2. Processing ACKs (updating Sender state).
    3. Processing DATA (sending ACKs back, buffering out-of-order packets).
    """
    global rx_expected_seq
    print("[Thread] RX Loop Started. Listening...")
    
    while True:
        try:
            # Blocking Receive with LONG Timeout (5000ms)
            # Crucial for SF12 where packet airtime > 1.5s
            data, err = sx_rx.recv(len=0, timeout_en=True, timeout_ms=5000)
            
            if len(data) > 0:
                print(f"[RX] Raw bytes received: {len(data)}")
                pkt = PacketV12.from_bytes(data)
                
                if pkt and pkt.to_addr == MY_ADDR:
                    print(f"[RX] Valid Pkt: Seq {pkt.seq_num} | Type {pkt.pkt_type} | From 0x{pkt.from_addr:02X}")
                    
                    # --- CASE A: RECEIVED ACK ---
                    # The other node received our packet. Update our ARQ state.
                    if pkt.pkt_type == PacketV12.TYPE_ACK:
                        print(f"[RX] ACK Received for Seq {pkt.seq_num}.")
                        with lock:
                            acked_buffer[pkt.seq_num] = True
                            
                    # --- CASE B: RECEIVED DATA ---
                    # We received a message. We MUST send an ACK back.
                    elif pkt.pkt_type in (PacketV12.TYPE_DATA, PacketV12.TYPE_DATA_END):
                        
                        # 1. Send ACK immediately using TX Module
                        print(f"[RX] Data Packet. Preparing ACK for Seq {pkt.seq_num}...")
                        ack = PacketV12(pkt.from_addr, MY_ADDR, pkt.seq_num, PacketV12.TYPE_ACK)
                        
                        # Simplified LBT for ACK (Short wait)
                        ack_backoff = random.randint(5, 15)
                        time.sleep_ms(ack_backoff) 
                        sx_tx.send(ack.to_bytes())
                        print(f"[TX] ACK Sent for Seq {pkt.seq_num}.")
                        
                        # 2. Process the Data (Selective Repeat Logic)
                        seq = pkt.seq_num
                        with lock:
                            # Is this the exact packet we were waiting for?
                            if seq == rx_expected_seq:
                                print(f"[RX] Packet is In-Order (Expected {rx_expected_seq}). Processing...")
                                process_ordered_packet(pkt)
                                rx_expected_seq = (rx_expected_seq + 1) % 16
                                
                                # Check if we have subsequent packets buffered
                                while rx_expected_seq in rx_packet_buffer:
                                    print(f"[RX] Found buffered Seq {rx_expected_seq}. Processing...")
                                    buffered_pkt = rx_packet_buffer.pop(rx_expected_seq)
                                    process_ordered_packet(buffered_pkt)
                                    rx_expected_seq = (rx_expected_seq + 1) % 16
                                    
                            # Is this a future packet? (Out of Order)
                            elif seq != rx_expected_seq: 
                                print(f"[RX] WARNING: Out-of-Order Packet (Seq {seq}, Expected {rx_expected_seq}). Buffering.")
                                rx_packet_buffer[seq] = pkt
                else:
                    if pkt:
                        print(f"[RX] Ignored packet for Addr 0x{pkt.to_addr:02X}")
                    else:
                        print("[RX] Packet decode failed (CRC error or too short).")

        except Exception as e:
            print(f"[RX Error] {e}")

# ==========================================
# MAIN EXECUTION
# ==========================================
# 1. Start Background Threads
_thread.start_new_thread(rx_loop, ())
_thread.start_new_thread(sender_loop, ())

print(f"--- LoRa V1.2 (LBT + Reassembly + Debug) Node 0x{MY_ADDR:02X} ---")

# 2. Main Input Loop
while True:
    try:
        # Wait for user input
        msg = input(">> ")
        if msg:
            # Queue the message (Sender thread will handle transmission)
            queue_message(msg)
    except KeyboardInterrupt:
        print("\n[System] Stopping...")
        break
