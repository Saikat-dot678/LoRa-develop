from machine import Pin
from sx1262 import SX1262
import time, _thread, random, network, socket, json

# Custom protocol definitions for parsing and building network frames
from beacon_protocol import BeaconPacket, ControlPacket, DataPacket, JoinReqPacket, HubSchedPacket, \
     TYPE_BEACON, TYPE_CONTROL, TYPE_DATA_REQ, TYPE_JOIN_REQ, TYPE_HUB_SCHED, TYPE_MSG_CHUNK, TYPE_FILE_CHUNK
from slot_manager import SlotManager
from config_loader import load_identity
from utils2 import get_network_time, set_network_time, log, web_logs

# ==========================================
# --- CONFIGURATION & IDENTITY ---
# ==========================================
id_data = load_identity()
# Node's unique address in the network. Defaults to 0x02 if not found.
MY_ADDR = id_data.get("my_addr", 0x02) 

# ==========================================
# --- STATE VARIABLES ---
# ==========================================
current_role = "LISTENER"  # Roles: LISTENER (boot), HUB (coordinator), CLIENT (node)
sync_source = "UNSYNCED"   # Tracks where network time comes from (Phone, Hub, or Self)
last_beacon_time = time.ticks_ms()

# Dynamic watchdog: Base 70s + staggered offset based on MAC/ADDR. 
# Staggering prevents all nodes from trying to become the Hub at the exact same millisecond.
WATCHDOG_TIMEOUT = 70000 + (MY_ADDR * 1000) 
missed_beacons = 0  

# ==========================================
# --- GPS AND SPATIAL AWARENESS ---
# ==========================================
my_lat = 0.0
my_lon = 0.0
# Dictionary mapping Node IDs to their last known GPS coordinates
node_locations = {} # Hub stores network map here: {addr: {"lat": x, "lon": y}}

# Test payload for Node 2 to transmit once connected
outgoing_payload = b"TDMA + GPS Network is ALIVE!" if MY_ADDR == 2 else b""

# SlotManager handles the TDMA timing logic (when to send/receive)
sm = SlotManager(MY_ADDR)
active_nodes = []   # List of node addresses currently in the network
pending_reqs = []   # Queue of nodes requesting data slots (Hub use only)
is_joined = False   # Network join status
last_phase = ""     # Tracks the previous TDMA phase to detect transitions

log(f"Booting Node 0x{MY_ADDR:02X}. Waiting for Phone Sync or Hub Beacon...", save_to_file=True)

# ==========================================
# --- HARDWARE & FREQUENCY SETUP ---
# ==========================================
# Define 6 frequency lanes for hopping. Base is 865.10/866.10, offset by 0.15 MHz per lane.
# Format: { Lane_ID: (TX_Freq, RX_Freq) }
FREQ_PAIRS = {i: (865.10 + i*0.15, 866.10 + i*0.15) for i in range(6)}

def get_sx(bus, cs, irq, rst, gpio, f):
    """Initializes an SX1262 LoRa module on a specific SPI bus with given parameters."""
    obj = SX1262(spi_bus=bus, clk=Pin(2 if bus==1 else 11), mosi=Pin(3 if bus==1 else 10), 
                 miso=Pin(4 if bus==1 else 9), cs=Pin(cs), irq=Pin(irq), rst=Pin(rst), gpio=Pin(gpio))
    # Standard LoRa params: 125kHz bandwidth, Spreading Factor 7, Coding Rate 4/5
    obj.begin(freq=f, bw=125.0, sf=7, cr=5, syncWord=0x1424, power=22)
    return obj

# Default to Lane 0 frequencies on boot
tx_f, rx_f = FREQ_PAIRS[0]
last_tx_f = tx_f
target_rx_f = rx_f  

log(f"[Init] Starting Hardware -> TX: {tx_f:.2f} MHz | RX: {rx_f:.2f} MHz")

# Dual radio setup: Dedicated TX module (SPI 1) and RX module (SPI 2)
sx_tx = get_sx(1, 1, 18, 5, 6, tx_f)
sx_rx = get_sx(2, 12, 13, 8, 7, rx_f)

def switch_lane(p, peer_addr=None):
    """
    Handles Frequency Hopping logic. Swaps TX/RX frequencies based on the node's role 
    and the target peer to ensure duplex paths align correctly.
    """
    global last_tx_f, target_rx_f
    t, r = FREQ_PAIRS[p]
    
    if p == 0:
        # On Lane 0 (Control/Beacon lane), Hub listens on the frequency Clients transmit on.
        if current_role == "HUB": t, r = r, t 
    else:
        # On Data Lanes (>0), nodes sort frequencies based on MAC address magnitude 
        # to prevent TX/TX or RX/RX mismatches when talking peer-to-peer.
        if peer_addr is not None:
            if MY_ADDR > peer_addr: t, r = r, t
        elif current_role == "HUB": t, r = r, t

    # Apply new TX frequency immediately if it changed
    if t != last_tx_f:
        sx_tx.setFrequency(t)
        last_tx_f = t
        log(f"[Freq Switch] TX tuned to {t:.2f} MHz")
        time.sleep_ms(15)

    # Queue new RX frequency (applied safely in the rx_loop)
    if r != target_rx_f:
        target_rx_f = r

# ==========================================
# --- COLLISION AVOIDANCE (CSMA) ---
# ==========================================
def csma_backoff():
    """Carrier Sense Multiple Access pseudo-backoff. Random delay before transmitting to avoid packet collisions."""
    delay = random.randint(300, 4500)
    log(f"[CSMA] Channel Activity Detection Wait: {delay}ms...")
    time.sleep_ms(delay)

# ==========================================
# --- MAIN TRANSMIT ENGINE (TDMA STATE MACHINE) ---
# ==========================================
def sender_loop():
    global current_role, sync_source, is_joined, last_phase, missed_beacons, outgoing_payload
    # Flags to ensure we only send one packet per phase per TDMA frame
    flags = {"b":0, "c":0, "r":0, "s":0, "d":0} 
    
    while True:
        try:
            now_ticks = time.ticks_ms()

            # State: Just booted, waiting for phone to provide initial timestamp
            if sync_source == "UNSYNCED":
                switch_lane(0) 
                time.sleep_ms(100)
                continue 

            # State: Waiting to hear a Hub. If timeout is reached, promote self to Hub.
            if current_role == "LISTENER":
                switch_lane(0)
                if (now_ticks - last_beacon_time) > WATCHDOG_TIMEOUT:
                    log(f"WATCHDOG TRIGGERED! Promoting to HUB.", save_to_file=True)
                    current_role = "HUB"
                    sync_source = "SELF (HUB)"
                    active_nodes.clear()
                    active_nodes.append(MY_ADDR)
                    is_joined = True
                    # Round network time to nearest minute to align TDMA frames
                    now_net = get_network_time()
                    set_network_time(now_net - (now_net % 60000))
                    last_phase = "" 
                time.sleep_ms(100)
                continue 

            # Update TDMA timings
            sm.update()
            tis = sm.time_in_slot
            curr_phase = sm.get_current_phase()

            # Handle phase transitions and reset flags for the new frame
            if curr_phase != last_phase:
                log(f"--- Transitioning to {curr_phase} ---")
                if curr_phase == "1. BEACON (SYNC)":
                    flags = {k:0 for k in flags} # Reset all transmission flags
                    sm.assigned_lane = 0         # Force everyone back to control lane
                elif curr_phase == "2. CONTROL/JOIN":
                    # Client health check: If we miss too many beacons, trigger Hub election
                    if current_role == "CLIENT":
                        if (time.ticks_ms() - last_beacon_time) > 15000:
                            missed_beacons += 1
                            if missed_beacons > MY_ADDR: # Staggered failover
                                log("HUB LOST! Promoting to HUB.", save_to_file=True)
                                current_role = "HUB"
                                sync_source = "SELF (HUB)"
                                active_nodes.clear()
                                active_nodes.append(MY_ADDR)
                                is_joined = True
                                now_net = get_network_time()
                                set_network_time(now_net - (now_net % 60000))
                        else: missed_beacons = 0 
                last_phase = curr_phase

            # ----------------------------------------
            # PHASE 1: BEACON (Hub synchronization)
            # ----------------------------------------
            if tis < sm.PHASE_BEACON_END:
                switch_lane(0)
                # Hub broadcasts the beacon to sync all client clocks and share active nodes
                if current_role == "HUB" and not flags["b"]:
                    now_net = get_network_time()
                    current_frame_start = now_net - (now_net % 60000) 
                    sx_tx.send(BeaconPacket(MY_ADDR, now_net, current_frame_start, 4-sm.slot_idx, active_nodes).to_bytes())
                    flags["b"] = 1
                    log(f"[TX] Beacon Sent (Active Nodes: {len(active_nodes)})")

            # ----------------------------------------
            # PHASE 2: CONTROL / JOIN (Client registration)
            # ----------------------------------------
            elif tis < sm.PHASE_CONTROL_END:
                if current_role == "CLIENT" and not flags["c"]:
                    csma_backoff() # Use randomized wait to prevent network storm
                    
                    if not is_joined:
                        # New node asking to enter the network, shares GPS
                        sx_tx.send(JoinReqPacket(MY_ADDR, my_lat, my_lon).to_bytes())
                        flags["c"] = 1
                        log(f"[TX] Join Request Sent with GPS ({my_lat:.4f}, {my_lon:.4f})", save_to_file=True)
                    else:
                        # Existing node sending alive heartbeat and updated GPS
                        sx_tx.send(ControlPacket(MY_ADDR, my_lat, my_lon).to_bytes())
                        flags["c"] = 1
                        log(f"[TX] Heartbeat Sent with GPS ({my_lat:.4f}, {my_lon:.4f})")

            # ----------------------------------------
            # PHASE 3: DATA REQUEST (Clients ask to transmit)
            # ----------------------------------------
            elif tis < sm.PHASE_DATAREQ_END:
                if current_role == "CLIENT" and is_joined and not flags["r"]:
                    if len(outgoing_payload) > 0:
                        # Node has data. Wait randomly, then raise hand to Hub
                        time.sleep_ms(random.randint(500, 3000)) 
                        sx_tx.send(DataPacket(1, MY_ADDR, 0, TYPE_DATA_REQ, b'RQ').to_bytes())
                        log("[TX] Hand raised! Data Request Sent.", save_to_file=True)
                    flags["r"] = 1

            # ----------------------------------------
            # PHASE 3.5: HUB SCHEDULING (Hub assigns lanes)
            # ----------------------------------------
            elif tis < sm.PHASE_SCHED_END:
                if current_role == "HUB" and not flags["s"]:
                    # Assign a data lane (1-5) to each requesting client
                    asgn = [(addr, (i%5)+1) for i, addr in enumerate(pending_reqs)]
                    sx_tx.send(HubSchedPacket(asgn).to_bytes())
                    
                    if len(asgn) > 0: sm.assigned_lane = asgn[0][1] 
                    else: sm.assigned_lane = 0
                    pending_reqs.clear()
                    flags["s"] = 1
                    
                    # Hub prepares its own radio to listen on the assigned lane
                    if sm.assigned_lane > 0:
                        switch_lane(sm.assigned_lane, peer_addr=asgn[0][0])
                    log(f"[TX] Schedule Broadcasted. Assignments: {asgn}")
                
                # Failsafe: If Client has a lane but missed the schedule confirmation, ping Hub
                if current_role == "CLIENT" and not flags["s"] and sm.assigned_lane > 0:
                    time.sleep_ms(1500) 
                    sx_tx.send(ControlPacket(MY_ADDR, my_lat, my_lon).to_bytes())
                    log("[TX] Wake-up ping sent to rescue Hub!")
                    flags["s"] = 1

            # ----------------------------------------
            # PHASE 4: DATA TRANSFER (Payload delivery)
            # ----------------------------------------
            else:
                if sm.assigned_lane > 0:
                    hub_addr = 1 
                    # Extract dynamic Hub Address from string
                    if "0x" in sync_source:
                        try: hub_addr = int(sync_source.split("0x")[1].replace(")", ""), 16)
                        except: pass
                    
                    # Both Hub and Client switch to the assigned frequency lane
                    switch_lane(sm.assigned_lane, peer_addr=hub_addr)
                    
                    if len(outgoing_payload) > 0 and current_role == "CLIENT" and not flags["d"]:
                        time.sleep_ms(1500) # Buffer to ensure radios are tuned
                        sx_tx.send(DataPacket(hub_addr, MY_ADDR, 1, TYPE_MSG_CHUNK, outgoing_payload).to_bytes())
                        log(f"[TX] PAYLOAD FIRED: {outgoing_payload.decode('utf-8')}", save_to_file=True)
                        flags["d"] = 1
                        outgoing_payload = b"" # Clear payload after sending
                        
                else: 
                    # No data assigned, return to control lane
                    switch_lane(0)

            time.sleep_ms(50) # Yield to prevent watchdog crash
        except Exception as e: log(f"TX Error: {e}")

# ==========================================
# --- MAIN RECEIVE ENGINE ---
# ==========================================
def rx_loop():
    global current_role, sync_source, is_joined, last_beacon_time, target_rx_f
    current_rx_f = FREQ_PAIRS[0][1] 
    
    while True:
        try:
            # Safely apply frequency changes dictated by the TX engine/state machine
            if current_rx_f != target_rx_f:
                sx_rx.setFrequency(target_rx_f)
                current_rx_f = target_rx_f
                log(f"[Freq Switch] RX safely tuned to {current_rx_f:.2f} MHz")
                time.sleep_ms(15)

            # Block and wait for incoming packet
            data, _ = sx_rx.recv(timeout_ms=500)
            if data:
                t = data[0] # First byte is the packet type header
                
                # --- BEACON RECEIVED ---
                if t == TYPE_BEACON:
                    b = BeaconPacket.from_bytes(data)
                    if b:
                        last_beacon_time = time.ticks_ms()
                        # If we aren't the hub, sync our clocks to the hub
                        if current_role != "HUB":
                            set_network_time(b.net_time) 
                            time_since_frame_start = b.net_time - b.frame_start
                            sm.time_in_slot = time_since_frame_start
                            sync_source = f"HUB (0x{b.hub_id:02X})"
                            
                            # Auto-demote to Client if a Hub is found
                            if current_role == "LISTENER":
                                log(f"Heard Hub 0x{b.hub_id:02X}. Switching to CLIENT role.", save_to_file=True)
                                current_role = "CLIENT"
                                switch_lane(0) 
                            
                            # Sync network map
                            active_nodes.clear()
                            active_nodes.extend(b.active_nodes)
                            if MY_ADDR in active_nodes and not is_joined:
                                is_joined = True
                                log("Successfully joined the network!", save_to_file=True)
                            
                # --- JOIN REQUEST RECEIVED (Hub only) ---
                elif t == TYPE_JOIN_REQ and current_role == "HUB":
                    j = JoinReqPacket.from_bytes(data)
                    if j:
                        if j.node_addr not in active_nodes: 
                            active_nodes.append(j.node_addr)
                        node_locations[j.node_addr] = {"lat": j.lat, "lon": j.lon} # Store GPS
                        log(f"[RX] Node 0x{j.node_addr:02X} joined at ({j.lat:.4f}, {j.lon:.4f})", save_to_file=True)

                # --- CONTROL/HEARTBEAT RECEIVED (Hub only) ---
                elif t == TYPE_CONTROL and current_role == "HUB":
                    c = ControlPacket.from_bytes(data)
                    if c:
                        node_locations[c.src] = {"lat": c.lat, "lon": c.lon} # Store GPS update
                        log(f"[RX] Heartbeat from 0x{c.src:02X} at ({c.lat:.4f}, {c.lon:.4f})")
                        
                # --- DATA REQUEST RECEIVED (Hub only) ---
                elif t == TYPE_DATA_REQ and current_role == "HUB":
                    d = DataPacket.from_bytes(data)
                    if d and d.from_addr not in pending_reqs: 
                        # Add client to the queue for the scheduling phase
                        pending_reqs.append(d.from_addr)
                        log(f"[RX] Data Request received from Node 0x{d.from_addr:02X}", save_to_file=True)
                        
                # --- HUB SCHEDULE RECEIVED (Clients only) ---
                elif t == TYPE_HUB_SCHED and current_role == "CLIENT":
                    s = HubSchedPacket.from_bytes(data)
                    if s:
                        # Check if Hub assigned us a frequency lane
                        sm.assigned_lane = next((l for a, l in s.assignments if a == MY_ADDR), 0)
                        if sm.assigned_lane > 0:
                            log(f"[RX] Hub assigned us to Data Lane {sm.assigned_lane}!", save_to_file=True)
                            hub_addr = 1 
                            if "0x" in sync_source:
                                try: hub_addr = int(sync_source.split("0x")[1].replace(")", ""), 16)
                                except: pass
                            
                            # Pre-tune RX frequency to the designated lane
                            _, next_r = FREQ_PAIRS[sm.assigned_lane]
                            if MY_ADDR > hub_addr: next_r = FREQ_PAIRS[sm.assigned_lane][0]
                            target_rx_f = next_r

                # --- ACTUAL DATA PAYLOAD RECEIVED ---
                elif t == TYPE_MSG_CHUNK or t == TYPE_FILE_CHUNK:
                    d = DataPacket.from_bytes(data)
                    if d:
                        msg = d.payload.decode('utf-8')
                        log(f"🟢 [INCOMING MESSAGE] From Node 0x{d.from_addr:02X}: {msg}", save_to_file=True)
                        
        except Exception as e: pass

# Start dual-core multi-threading for concurrent TX and RX operations
_thread.start_new_thread(rx_loop, ())
_thread.start_new_thread(sender_loop, ())

# ==========================================
# --- WEB DASHBOARD & PHONE SYNC API ---
# ==========================================
def run_web():
    """Hosts a local WiFi Access Point and HTTP server to serve the dashboard and API."""
    global sync_source, my_lat, my_lon 
    w = network.WLAN(network.AP_IF); w.active(True)
    ssid = f"NODE_{hex(MY_ADDR).upper().replace('0X', '')}_NET"
    w.config(essid=ssid, authmode=0) # Open network for easy phone connection
    log(f"Dashboard AP Active: {ssid}")

    # Standard blocking sockets
    s = socket.socket(); s.bind(('', 80)); s.listen(3)
    while True:
        try:
            cl, _ = s.accept(); cl.settimeout(2.0)
            r = cl.recv(1024).decode()
            
            # --- API ENDPOINT: Phone syncs NTP time and GPS to the ESP32 ---
            if "/api/set_time" in r:
                try:
                    # Parse the raw HTTP GET request query string manually
                    query = r.split(" /api/set_time?")[1].split(" ")[0]
                    params = dict(p.split('=') for p in query.split('&'))
                    
                    # Apply time if we don't have it yet
                    if "epoch" in params and sync_source == "UNSYNCED":
                        set_network_time(int(params["epoch"]))
                        sync_source = "PHONE (NTP)"  
                        
                    # Apply GPS coordinates
                    if "lat" in params and "lon" in params:
                        my_lat = float(params["lat"])
                        my_lon = float(params["lon"])
                        
                    log(f"Phone Sync: Time and GPS ({my_lat:.4f}, {my_lon:.4f}) captured.", save_to_file=True)
                except Exception as e: 
                    print(f"Sync Parsing Error: {e}")
                cl.send("HTTP/1.1 200 OK\r\n\r\nOK")

            # --- API ENDPOINT: Frontend Dashboard polling node state ---
            elif "/api/state" in r:
                # Expose the internal network map and TDMA state to the frontend
                res = {
                    "addr": hex(MY_ADDR), "role": current_role, "sync": sync_source,
                    "phase": sm.get_current_phase(), "slot": sm.slot_idx+1, 
                    "active": [hex(n) for n in active_nodes], 
                    "locations": node_locations,
                    "logs": web_logs
                }
                cl.send("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n" + json.dumps(res))
            
            # --- ROOT: Serve the HTML dashboard ---
            else:
                with open("index.html", "r") as f: 
                    cl.send("HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n" + f.read())
            cl.close()
        except: 
            try: cl.close()
            except: pass

# Start the blocking web server on the main thread
run_web()
