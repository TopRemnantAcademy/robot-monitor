import json, urllib.request, urllib.parse, sys

BASE = "http://localhost:8000"
EMAIL = "spike2_verify_q9m4@robomon.dev"
PW = "SpikeVerify!2026"

def post_json(path, data):
    req = urllib.request.Request(BASE + path, data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

st, body = post_json("/auth/register", {"email": EMAIL, "password": PW})
print("register:", st, (body if st >= 400 else "ok"))

login_data = urllib.parse.urlencode({"username": EMAIL, "password": PW}).encode()
req = urllib.request.Request(BASE + "/auth/jwt/login", data=login_data,
                             headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
try:
    with urllib.request.urlopen(req) as r:
        tok = json.loads(r.read())["access_token"]
except urllib.error.HTTPError as e:
    print("login failed:", e.code, e.read().decode()); sys.exit(1)

# python
req = urllib.request.Request(BASE + "/supported-robots", headers={"Authorization": f"Bearer {tok}"})
with urllib.request.urlopen(req) as r:
    py = json.loads(r.read())
print("PY total_robots:", py["total_robots"], "models:", len(py["models"]))

# rust (unauthenticated spike)
with urllib.request.urlopen("http://localhost:8001/supported-robots") as r:
    rs = json.loads(r.read())
print("RS total_robots:", rs["total_robots"], "models:", len(rs["models"]))

py_map = {m["model_type"]: m for m in py["models"]}
rs_map = {m["model_type"]: m for m in rs["models"]}

print("total_robots equal:", py["total_robots"] == rs["total_robots"])
print("model_type sets equal:", set(py_map) == set(rs_map))
print("  only in py:", sorted(set(py_map) - set(rs_map)))
print("  only in rs:", sorted(set(rs_map) - set(py_map)))

# field-by-field per model
fields = ["display_name", "form_factor", "dof", "vendor", "description", "generic",
          "total", "online", "stations"]
mism = {f: 0 for f in fields}
fw_mism = 0
for mt in set(py_map) & set(rs_map):
    p, s = py_map[mt], rs_map[mt]
    for f in fields:
        if p[f] != s[f]:
            mism[f] += 1
    if sorted(p["firmware_versions"]) != sorted(s["firmware_versions"]):
        fw_mism += 1
print("field mismatches:", mism)
print("firmware_versions mismatches (by set):", fw_mism)

# also compare ordered firmware list (DB returns sorted via array_agg DISTINCT)
fw_order_mism = 0
for mt in set(py_map) & set(rs_map):
    p, s = py_map[mt], rs_map[mt]
    if p["firmware_versions"] != s["firmware_versions"]:
        fw_order_mism += 1
print("firmware_versions mismatches (by order):", fw_order_mism)

print("sample models:")
for mt in sorted(py_map):
    m = py_map[mt]
    print("  ", mt, "generic=", m["generic"], "total=", m["total"], "online=", m["online"],
          "fw=", m["firmware_versions"], "stations=", m["stations"])
print("DONE")
