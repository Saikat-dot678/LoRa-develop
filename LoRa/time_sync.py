import time

class TimeManager:
    def __init__(self):
        self.offset = 0
        self.is_synced = False
        self.last_sync_local = 0

    def sync(self, network_time):
        """
        Called when a Beacon is received.
        Net_Time = Local_Time + Offset
        """
        local_now = time.ticks_ms()
        new_offset = network_time - local_now
        self.offset = new_offset
        self.last_sync_local = local_now
        self.is_synced = True

    def get_net_time(self):
        if not self.is_synced: return 0
        return time.ticks_ms() + self.offset

    def get_time_since_sync(self):
        return time.ticks_diff(time.ticks_ms(), self.last_sync_local)
