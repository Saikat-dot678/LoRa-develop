import json
identity = {"my_addr": 0x02, "my_name": "Client_Node"}
with open('/identity.json', 'w') as f:
    json.dump(identity, f)
print("Board 2 configured as 0x02")
