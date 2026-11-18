"""Replace identifying attribute values in captured OTLP JSON with stable fake values."""
import hashlib, json, sys
from pathlib import Path

PII_KEYS = {"user.email", "user.account_uuid", "user.account_id", "organization.id", "user.id", "host.name"}

def fake(key: str, value: str) -> str:
    h = hashlib.sha256(f"{key}:{value}".encode()).hexdigest()[:12]
    return f"user-{h}@example.com" if key == "user.email" else f"fake-{key.split('.')[-1]}-{h}"

def scrub(obj):
    if isinstance(obj, dict):
        if obj.get("key") in PII_KEYS and isinstance(obj.get("value"), dict):
            v = obj["value"]
            for t in v:
                v[t] = fake(obj["key"], str(v[t]))
            return obj
        return {k: scrub(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(x) for x in obj]
    return obj

for src in map(Path, sys.argv[1:]):
    dst = src.parent.parent / src.name
    with src.open() as f, dst.open("w") as out:
        for line in f:
            out.write(json.dumps(scrub(json.loads(line))) + "\n")
    print(f"{src} -> {dst}")
