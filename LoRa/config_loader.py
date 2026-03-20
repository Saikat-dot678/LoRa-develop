import json
import os

DEFAULT_IDENTITY = {"my_addr": 0x0B, "my_name": "Unknown"}
DEFAULT_NEIGHBORS = {"nodes": []}

def load_identity():
    try:
        with open('/identity.json', 'r') as f:
            return json.load(f)
    except:
        print("[Config] Identity file missing! Using default.")
        return DEFAULT_IDENTITY

def load_neighbors():
    try:
        with open('/neighbors.json', 'r') as f:
            return json.load(f)
    except:
        return DEFAULT_NEIGHBORS

def save_neighbor(addr, name="New Node"):
    """Updates or Adds a neighbor to the history"""
    data = load_neighbors()
    
    found = False
    for node in data['nodes']:
        if node['addr'] == addr:
            node['last_seen_epoch'] = 0 # Update time here if available
            found = True
            break
    
    if not found:
        data['nodes'].append({
            "addr": addr,
            "name": name,
            "last_seen_epoch": 0,
            "status": "new"
        })
    
    try:
        with open('/neighbors.json', 'w') as f:
            json.dump(data, f)
    except Exception as e:
        print(f"[Config] Save Error: {e}")
