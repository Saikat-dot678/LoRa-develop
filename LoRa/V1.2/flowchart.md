# LoRa Mesh System v1.2 - Protocol Flow Documentation


```mermaid
flowchart TD
    %% SUBGRAPH: MAIN THREAD
    subgraph Main_Thread [Main Application Loop]
        A1([Start]) --> A2[Init Hardware SX1262 x2]
        A2 --> A3[Start Receiver Thread]
        A3 --> A4[Start Sender Thread]
        A4 --> A5{User Input?}
        A5 -- Yes --> A6[Fragment Message 50-byte chunks]
        A6 --> A7[Create Packet Objects]
        A7 --> A8[Acquire Lock]
        A8 --> A9[Push to tx_queue & Set acked=False]
        A9 --> A10[Release Lock]
        A10 --> A5
    end

    %% SUBGRAPH: SENDER THREAD
    subgraph Sender_Thread [Thread 1: ARQ & LBT]
        B1([Start Loop]) --> B2[Acquire Lock]
        B2 --> B3[Iterate Window Size]
        B3 --> B4{Packet in Queue?}
        B4 -- No --> B13[Release Lock & Sleep]
        B4 -- Yes --> B5{Already ACKed?}
        B5 -- Yes --> B12
        B5 -- No --> B6{First Send OR Timeout?}
        B6 -- No --> B12
        B6 -- Yes --> B7[LBT: Random Backoff]
        B7 --> B8{Channel Free?}
        B8 -- No --> B9[Wait & Retry LBT]
        B9 --> B8
        B8 -- Yes --> B10[TX Module: Send Packet]
        B10 --> B11[Update Timestamp]
        B11 --> B12{Base Packet ACKed?}
        B12 -- Yes --> B14[Slide Window & Pop Queue]
        B12 -- No --> B13
        B14 --> B13
        B13 --> B1
    end

    %% SUBGRAPH: RECEIVER THREAD
    subgraph RX_Thread [Thread 2: RX & Reassembly]
        C1([Start Loop]) --> C2[RX Module: Listen 5000ms]
        C2 --> C3{Data Received?}
        C3 -- No --> C1
        C3 -- Yes --> C4{Valid Packet & My Addr?}
        C4 -- No --> C1
        C4 -- Yes --> C5{Packet Type?}
        
        %% ACK Handling
        C5 -- ACK --> C6[Acquire Lock]
        C6 --> C7[Mark acked_buffer = True]
        C7 --> C8[Release Lock]
        C8 --> C1

        %% DATA Handling
        C5 -- DATA --> C9[TX Module: Send ACK immediately]
        C9 --> C10[Acquire Lock]
        C10 --> C11{SeqNum == Expected?}
        
        %% In Order
        C11 -- Yes --> C12[Process & Print Payload]
        C12 --> C13[Increment Expected Seq]
        C13 --> C14[Check Buffer for Next Seq]
        C14 --> C15[Release Lock]
        
        %% Out of Order
        C11 -- No --> C16[Buffer Packet in Dict]
        C16 --> C15
        C15 --> C1
    end

    %% INTER-THREAD RELATIONSHIPS
    A9 -.-> B4
    C7 -.-> B5
```


## 1. System Overview
This system implements a reliable "Listen Before Talk" (LBT) protocol using a Dual-Radio architecture (ESP32 + 2x SX1262). 
* **Node A (Sender):** Uses `TX_A` for transmission and `RX_A` for confirmation.
* **Node B (Receiver):** Uses `RX_B` for continuous listening and `TX_B` for replying.

### High-Level Logic Flow
```mermaid
flowchart TD
    subgraph Node_A [Node A: Sender]
        A1([User Input]) --> A2[Queue Packet]
        A2 --> A3{Channel Free?}
        A3 -- No --> A3
        A3 -- Yes --> A4[TX_A: Send Data]
        A4 --> A5[RX_A: Wait for ACK]
    end

    subgraph Node_B [Node B: Receiver]
        B1[RX_B: Listen] --> B2{Packet Recv?}
        B2 -- Yes --> B3[Validate CRC]
        B3 --> B4[TX_B: Send ACK]
        B4 --> B5[Print Message]
    end

    A4 -.->|Radio Waves| B1
    B4 -.->|Radio Waves| A5
```

---

## 2. Detailed Process Breakdown

### Phase 1: Input & Fragmentation (Node A Internal)
**Description:**
The process begins when the user interacts with Node A.
1.  **User Input:** The user types the message "Hello" into the console of Node A.
2.  **Main Thread Processing:** The main application thread captures this input.
3.  **Fragmentation:** The system checks the message length. Since "Hello" is short, it creates a single packet (Sequence 0).
4.  **Queueing:** The Main Thread acquires a thread lock to ensure safety, pushes the packet into the `tx_queue`, and initializes its status in the `acked_buffer` as `False` (Not Acknowledged).
5.  **Status:** The packet is now waiting in memory, ready for the Sender Thread to pick it up.

```mermaid
sequenceDiagram
    participant User
    participant Main_Thread as Node A (Main)
    participant Memory as Shared Memory (Queue)

    User->>Main_Thread: Inputs "Hello"
    Main_Thread->>Main_Thread: Fragment into [Seq 0]
    Main_Thread->>Memory: Lock & Push [Seq 0] to tx_queue
    Main_Thread->>Memory: Set acked_buffer[0] = False
    Note over Memory: Packet is now queued<br/>waiting for TX Thread
```

---

### Phase 2: Transmission & LBT (Node A -> Air)
**Description:**
The dedicated Sender Thread on Node A handles the physical transmission.
1.  **Wake Up:** The Sender Thread wakes up and iterates through the `tx_queue`. It sees `[Seq 0]` is waiting and has not been ACKed.
2.  **LBT (Listen Before Talk):**
    * The thread commands module **TX_A** to scan the current frequency.
    * It measures the RSSI (Signal Strength) of the noise floor.
    * If the channel is busy, it waits/sleeps (Backoff).
    * If the channel is free, it proceeds.
3.  **Transmission:** The Sender Thread commands **TX_A** to transmit the packet.
4.  **Timestamping:** Immediately after sending, Node A records the current time. This is used to trigger a retransmission (Timeout) later if no confirmation arrives.

```mermaid
sequenceDiagram
    participant Sender_Thread as Node A (Sender Thread)
    participant TX_A as Hardware: TX_A
    participant Air as The Airwaves

    Sender_Thread->>Sender_Thread: Read [Seq 0] from Queue
    loop Listen Before Talk (LBT)
        Sender_Thread->>TX_A: scanChannel()
        TX_A-->>Sender_Thread: Result: Channel Free
    end
    Sender_Thread->>TX_A: send(Payload="Hello")
    TX_A->>Air: Radio Waves [Seq 0: "Hello"]
    Note over Sender_Thread: Start Timeout Timer
```

---

### Phase 3: Reception & Immediate Reply (Node B)
**Description:**
Node B receives the message and ensures reliability by replying.
1.  **Continuous Listening:** Module **RX_B** on Node B has been in receiving mode continuously.
2.  **Packet Capture:** **RX_B** detects the preamble and captures the "Hello" packet.
3.  **Validation:** The Receiver Thread checks the CRC (Cyclic Redundancy Check) and confirms the destination address matches Node B.
4.  **The Hand-off:** The Receiver Thread immediately identifies this as a `DATA` packet. It constructs an `ACK` packet.
5.  **ACK Transmission:** The Receiver Thread commands **TX_B** (Node B's transmitter) to send the Acknowledgement back to Node A. This is the "Dual-Radio" advantage—receiving on one, replying on the other.
6.  **Application Output:** The message "Hello" is printed to Node B's screen.

```mermaid
sequenceDiagram
    participant Air as The Airwaves
    participant RX_B as Hardware: RX_B
    participant Recv_Thread as Node B (RX Thread)
    participant TX_B as Hardware: TX_B

    Air->>RX_B: Radio Waves [Seq 0: "Hello"]
    RX_B->>Recv_Thread: Interrupt: Packet Received
    Recv_Thread->>Recv_Thread: Validate CRC & Address
    
    par Parallel Actions
        Recv_Thread->>TX_B: send(ACK [Seq 0])
        TX_B->>Air: Radio Waves [ACK]
    and
        Recv_Thread->>Recv_Thread: Process & Print "Hello"
    end
```

---

### Phase 4: Closing the Loop (Node A Receives ACK)
**Description:**
Node A confirms delivery and cleans up its memory.
1.  **Listening:** While **TX_A** was sending, module **RX_A** was listening in the background.
2.  **ACK Capture:** **RX_A** receives the ACK packet sent by Node B.
3.  **State Update:**
    * The Receiver Thread on Node A processes the ACK.
    * It acquires the thread lock and updates the shared memory: `acked_buffer[0] = True`.
4.  **Window Slide:**
    * The Sender Thread (from Phase 2) checks the status again.
    * It sees that `[Seq 0]` is now `True`.
    * It removes `[Seq 0]` from the queue and "slides the window" forward to accept new messages (e.g., Seq 1).

```mermaid
sequenceDiagram
    participant Air as The Airwaves
    participant RX_A as Hardware: RX_A
    participant RX_Thread_A as Node A (RX Thread)
    participant Memory as Shared Memory
    participant Sender_Thread_A as Node A (Sender Thread)

    Air->>RX_A: Radio Waves [ACK]
    RX_A->>RX_Thread_A: Packet Received
    RX_Thread_A->>Memory: Lock & Set acked_buffer[0] = True
    
    Note over Sender_Thread_A: Context Switch
    
    Sender_Thread_A->>Memory: Check acked_buffer[0]
    Memory-->>Sender_Thread_A: Value is TRUE
    Sender_Thread_A->>Memory: Remove [Seq 0] from Queue
    Note over Sender_Thread_A: Window Slides Forward
```
