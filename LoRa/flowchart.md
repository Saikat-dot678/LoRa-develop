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
