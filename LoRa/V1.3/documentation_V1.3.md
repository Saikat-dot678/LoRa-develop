---

# LoRa Reliable Link – Full Duplex + Web UI (v1.3)

**Project:** LoRa Point-to-Point Reliable Link
**Version:** 1.3 (Full Duplex + Web UI)
**Date:** December 2025
**Platform:** ESP32-S3 + Dual SX1262
**Status:** Experimental / Dev-Ready

---

## 1. Executive Summary

The **LoRa Reliable Link v1.3** is a point-to-point, TCP-like transport layer built on LoRa radios. It provides:

* **Reliable delivery** with **Selective Repeat ARQ**
* **Fragmentation & reassembly** for both text and files
* **CRC16-CCITT** data integrity
* **Dual-radio, frequency-split “full duplex”** operation
* A built-in **Wi-Fi Access Point + Web UI** for sending messages and files

Compared to **v1.2**, this version:

* Switches from SF12-tuned, ultra-long-range mode to a **more responsive configuration (e.g. SF7, BW 250 kHz)**
* Introduces a **simpler v1.3 packet header** (`to`, `from`, `seq`, `type` + CRC)
* Adds **file transfer support** (`FILE_START / FILE_CHUNK / FILE_END`)
* Integrates a **web server** that exposes:

  * `/api/send_msg` – send text over LoRa
  * `/api/upload_file` – upload files over LoRa
  * `/api/state` – node status + recent logs

---

## 2. System Architecture

### 2.1 Logical Layers

| Layer            | Component                                                | Responsibility                                                             |
| :--------------- | :------------------------------------------------------- | :------------------------------------------------------------------------- |
| **Application**  | Web UI (`index.html`, HTTP handlers)                     | User text/file input, UI logs, state view.                                 |
| **Transport**    | ARQ Manager (`sender_loop`, `rx_loop`, reassembly logic) | Windowing, retransmission, in-order delivery, fragmentation.               |
| **Network/Link** | `PacketV13`                                              | Addressing (`to_addr`, `from_addr`), sequence numbers, packet typing, CRC. |
| **MAC**          | LBT (`scanChannel`, random backoff)                      | Listen-Before-Talk, collision avoidance.                                   |
| **PHY**          | Dual SX1262                                              | RF modulation (LoRa), dual-radio full-duplex emulation.                    |

### 2.2 Dual-Radio “Full Duplex” Strategy

The system uses **two SX1262 modules**:

* **TX Radio (`sx_tx`)**

  * Configured to **transmit** on `FREQ_TX`
  * Performs `scanChannel()` before sending (LBT)

* **RX Radio (`sx_rx`)**

  * Configured to **receive** continuously on `FREQ_RX`

The **frequency plan** is address-dependent:

```python
MY_ADDR = 0x0B
TARGET_ADDR = 0x0A

if MY_ADDR == 0x0B:
    FREQ_TX = 866.5
    FREQ_RX = 866
else:
    FREQ_TX = 866
    FREQ_RX = 866.5
```

This way, node `0x0B` TX matches node `0x0A` RX, and vice-versa, allowing both nodes to “talk and listen” concurrently (on different frequencies).

---

## 3. Hardware Integration

The hardware pinout mirrors the v1.2 design: one SPI bus per radio. 

| Signal          | ESP32-S3 Pin | TX SX1262 | RX SX1262 | Notes         |
| :-------------- | :----------- | :-------- | :-------- | :------------ |
| **MISO (SPI1)** | GPIO 4       | MISO      | -         | TX radio SPI  |
| **MOSI (SPI1)** | GPIO 3       | MOSI      | -         | TX radio SPI  |
| **SCK (SPI1)**  | GPIO 2       | SCK       | -         | TX radio SPI  |
| **CS (SPI1)**   | GPIO 1       | NSS       | -         | TX CS         |
| **RST (TX)**    | GPIO 5       | RST       | -         | TX reset      |
| **DIO1 (TX)**   | GPIO 18      | DIO1      | -         | TX IRQ        |
| **BUSY (TX)**   | GPIO 6       | BUSY      | -         | TX busy line  |
| **MISO (SPI2)** | GPIO 9       | -         | MISO      | RX radio SPI  |
| **MOSI (SPI2)** | GPIO 10      | -         | MOSI      | RX radio SPI  |
| **SCK (SPI2)**  | GPIO 11      | -         | SCK       | RX radio SPI  |
| **CS (SPI2)**   | GPIO 12      | -         | NSS       | RX CS         |
| **RST (RX)**    | GPIO 8       | -         | RST       | RX reset      |
| **DIO1 (RX)**   | GPIO 13      | -         | DIO1      | RX IRQ        |
| **BUSY (RX)**   | GPIO 7       | -         | BUSY      | RX busy line  |
| **VCC**         | 3.3V         | VCC       | VCC       | Power         |
| **GND**         | GND          | GND       | GND       | Common ground |

> Make sure **both modules share a common ground** with the ESP32-S3.

---

## 4. Mini Protocol v1.3 – Packet Specification

### 4.1 CRC16-CCITT

All packets use **CRC16-CCITT** with:

* Initial value: `0xFFFF`
* Polynomial: `0x1021`
* Bitwise processing per byte (no table)

```python
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
```

### 4.2 Packet Layout (v1.3)

`PacketV13` uses a **fixed header** followed by payload and CRC:

```text
+------------+------------+------------+------------+-----------+-----------+
| TO (1 B)   | FROM (1 B) | SEQ (1 B)  | TYPE (1 B) | PAYLOAD… | CRC16 (2)|
+------------+------------+------------+------------+-----------+-----------+
```

* **Header format:** `HEADER_FMT = 'BBBB'`
* **Header size:** 4 bytes
* **Footer size:** 2 bytes (CRC16, big-endian)
* **Payload size:** variable (bounded by your LoRa config / app logic)

#### Serialization

```python
header = struct.pack('BBBB', to_addr, from_addr, seq_num, pkt_type)
packet_no_crc = header + payload
crc = crc16(packet_no_crc)
crc_bytes = struct.pack('>H', crc)
packet = packet_no_crc + crc_bytes
```

On receive, CRC is recalculated and compared. If mismatch → packet is discarded (`from_bytes` returns `None`).

### 4.3 Packet Types

From `PacketV13`:

```python
TYPE_ACK        = 0x01
TYPE_MSG_CHUNK  = 0x02  # Intermediate text chunk
TYPE_MSG_END    = 0x06  # Final text chunk

TYPE_FILE_START = 0x03  # File metadata "name|size"
TYPE_FILE_CHUNK = 0x04  # File content
TYPE_FILE_END   = 0x05  # File end marker
```

**Semantics:**

* `TYPE_ACK`

  * Empty payload.
  * Sent in response to any data packet (text or file).
  * Echoes the `seq_num` of the received data.

* `TYPE_MSG_CHUNK` / `TYPE_MSG_END`

  * Text messages are fragmented into **≤ 200-byte** chunks.
  * All but the last chunk → `TYPE_MSG_CHUNK`.
  * Last chunk → `TYPE_MSG_END`.
  * Receiver concatenates all payloads until `TYPE_MSG_END`, then decodes UTF-8.

* `TYPE_FILE_START`

  * Payload: `b"<filename>|<size>"`.
  * Opens a local file for writing on receiver.

* `TYPE_FILE_CHUNK`

  * File data chunks (≤ ~180 bytes).
  * Appended to the open file handle.

* `TYPE_FILE_END`

  * Closes the file and logs completion.

---

## 5. Reliability Layer – Selective Repeat ARQ

### 5.1 Sender State

Global variables:

* `tx_queue`: list of `PacketV13` waiting to be sent / retransmitted
* `window_base`: sequence number of the **oldest unacked** packet
* `next_seq_num`: next free sequence number (`0..255`, wraps)
* `acked_buffer[seq]`: `True` if `seq` has been ACKed
* `tx_timestamps[seq]`: last send time for `seq` (for timeout / retransmit)

Key constants in `main.py`:

```python
WINDOW_SIZE = 8       # Max in-flight packets
TIMEOUT_MS  = 1500    # Retransmit timeout (ms)
MAX_LBT_RETRIES = 10  # Max Listen-Before-Talk attempts
```

### 5.2 Sender Loop (`sender_loop()`)

* Runs in a **separate thread**.
* For each sequence number within the current window:

  1. Find the corresponding packet in `tx_queue`.

  2. If **not ACKed** and:

     * Never sent before, or
     * `current_time - last_sent > TIMEOUT_MS`
       → attempt to send with LBT:

     ```python
     # Random initial backoff
     time.sleep_ms(random.randint(10, 40))

     for attempt in range(MAX_LBT_RETRIES):
         if sx_tx.scanChannel() == sx126x.CHANNEL_FREE:
             sx_tx.send(pkt_to_send.to_bytes())
             tx_timestamps[seq] = millis()
             break
         else:
             time.sleep_ms(random.randint(20, 50))
     ```

  3. After sending / retries, check `acked_buffer` from `window_base` upward:

     * While `acked_buffer[window_base]` is `True`:

       * Remove that packet from `tx_queue`
       * Delete its timestamp and ack state
       * Increment `window_base` (with wraparound)

### 5.3 Receiver Loop (`rx_loop()`)

* Calls `sx_rx.recv(..., timeout_ms=1000)` repeatedly.
* For each non-empty `data`:

  1. Parse via `PacketV13.from_bytes`.
  2. Ignore packets not addressed to `MY_ADDR`.
  3. If packet type is `TYPE_ACK`:

     * Mark `acked_buffer[seq] = True`.
  4. Otherwise (data packet):

     * Immediately send a `TYPE_ACK` back to `pkt.from_addr`:

       ```python
       ack = PacketV13(pkt.from_addr, MY_ADDR, pkt.seq_num, PacketV13.TYPE_ACK)
       time.sleep_ms(random.randint(5, 15))  # small jitter
       sx_tx.send(ack.to_bytes())
       ```

     * Perform **in-order delivery** using `rx_expected_seq` and `rx_packet_buffer`:

       ```python
       diff = (seq - rx_expected_seq) % 256

       if diff == 0:
           # Next expected packet
           process_ordered_packet(pkt)
           rx_expected_seq = (rx_expected_seq + 1) % 256

           # Flush any buffered follow-ups
           while rx_expected_seq in rx_packet_buffer:
               process_ordered_packet(rx_packet_buffer.pop(rx_expected_seq))
               rx_expected_seq = (rx_expected_seq + 1) % 256

       elif diff < WINDOW_SIZE:
           # In receive window but out-of-order
           rx_packet_buffer[seq] = pkt
       ```

### 5.4 Fragmentation & Reassembly

#### Text (`queue_message()` / `process_ordered_packet()`)

* Outgoing:

  * UTF-8 text is encoded: `data = text.encode('utf-8')`
  * Split into ≤ 200-byte chunks:

    ```python
    chunks = [data[i:i+200] for i in range(0, len(data), 200)]
    ```
  * Chunks enqueued with types:

    * Middle: `TYPE_MSG_CHUNK`
    * Last: `TYPE_MSG_END`

* Incoming:

  * `TYPE_MSG_CHUNK` → append to `rx_msg_reassembly`
  * `TYPE_MSG_END` → append, then decode & log:

    ```python
    full_msg = rx_msg_reassembly.decode('utf-8')
    print(f"[RX MSG] {full_msg}")
    log_web(f"<< {full_msg}")
    rx_msg_reassembly = b''
    ```

#### Files (`queue_file()` / file part of `process_ordered_packet()`)

* Outgoing (`queue_file(filename, content)`):

  1. Enqueue `TYPE_FILE_START` with payload `b"<filename>|<size>"`.
  2. Split file content into ≤ 180-byte chunks:

     ```python
     chunks = [content[i:i+180] for i in range(0, len(content), 180)]
     ```
  3. For each chunk, enqueue `TYPE_FILE_CHUNK`.
  4. Finally enqueue `TYPE_FILE_END` with empty payload.

* Incoming (`process_ordered_packet(pkt)`):

  * `TYPE_FILE_START`:

    * Parse metadata: `name|size`
    * Open file for writing: `open(rx_file_name, 'wb')`
  * `TYPE_FILE_CHUNK`:

    * `rx_file_handle.write(pkt.payload)`
  * `TYPE_FILE_END`:

    * Close file and log completion.

---

## 6. Medium Access Control – Listen Before Talk (LBT)

Before any transmission in `sender_loop`, the code uses **scanChannel()** to detect activity:

1. Initial random backoff: `10–40 ms`
2. Up to `MAX_LBT_RETRIES` checks:

   * If `sx_tx.scanChannel() == sx126x.CHANNEL_FREE` → transmit
   * Else random backoff `20–50 ms` and try again

This reduces collisions between:

* Data packets from both nodes
* Data + ACK packets
* ARQ retransmissions

---

## 7. Wi-Fi AP + Web Server

### 7.1 Access Point Configuration

```python
WIFI_SSID = "LoRa_Node_AP"
WIFI_PASS = "12345678"

def setup_wifi():
    ap = network.WLAN(network.AP_IF)
    ap.active(True)
    ap.config(essid=f"{WIFI_SSID}_{MY_ADDR:02X}", password=WIFI_PASS)
    print(f"[WiFi] AP Created: {ap.ifconfig()[0]}")
```

* The SSID includes the node’s address, e.g. `LoRa_Node_AP_0B`.
* Default AP IP is typically `192.168.4.1` (printed on boot).

### 7.2 HTTP Endpoints

Handled inside `run_web_server()`:

1. **`GET /`**

   * Serves `index.html` from the filesystem:

     ```python
     with open('index.html', 'r') as f:
         ...
     ```

2. **`GET /api/state`**

   * Returns JSON:

     ```json
     {
       "my_addr": 11,
       "logs": [
         "... latest log messages ..."
       ]
     }
     ```
   * Logs come from `web_logs`, updated via `log_web()`.

3. **`POST /api/send_msg`**

   * Body: raw UTF-8 text.
   * Calls `queue_message(msg_text)` to send over LoRa.
   * Returns `200 OK`.

4. **`POST /api/upload_file`**

   * Content-Type: `multipart/form-data` with a file field.

   * The server:

     * Extracts the `boundary` from the header.
     * Parses the multipart body to obtain `(filename, content)`.
     * Calls `queue_file(fname, fcontent)`.

   * Responses:

     * `200 OK` on success
     * `400 Bad Request` on parse / boundary errors
     * `500 Error` on exception

### 7.3 Log Buffer

* `web_logs` holds the **last 25 log entries** (TX/RX, files, etc.).
* `log_web(msg)` handles appending with a lock.

This makes it easy to display real-time status in the browser UI.

---

## 8. Configuration Guide (v1.3 Defaults)

From `main.py`:

| Parameter         | Default           | Description                                                        |
| :---------------- | :---------------- | :----------------------------------------------------------------- |
| `MY_ADDR`         | `0x0B`            | Local node address (0–255). Must match the remote’s `TARGET_ADDR`. |
| `TARGET_ADDR`     | `0x0A`            | Remote node address.                                               |
| `FREQ_TX`         | `866 / 866.5 MHz` | TX frequency, chosen based on `MY_ADDR`.                           |
| `FREQ_RX`         | `866 / 866.5 MHz` | RX frequency (the opposite of TX).                                 |
| `LORA_SF`         | `7`               | Spreading factor – lower SF = faster, shorter range.               |
| `LORA_BW`         | `250.0` kHz       | LoRa bandwidth.                                                    |
| `WINDOW_SIZE`     | `8`               | ARQ sliding window size.                                           |
| `TIMEOUT_MS`      | `1500` ms         | Retransmission timeout for unacked packets.                        |
| `MAX_LBT_RETRIES` | `10`              | Maximum channel scan attempts before giving up this cycle.         |

You can tune these based on:

* **Range vs latency** → adjust `LORA_SF`, `LORA_BW`
* **Channel usage / congestion** → adjust `WINDOW_SIZE`, backoff ranges, `TIMEOUT_MS`

---

## 9. Example Flows

### 9.1 Sending a Text Message

1. Connect to the node’s Wi-Fi AP.

2. Open `http://192.168.4.1` in a browser.

3. Use the web UI or send:

   ```http
   POST /api/send_msg
   Content-Type: text/plain

   Hello from node 0x0B!
   ```

4. The node will:

   * Fragment text into 200-byte chunks
   * Queue them with `TYPE_MSG_CHUNK`/`TYPE_MSG_END`
   * Transmit with ARQ + LBT

5. Remote node will reassemble and log:

   ```text
   [RX MSG] Hello from node 0x0B!
   ```

### 9.2 Sending a File

1. Use the web UI file upload form **or** issue a multipart `POST /api/upload_file`.
2. Node parses the file and calls `queue_file(filename, content)`.
3. On the remote node, you’ll see logs like:

   ```text
   [RX FILE] Start: photo.jpg (12345 B)
   [RX FILE] Complete: photo.jpg
   ```

The file is saved on the ESP32 filesystem under the received `filename`.

---

## 10. Notes & Future Work

* **Mesh routing:** v1.3 is purely **point-to-point**; routing logic would sit above `PacketV13`.
* **Congestion control:** current ARQ is window-based but not rate-adaptive.
* **Security:** no encryption yet; can be layered on top of `PacketV13.payload` (e.g., AES).
* **Dynamic configuration:** SF/BW, frequencies, addresses are compile-time constants in `main.py`; a next step is runtime configurability via the web UI.

---

