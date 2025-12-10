from machine import Pin
from sx1262 import SX1262
import sx126x
import time
import _thread
import random
import network
import socket
import select
import os
import json
import gc
from mini_protocol import PacketV13

# --- SYSTEM CONFIG ---
WIFI_SSID = "LoRa_Node_AP"       # WiFi Access Point base SSID
WIFI_PASS = "12345678"           # WiFi AP password
MY_ADDR = 0x0B                   # This node's logical LoRa address
TARGET_ADDR = 0x0A               # Destination node logical address

# --- FREQUENCY PLAN (FULL DUPLEX) ---
# Use different TX/RX frequencies depending on which node this is.
# Node 0x0B transmits on 866.5 MHz and listens on 866 MHz.
# Node 0x0A does the opposite.
if MY_ADDR == 0x0B:
    FREQ_TX = 866.5
    FREQ_RX = 866
else:
    FREQ_TX = 866
    FREQ_RX = 866.5

# --- LORA SETTINGS ---
LORA_SF = 7                       # Spreading factor
LORA_BW = 250.0                   # Bandwidth in kHz
WINDOW_SIZE = 8                   # Sliding window size for ARQ
TIMEOUT_MS = 1500                 # Retransmission timeout for unacked packets
MAX_LBT_RETRIES = 10              # Max Listen-Before-Talk retries per send

# --- HARDWARE INIT ---
print(f"[System] Init Node 0x{MY_ADDR:02X} (TX:{FREQ_TX}MHz, RX:{FREQ_RX}MHz)")

# Transmitter radio (LoRa) on SPI bus 1
sx_tx = SX1262(
    spi_bus=1,
    clk=Pin(2),
    mosi=Pin(3),
    miso=Pin(4),
    cs=Pin(1),
    irq=Pin(18),
    rst=Pin(5),
    gpio=Pin(6)
)
sx_tx.begin(
    freq=FREQ_TX,
    bw=LORA_BW,
    sf=LORA_SF,
    cr=5,
    syncWord=0x1424,
    power=22,
    currentLimit=140.0,
    preambleLength=8,
    crcOn=True,
    blocking=True
)

# Receiver radio (LoRa) on SPI bus 2
sx_rx = SX1262(
    spi_bus=2,
    clk=Pin(11),
    mosi=Pin(10),
    miso=Pin(9),
    cs=Pin(12),
    irq=Pin(13),
    rst=Pin(8),
    gpio=Pin(7)
)
sx_rx.begin(
    freq=FREQ_RX,
    bw=LORA_BW,
    sf=LORA_SF,
    cr=5,
    syncWord=0x1424,
    power=22,
    currentLimit=140.0,
    preambleLength=8,
    crcOn=True,
    blocking=True
)

# --- GLOBALS & BUFFERS ---
tx_queue = []            # Outgoing packets waiting to be (re)sent
window_base = 0          # Base of sliding window (lowest unacked seq num)
next_seq_num = 0         # Next sequence number to allocate
acked_buffer = {}        # seq_num -> bool (True if ACK received)
tx_timestamps = {}       # seq_num -> last transmit time (ms)
web_logs = []            # Recent log messages for web UI

# --- RX BUFFERS ---
rx_expected_seq = 0      # Next sequence number expected in-order
rx_packet_buffer = {}    # Out-of-order packets buffer: seq_num -> PacketV13
rx_msg_reassembly = b''  # Buffer to reassemble multi-packet text messages

# File Reassembly
rx_file_handle = None    # File object for writing incoming file chunks
rx_file_name = ""        # Name of file being received

# --- LOCKS ---
main_lock = _thread.allocate_lock()  # Protects tx_queue, window, and related state
log_lock = _thread.allocate_lock()   # Protects web_logs

# --- UTILS ---
def log_web(msg):
    """
    Append a log message to the web_logs ring buffer,
    keeping only the latest 25 messages.
    """
    global web_logs
    with log_lock:
        web_logs.append(msg)
        if len(web_logs) > 25:
            web_logs.pop(0)

def millis():
    """
    Return current time in milliseconds based on time.ticks_ms().
    """
    return time.ticks_ms()

# --- WIFI & WEB SERVER (FIXED READ LOOP) ---
def setup_wifi():
    """
    Configure ESP as a WiFi Access Point with SSID including node address.
    """
    ap = network.WLAN(network.AP_IF)
    ap.active(True)
    ap.config(essid=f"{WIFI_SSID}_{MY_ADDR:02X}", password=WIFI_PASS)
    print(f"[WiFi] AP Created: {ap.ifconfig()[0]}")

def parse_multipart(body, boundary):
    """
    Very simple multipart/form-data parser.
    Extracts first file with 'filename="' in its headers.

    Returns:
        (filename, content_bytes) or (None, None) on failure.
    """
    try:
        parts = body.split(boundary)
        for part in parts:
            if b'filename="' in part:
                # Split headers and content
                headers, content = part.split(b'\r\n\r\n', 1)
                h_str = headers.decode()
                # Extract filename="..."
                fname = h_str.split('filename="')[1].split('"')[0]
                # Strip multipart trailing markers
                content = content.rstrip(b'\r\n--')
                return fname, content
    except:
        return None, None
    return None, None

def run_web_server():
    """
    Main HTTP server loop.
    - Serves index.html
    - Provides /api/state for status/logs
    - Handles /api/send_msg to queue text messages
    - Handles /api/upload_file to queue file transfer over LoRa
    """
    setup_wifi()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('', 80))
    s.listen(5)
    
    print("[Web] Server Ready.")
    
    while True:
        try:
            conn, addr = s.accept()
            conn.settimeout(3.0)  # Increased timeout for slow uploads
            
            # 1. Read Headers first, until we find header/body separator
            request = b''
            try:
                while b'\r\n\r\n' not in request:
                    chunk = conn.recv(512)
                    if not chunk:
                        break
                    request += chunk
            except:
                pass
            
            if not request:
                conn.close()
                continue
                
            # Decode only header part in latin-1 to avoid errors
            header_part = request.decode('latin-1').split('\r\n\r\n')[0]
            
            # 2. Check for Content-Length (needed for file uploads / POST bodies)
            content_length = 0
            for line in header_part.split('\r\n'):
                if 'Content-Length:' in line:
                    content_length = int(line.split(':')[1].strip())
                    break
            
            # 3. Read Body Loop (ensure we read the entire body for uploads)
            body = request.split(b'\r\n\r\n', 1)[1] if b'\r\n\r\n' in request else b''
            
            if content_length > 0:
                print(f"[Web] Receiving {content_length} bytes...")
                while len(body) < content_length:
                    try:
                        chunk = conn.recv(1024)
                        if not chunk:
                            break
                        body += chunk
                    except:
                        break
            
            # 4. Route Handling
            if "GET / " in header_part:
                # Serve basic index.html UI
                with open('index.html', 'r') as f:
                    response = "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n" + f.read()
                    conn.send(response.encode())
            
            elif "GET /api/state" in header_part:
                # Return current node info and logs as JSON
                with log_lock:
                    current_logs = list(web_logs)
                state = {"my_addr": MY_ADDR, "logs": current_logs}
                response = "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n" + json.dumps(state)
                conn.send(response.encode())
                
            elif "POST /api/send_msg" in header_part:
                # Text message is sent as raw body
                msg_text = body.decode('utf-8')
                queue_message(msg_text)
                conn.send("HTTP/1.1 200 OK\r\n\r\nOK".encode())
                
            elif "POST /api/upload_file" in header_part:
                # Handle file upload via multipart/form-data
                try:
                    # Parse boundary string from Content-Type header
                    boundary = ''
                    for line in header_part.split('\r\n'):
                        if 'boundary=' in line:
                            boundary = line.split('boundary=')[1]
                            break
                    
                    if boundary:
                        boundary_bytes = b'--' + boundary.encode()
                        fname, fcontent = parse_multipart(body, boundary_bytes)
                        
                        if fname and fcontent:
                            print(f"[TX FILE] Queued: {fname} ({len(fcontent)} B)")
                            log_web(f"[Web] Queued: {fname}")
                            queue_file(fname, fcontent)
                            conn.send("HTTP/1.1 200 OK\r\n\r\nOK".encode())
                        else:
                            conn.send("HTTP/1.1 400 Bad Request\r\n\r\nParse Fail".encode())
                    else:
                        conn.send("HTTP/1.1 400 Bad Request\r\n\r\nNo Boundary".encode())
                except Exception as e:
                    print(f"[Web Err] {e}")
                    conn.send("HTTP/1.1 500 Error\r\n\r\nError".encode())
            else:
                # Unknown path
                conn.send("HTTP/1.1 404 Not Found\r\n\r\n".encode())
            
            conn.close()
            gc.collect()  # Free memory regularly
            
        except Exception as e:
            # Generic error in server loop; silently ignore to keep server alive
            # print(f"[Server Loop Err] {e}")
            pass

# --- QUEUING LOGIC (V1.2 Fragmentation Logic) ---
def queue_message(text):
    """
    Fragment a text message into multiple LoRa packets (200 bytes max payload each),
    using TYPE_MSG_CHUNK for intermediate chunks and TYPE_MSG_END for final chunk.
    Packets are appended to tx_queue with sequence numbers assigned.
    """
    global next_seq_num
    print(f"[TX MSG] {text}")
    log_web(f">> {text}")
    data = text.encode('utf-8')
    # Split into chunks of up to 200 bytes
    chunks = [data[i:i+200] for i in range(0, len(data), 200)]
    
    with main_lock:
        for i, chunk in enumerate(chunks):
            # V1.2 Logic: Mark last chunk with TYPE_MSG_END
            is_last = (i == len(chunks) - 1)
            p_type = PacketV13.TYPE_MSG_END if is_last else PacketV13.TYPE_MSG_CHUNK
            
            # Create packet and enqueue
            pkt = PacketV13(TARGET_ADDR, MY_ADDR, next_seq_num, p_type, chunk)
            tx_queue.append(pkt)
            acked_buffer[next_seq_num] = False  # Not yet acknowledged
            next_seq_num = (next_seq_num + 1) % 256  # Wrap at 256

def queue_file(filename, content):
    """
    Queue a file for transmission:
    - First send TYPE_FILE_START with "filename|size" metadata.
    - Then send multiple TYPE_FILE_CHUNK packets (up to 180 bytes each).
    - Finally send TYPE_FILE_END with empty payload.
    """
    global next_seq_num
    # Metadata: "filename|filesize"
    meta = f"{filename}|{len(content)}".encode('utf-8')
    with main_lock:
        # Start packet with metadata
        tx_queue.append(PacketV13(TARGET_ADDR, MY_ADDR, next_seq_num, PacketV13.TYPE_FILE_START, meta))
        acked_buffer[next_seq_num] = False
        next_seq_num = (next_seq_num + 1) % 256
        
        # File data chunks (slightly smaller to account for headers)
        chunks = [content[i:i+180] for i in range(0, len(content), 180)]
        for chunk in chunks:
            tx_queue.append(PacketV13(TARGET_ADDR, MY_ADDR, next_seq_num, PacketV13.TYPE_FILE_CHUNK, chunk))
            acked_buffer[next_seq_num] = False
            next_seq_num = (next_seq_num + 1) % 256
            
        # End-of-file marker packet
        tx_queue.append(PacketV13(TARGET_ADDR, MY_ADDR, next_seq_num, PacketV13.TYPE_FILE_END, b''))
        acked_buffer[next_seq_num] = False
        next_seq_num = (next_seq_num + 1) % 256

# --- PROCESS PACKET (Reassembly Logic) ---
def process_ordered_packet(pkt):
    """
    Handle an in-order received packet, performing application-level actions:
    - Reassemble text messages (TYPE_MSG_CHUNK / TYPE_MSG_END)
    - Reassemble files (TYPE_FILE_START / TYPE_FILE_CHUNK / TYPE_FILE_END)
    """
    global rx_file_handle, rx_file_name, rx_msg_reassembly
    
    # 1. Text Reassembly
    if pkt.pkt_type == PacketV13.TYPE_MSG_CHUNK:
        # Accumulate partial text
        rx_msg_reassembly += pkt.payload
        
    elif pkt.pkt_type == PacketV13.TYPE_MSG_END:
        # Final chunk of a text message
        rx_msg_reassembly += pkt.payload
        try:
            full_msg = rx_msg_reassembly.decode('utf-8')
            print(f"[RX MSG] {full_msg}")
            log_web(f"<< {full_msg}")
        except:
            # Fallback if decoding fails
            print(f"[RX MSG] (Binary/Error)")
        # Clear text buffer after message completion
        rx_msg_reassembly = b''

    # 2. File Handling
    elif pkt.pkt_type == PacketV13.TYPE_FILE_START:
        # Start of file transfer: parse "filename|size" and open file for writing
        try:
            meta = pkt.payload.decode().split('|')
            rx_file_name = meta[0]
            size = int(meta[1])
            rx_file_handle = open(rx_file_name, 'wb')
            print(f"[RX FILE] Start: {rx_file_name} ({size} B)")
            log_web(f"[File] Incoming: {rx_file_name}")
        except:
            # Ignore malformed metadata
            pass
    
    elif pkt.pkt_type == PacketV13.TYPE_FILE_CHUNK:
        # Write file chunk if a file is currently open
        if rx_file_handle:
            rx_file_handle.write(pkt.payload)
    
    elif pkt.pkt_type == PacketV13.TYPE_FILE_END:
        # Final packet of file transfer: close handle and report completion
        if rx_file_handle:
            rx_file_handle.close()
            rx_file_handle = None
            print(f"[RX FILE] Complete: {rx_file_name}")
            log_web(f"[File] Saved: {rx_file_name}")

# --- SENDER LOOP ---
def sender_loop():
    """
    Continuous sender thread implementing sliding window ARQ with:
    - Window size WINDOW_SIZE
    - Retransmission after TIMEOUT_MS
    - Listen-Before-Talk (LBT) with random backoff and MAX_LBT_RETRIES
    """
    global window_base
    while True:
        current_time = millis()
        with main_lock:
            # Iterate over all positions in the current window
            for i in range(WINDOW_SIZE):
                seq = (window_base + i) % 256
                pkt_to_send = None

                # Find packet with this sequence number in tx_queue
                for pkt in tx_queue:
                    if pkt.seq_num == seq:
                        pkt_to_send = pkt
                        break
                
                # If packet exists and is not yet ACKed, consider sending/retransmitting
                if pkt_to_send and not acked_buffer.get(seq, False):
                    last_sent = tx_timestamps.get(seq, 0)
                    # Send if never sent or timed out
                    if last_sent == 0 or (time.ticks_diff(current_time, last_sent) > TIMEOUT_MS):
                        # LBT: random initial backoff
                        initial_backoff = random.randint(10, 40)
                        time.sleep_ms(initial_backoff)
                        sent = False
                        # Try up to MAX_LBT_RETRIES if channel is busy
                        for attempt in range(MAX_LBT_RETRIES):
                            if sx_tx.scanChannel() == sx126x.CHANNEL_FREE:
                                # Channel free, transmit packet
                                sx_tx.send(pkt_to_send.to_bytes())
                                tx_timestamps[seq] = millis()
                                sent = True
                                break
                            else:
                                # Channel busy, back off randomly
                                time.sleep_ms(random.randint(20, 50))

            # Slide window forward past any consecutive ACKed packets from window_base
            while acked_buffer.get(window_base, False):
                to_rem = next((p for p in tx_queue if p.seq_num == window_base), None)
                if to_rem:
                    tx_queue.remove(to_rem)
                # Remove bookkeeping for this sequence number
                del acked_buffer[window_base]
                if window_base in tx_timestamps:
                    del tx_timestamps[window_base]
                # Move window base forward with wraparound
                window_base = (window_base + 1) % 256
        # Small sleep to avoid hogging CPU
        time.sleep_ms(10)

# --- RECEIVER LOOP ---
def rx_loop():
    """
    Continuous receiver thread:
    - Receives LoRa packets on sx_rx
    - Handles ACK packets to update sender state
    - For data packets: sends ACK back and performs in-order delivery using
      rx_expected_seq and rx_packet_buffer (reordering buffer).
    """
    global rx_expected_seq
    while True:
        try:
            # Blocking receive with timeout
            data, err = sx_rx.recv(len=0, timeout_en=True, timeout_ms=1000)
            if len(data) > 0:
                pkt = PacketV13.from_bytes(data)
                # Ensure the packet is intended for this node
                if pkt and pkt.to_addr == MY_ADDR:
                    if pkt.pkt_type == PacketV13.TYPE_ACK:
                        # ACK packet: mark corresponding seq as acknowledged
                        with main_lock:
                            acked_buffer[pkt.seq_num] = True
                    else:
                        # Data packet: send ACK back to sender
                        ack = PacketV13(pkt.from_addr, MY_ADDR, pkt.seq_num, PacketV13.TYPE_ACK)
                        # Small randomized delay to reduce collision chance
                        time.sleep_ms(random.randint(5, 15))
                        sx_tx.send(ack.to_bytes())
                        
                        seq = pkt.seq_num
                        with main_lock:
                            # Compute distance from expected sequence number modulo 256
                            diff = (seq - rx_expected_seq) % 256
                            if diff == 0:
                                # This is exactly the next in-order packet
                                process_ordered_packet(pkt)
                                rx_expected_seq = (rx_expected_seq + 1) % 256
                                # Deliver any subsequent buffered packets in order
                                while rx_expected_seq in rx_packet_buffer:
                                    process_ordered_packet(rx_packet_buffer.pop(rx_expected_seq))
                                    rx_expected_seq = (rx_expected_seq + 1) % 256
                            elif diff < WINDOW_SIZE:
                                # Packet is within receive window but out of order: buffer it
                                if seq not in rx_packet_buffer:
                                    rx_packet_buffer[seq] = pkt
        except Exception as e:
            # Print RX error and continue listening
            print(f"[RX Error] {e}")

# Start receiver and sender threads
_thread.start_new_thread(rx_loop, ())
_thread.start_new_thread(sender_loop, ())

print("Services Started. Access Web UI at http://192.168.4.1")

# Run web server in main thread (blocking)
run_web_server()
