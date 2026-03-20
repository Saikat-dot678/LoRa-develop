from utils2 import get_network_time

class SlotManager:
    def __init__(self, my_addr):
        self.my_addr = my_addr
        self.slot_idx = 0     
        self.time_in_slot = 0
        
        # --- V2.0 STABLE PHASE BOUNDARIES ---
        self.PHASE_BEACON_END  = 8000   # 0-8s: Sync
        self.PHASE_CONTROL_END = 25000  # 8-25s: Join/Health
        self.PHASE_DATAREQ_END = 35000  # 25-35s: Traffic Req
        self.PHASE_SCHED_END   = 40000  # 35-40s: Hub Sched
        # 40-60s: DUPLEX DATA TRANSFER

        self.assigned_lane = 0

    def update(self):
        """Called every loop to calculate current phase based on synced time"""
        net_time = get_network_time()
        self.slot_idx = (net_time // 60000) % 4
        self.time_in_slot = net_time % 60000
        
        if self.time_in_slot < 1000:
            self.assigned_lane = 0

    def get_current_phase(self):
        tis = self.time_in_slot
        if tis < self.PHASE_BEACON_END:  return "1. BEACON (SYNC)"
        if tis < self.PHASE_CONTROL_END: return "2. CONTROL/JOIN"
        if tis < self.PHASE_DATAREQ_END: return "3. DATA REQUEST"
        if tis < self.PHASE_SCHED_END:   return "3.5 SCHEDULING"
        if self.assigned_lane > 0:       return f"4. DATA (LANE {self.assigned_lane})"
        return "4. DATA (IDLE)"
