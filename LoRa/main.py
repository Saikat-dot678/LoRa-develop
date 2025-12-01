from machine import Pin, Timer
from sx1262 import SX1262
import sx126x
import time
import _thread
recv_msg_queue = []
# LoRa Init 01
sx01 = SX1262(spi_bus=1, clk=Pin(2), mosi=Pin(3), miso=Pin(4), cs=Pin(1), irq=Pin(18), rst=Pin(5), gpio=Pin(6))

# sx01.begin(
#     freq=866, 
#     bw=125.0, 
#     sf=7, 
#     cr=8, 
#     syncWord=0x3444,
#     power=17, 
#     currentLimit=60.0, 
#     preambleLength=8,
#     crcOn=True, 
#     tcxoVoltage=1.7,
#     blocking=True)

sx01.begin(
    freq=866, 
    bw=125.0, 
    sf=12, 
    cr=8, 
    syncWord=0x3444,
    power=22, 
    currentLimit=140.0, 
    preambleLength=16,
    crcOn=True, 
    tcxoVoltage=1.7,
    blocking=True)

# LoRa Init 02
sx02 = SX1262(spi_bus=2, clk=Pin(11), mosi=Pin(10), miso=Pin(9), cs=Pin(12), irq=Pin(13), rst=Pin(8), gpio=Pin(7))

# sx02.begin(
#     freq=866, 
#     bw=125.0, 
#     sf=7, 
#     cr=8, 
#     syncWord=0x3444,
#     power=17, 
#     currentLimit=60.0, 
#     preambleLength=8,
#     crcOn=True, 
#     tcxoVoltage=1.7,
#     blocking=True)

sx02.begin(
    freq=866, 
    bw=125.0, 
    sf=12, 
    cr=8, 
    syncWord=0x3444,
    power=22, 
    currentLimit=140.0, 
    preambleLength=16,
    crcOn=True, 
    tcxoVoltage=1.7,
    blocking=True)


def tx_mode():
    msg = "node02"
    
    while True :
        
        p = sx01.scanChannel()
        print(p)
        if p == -15 :
            sx01.send(msg.encode('utf-8'))

        

def rx_mode():
    global recv_msg_queue
    while True :
#         n = sx02.scanChannel()
#         print(n)
        recv, err = sx02.recv()
        recv_msg_queue.append(recv)
        msg_recv = recv_msg_queue.pop(0)#.decode('utf-8').strip()
        print(msg_recv)
        

_thread.start_new_thread(rx_mode, ())

tx_mode()
