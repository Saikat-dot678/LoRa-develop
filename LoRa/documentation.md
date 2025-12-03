# LoRa Mesh Communication System - Technical Manual v1.2

**Project:** LoRa Mesh / Point-to-Point Reliable Link  
**Version:** 1.2 (Stable - SF12 Optimized)  
**Date:** December 03, 2025  
**Platform:** ESP32-S3 + Dual SX1262  
**Status:** Released

---

## 1. Executive Summary

The LoRa Mesh System v1.2 is a robust, long-range communication protocol designed for decentralized networks. Unlike standard LoRa implementations which operate on a "fire-and-forget" basis, v1.2 introduces a **Reliable Transport Layer** modeled after TCP. 

This version specifically addresses packet loss and collision issues identified in v1.1 by implementing **Selective Repeat ARQ** (Automatic Repeat Request), **Packet Fragmentation**, and **CRC Data Integrity**. It utilizes a unique dual-radio architecture to emulate full-duplex communication, significantly reducing latency and "blind spots" during transmission.

**v1.2 Optimization:** This release is specifically tuned for **Spreading Factor 12 (SF12)**, accommodating the extremely long "Time on Air" (ToA) required for maximum range communication.

---

## 2. System Architecture

The system is built on a layered architecture, separating the physical transmission handling from the application logic.

### 2.1 Logical Layers

| Layer | Component | Functionality |
| :--- | :--- | :--- |
| **Application** | `User Input / CLI` | Handles text input, display, and command parsing. |
| **Transport** | `ARQ Manager` | Manages Sequencing, Windowing, Retransmission, and Reassembly. |
| **Network/Link** | `MiniProtocol` | Packet framing, Addressing (Src/Dst), and CRC generation. |
| **MAC** | `LBT Algorithm` | Carrier Sense (CAD), Random Backoff, Collision Avoidance. |
| **PHY** | `SX1262 Driver` | SPI communication, RF modulation (LoRa), Frequency setting. |

### 2.2 Dual-Radio Strategy
To overcome the half-duplex nature of LoRa radios (cannot listen while transmitting), v1.2 employs two physical modules:
* **Radio A (TX):** Dedicated to outgoing traffic. It stays in Standby and performs CAD (Channel Activity Detection) before every transmission.
* **Radio B (RX):** Dedicated to incoming traffic. It remains in continuous RX mode with a maximized timeout window (5000ms+) to capture slow SF12 packets.

---

## 3. Hardware Integration

This section details the physical connection between the MCU (ESP32-S3) and the LoRa modules.

### 3.1 Pin Configuration Table

| Signal | ESP32-S3 Pin | SX1262 Module 1 (TX) | SX1262 Module 2 (RX) | Function |
| :--- | :--- | :--- | :--- | :--- |
| **MISO** | GPIO 4 | MISO | - | Master In Slave Out (SPI 1) |
| **MOSI** | GPIO 3 | MOSI | - | Master Out Slave In (SPI 1) |
| **SCK** | GPIO 2 | SCK | - | Serial Clock (SPI 1) |
| **NSS/CS** | GPIO 1 | NSS | - | Chip Select (Active Low) |
| **RST** | GPIO 5 | RST | - | Reset |
| **DIO1** | GPIO 18 | DIO1 | - | IRQ (TxDone/CadDone) |
| **BUSY** | GPIO 6 | BUSY | - | Status Line |
| **MISO** | GPIO 9 | - | MISO | Master In Slave Out (SPI 2) |
| **MOSI** | GPIO 10 | - | MOSI | Master Out Slave In (SPI 2) |
| **SCK** | GPIO 11 | - | SCK | Serial Clock (SPI 2) |
| **NSS/CS** | GPIO 12 | - | NSS | Chip Select |
| **RST** | GPIO 8 | - | RST | Reset |
| **DIO1** | GPIO 13 | - | DIO1 | IRQ (RxDone) |
| **BUSY** | GPIO 7 | - | BUSY | Status Line |
| **VCC** | 3.3V | VCC | VCC | Power Supply |
| **GND** | GND | GND | GND | Ground |

> **Note:** Ensure a common ground is shared between the ESP32 and both LoRa modules.

---

## 4. Protocol Specification (v1.2)

The "Mini Protocol" v1.2 uses a bit-packed binary header to minimize airtime.

### 4.1 Packet Layout

**Total Header Size:** 3 Bytes  
**Total Footer Size:** 2 Bytes  
**Max Payload:** 50 Bytes  

| Byte Offset | 7 | 6 | 5 | 4 | 3 | 2 | 1 | 0 | Description |
| :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :--- |
| **0** | **TO ADDR [3:0]** | **FROM ADDR [3:0]** | Address Byte. 4 bits per address (Max 16 nodes). |
| **1** | **SEQ NUM [3:0]** | **PKT TYPE [3:0]** | Info Byte. Sequence ID (0-15) and Type Enum. |
| **2** | **PAYLOAD LENGTH [7:0]** | Length of data payload (0-50). |
| **3...N** | **PAYLOAD DATA** | Variable length user data. |
| **N+1** | **CRC16 [15:8]** | High byte of CRC16-CCITT checksum. |
| **N+2** | **CRC16 [7:0]** | Low byte of CRC16-CCITT checksum. |

### 4.2 Packet Types
* **`0x01` (TYPE_DATA):** An intermediate fragment of a larger message. Receiver should buffer this.
* **`0x02` (TYPE_ACK):** Acknowledgment packet. Payload length is 0. Contains the SEQ number being acknowledged.
* **`0x03` (TYPE_DATA_END):** The final fragment of a message. Triggers reassembly and display on the receiver.

---

## 5. Reliability Layer (ARQ)

v1.2 implements **Selective Repeat ARQ**. This mechanism allows the sender to transmit multiple packets (the "window") without waiting for individual ACKs, boosting throughput compared to Stop-and-Wait.

### 5.1 Sender Logic (Sliding Window)
1.  **Window Size:** Fixed at **4** (optimized for SF12 congestion control).
2.  **Operation:** The sender maintains a list of sent packets.
    * If `ACK` for Seq `N` is received: Mark `N` as delivered.
    * If `ACK` for Seq `N` is NOT received within `TIMEOUT_MS`: Retransmit packet `N`.
    * **Slide:** If the base of the window (oldest packet) is ACKed, shift the window to `N+1` and transmit the next queued packet.

### 5.2 Receiver Logic (Reassembly Buffer)
1.  **In-Order:** If received Seq equals `Expected Seq`:
    * Append payload to buffer.
    * Increment `Expected Seq`.
    * Check "Out-of-Order" buffer for the *next* packet.
2.  **Out-Of-Order:** If received Seq > `Expected Seq`:
    * Store packet in `rx_packet_buffer`.
    * Do NOT display yet.
    * Send `ACK` (to prevent sender timeout).
3.  **Completion:** When a packet of type `DATA_END` is processed in order, the buffer is decoded to UTF-8 and printed.

---

## 6. Medium Access Control (LBT)

To prevent packet collisions, especially during ARQ retransmissions, the system uses Listen Before Talk.

### 6.1 LBT Algorithm Flowchart
1.  **Request to Send:** Packet selected for transmission.
2.  **Desync:** Wait `random(50ms, 200ms)`. *Increased in v1.2 for SF12 compatibility.*
3.  **CAD Scan:** Radio A checks channel noise floor.
    * **Busy:** Wait `random(100ms, 300ms)`. Retry (Max 5 times).
    * **Free:** Transmit immediately.
4.  **Drop:** If 5 retries fail, abort transmission for this cycle (Sender ARQ will retry later).

---

## 7. Configuration Guide

The following constants in `main.py` dictate system performance. These specific values are tuned for **SF12**.

| Parameter | Recommended Value | Impact |
| :--- | :--- | :--- |
| **`SF`** | 12 | **Spreading Factor.** Longest range, highest immunity to noise, but very slow data rate. |
| **`TIMEOUT_MS`** | 10000 | **ARQ Timeout.** Set to 10 seconds. Since one packet takes ~2s to fly, the round trip is ~4s-6s. 10s prevents premature retries. |
| **`WINDOW_SIZE`** | 4 | **ARQ Window.** Kept small to prevent flooding the channel at slow data rates. |
| **`RX_TIMEOUT`** | 5000 | **Radio Timeout.** Set to 5 seconds. Vital for SF12; ensures the radio doesn't stop listening while a slow packet is still arriving. |
| **`MY_ADDR`** | 0x00 - 0x0F | **Device ID.** Must be unique. |

---

## 8. Troubleshooting & FAQ

### Issue: "I see [TX] Success logs but no [RX] logs on the other side."
* **Cause 1 (SF12):** The packet is taking too long to fly.
    * *Fix:* Ensure `timeout_ms` in `sx_rx.recv()` is at least 4000-5000ms.
* **Cause 2 (CRC):** Signal is too weak or colliding.
    * *Fix:* Check antenna connections. Reduce distance.
* **Cause 3 (Addressing):** `TARGET_ADDR` on sender does not match `MY_ADDR` on receiver.

### Issue: "The system feels very slow."
* **Explanation:** This is expected behavior for SF12.
    * Time on Air for 1 packet @ SF12 ≈ 1.5 to 2.0 seconds.
    * ACK return time ≈ 1.5 to 2.0 seconds.
    * Total Round Trip ≈ 4.0 seconds per packet.
* *Optimization:* Switch to SF9 if maximum range (>5km) is not strictly required.

### Issue: "Channel Congested" or "LBT Busy" logs.
* **Cause:** With SF12, the airtime is long. If two nodes try to talk, the channel stays "Busy" for seconds at a time.
* **Fix:** The code handles this via `random(100, 300)` backoff. Do not lower this value, or you will create a collision loop.

---
