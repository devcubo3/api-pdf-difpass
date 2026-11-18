"""Write the sandbox privilege configuration for an EXISTING Swarm service.

This only writes a private configuration file; it never contacts Docker or deploys.
The hosting operator must check that their Engine/Dokploy supports these fields.
"""
import argparse
import base64
import json
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
profile = Path(__file__).resolve().parents[1] / 'seccomp-chromium.json'
payload = {'Seccomp': {'Mode': 'custom', 'Profile': base64.b64encode(profile.read_bytes()).decode()},
           'NoNewPrivileges': True}
with args.output.open('x', encoding='utf-8') as stream:
    json.dump(payload, stream, indent=2)
args.output.chmod(0o600)
print('Wrote ContainerSpec.Privileges configuration. No Docker service was changed.')
