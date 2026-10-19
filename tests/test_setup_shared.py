"""Exercise the real VM-key shell section without SSH or host mutations."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'deploy/gamma2/host/setup-shared.sh'


class SharedSetupSshTests(unittest.TestCase):
    def run_key_setup(self, key, ssh_status=0):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.ssh').mkdir()
            (root / '.ssh/id_ed25519.pub').write_text(key + '\n')
            (root / '.env').write_text('PAI_VM_USER=daqvnc1\n')
            fake_bin = root / 'bin'
            fake_bin.mkdir()
            mocks = {
                'virsh': '#!/bin/sh\nprintf "vnet3 52:54:00:20:6f:12 ipv4 192.168.122.51/24\\n"\n',
                'getent': '#!/bin/sh\nprintf "ian:x:1000:1000::%s:/bin/bash\\n" "$TEST_HOME"\n',
                'sudo': f'#!{sys.executable}\nimport json, os, pathlib, sys\npathlib.Path(os.environ["SSH_CAPTURE"]).write_text(json.dumps(sys.argv[1:]))\nsys.exit(int(os.environ["SSH_STATUS"]))\n',
            }
            for name, content in mocks.items():
                p = fake_bin / name
                p.write_text(content)
                p.chmod(0o755)
            source = SCRIPT.read_text()
            section = source.split('# ---------------------------------------------------------------- VM ssh keys\n', 1)[1]
            section = section.split('# ---------------------------------------------------------------- workloads', 1)[0]
            env = dict(os.environ, PATH=str(fake_bin) + os.pathsep + os.environ['PATH'],
                       USERS='ian', SUDO_USER='ian', PIPELINE=str(root),
                       TEST_HOME=str(root), SSH_CAPTURE=str(root / 'ssh.json'),
                       SSH_STATUS=str(ssh_status))
            proc = subprocess.run(['bash', '-c', 'set -euo pipefail\nlog() { echo "$*"; }\n' + section + '\necho REACHED_WORKLOADS'],
                                  env=env, text=True, capture_output=True, timeout=10)
            captured = json.loads((root / 'ssh.json').read_text())
            return proc, captured

    def test_powershell_variables_survive_remote_shells(self):
        key = "ssh-ed25519 AAAAtest ian's workstation $(not-a-command) `literal`"
        proc, args = self.run_key_setup(key)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('REACHED_WORKLOADS', proc.stdout)
        self.assertEqual(args[:4], ['-u', 'ian', 'ssh', '-n'])
        self.assertIn('BatchMode=yes', args)
        self.assertEqual(args[-2], 'daqvnc1@192.168.122.51')
        prefix = 'powershell.exe -NoProfile -NonInteractive -EncodedCommand '
        self.assertTrue(args[-1].startswith(prefix))
        payload = args[-1][len(prefix):]
        self.assertNotIn('$', payload)
        self.assertNotIn('`', payload)
        script = base64.b64decode(payload, validate=True).decode('utf-16le')
        self.assertIn("$f = 'C:\\ProgramData\\ssh\\administrators_authorized_keys'", script)
        self.assertIn("$key = '" + key.replace("'", "''") + "'", script)
        self.assertIn("$ErrorActionPreference = 'Stop'", script)
        self.assertIn('Test-Path -LiteralPath $f', script)
        self.assertIn('Get-Content -LiteralPath $f) -contains $key', script)
        self.assertIn('Add-Content -LiteralPath $f -Value $key', script)

    def test_failed_ssh_stops_before_installing_workloads(self):
        proc, _ = self.run_key_setup('ssh-ed25519 AAAAtest ian', ssh_status=255)
        self.assertEqual(proc.returncode, 1)
        self.assertIn('stopping setup', proc.stdout)
        self.assertNotIn('REACHED_WORKLOADS', proc.stdout)


if __name__ == '__main__':
    unittest.main()
