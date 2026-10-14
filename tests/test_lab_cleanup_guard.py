"""Run the cleanup preflight against fake native Azure CLI commands."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PWSH = os.environ.get("PWSH_BIN") or shutil.which("pwsh")
pytestmark = pytest.mark.skipif(not PWSH, reason="PowerShell 7 required")


def test_cleanup_refuses_selected_subscription_mismatch_before_cloud_mutation(tmp_path):
    root = Path(__file__).resolve().parents[1]
    relative = "scripts/delete-environment-resource-group.ps1"
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    shutil.copyfile(root / relative, target)
    local = tmp_path / ".local"
    local.mkdir()
    (local / "rg-demo.json").write_text(json.dumps({"subscriptionId":"recorded-sub", "resourceGroup":"rg-demo"}))
    binary = tmp_path / "bin"
    binary.mkdir()
    az = binary / "az"
    az.write_text("#!" + sys.executable + "\nimport json,sys\nprint(json.dumps({'id':'wrong-sub','name':'Wrong'}))\n")
    az.chmod(0o755)
    env = {**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"]}
    result = subprocess.run([PWSH, "-NoProfile", "-File", str(target), "-ResourceGroup", "rg-demo"],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "differs from recorded lab" in result.stderr


@pytest.mark.parametrize("exists", [False, True])
def test_setup_retains_live_state_but_retires_deleted_lab_names(tmp_path, exists):
    root = Path(__file__).resolve().parents[1]
    source = (root / "scripts/setup-environment.ps1").read_text()
    begin = source.index("$local =")
    end = source.index("$work =", begin)
    local = tmp_path / ".local"
    local.mkdir()
    previous = dict(subscriptionId="sub", resourceGroup="rg-demo", location="polandcentral",
                    aiLocation="swedencentral", nodeVmSize="Standard_D2as_v4",
                    modelName="gpt-4.1-nano", modelVersion="2025-04-14",
                    modelDeployment="diagnostic-gpt41nano", modelCapacity=10,
                    acrName="prior-registry", aiName="prior-account")
    (local / "rg-demo.json").write_text(json.dumps(previous))
    header = "$root = '" + str(tmp_path).replace("'", "''") + "'\n"
    for name in ["SubscriptionId", "ResourceGroup", "Location", "AiLocation", "NodeVmSize", "ModelName", "ModelVersion", "ModelDeployment"]:
        key = name[0].lower() + name[1:]
        header += "$" + name + " = '" + previous[key] + "'\n"
    header += "$ModelCapacity = 10\n"
    header += "function Invoke-Az { param($Arguments) '" + str(exists).lower() + "' }\n"
    fixture = tmp_path / "state-check.ps1"
    fixture.write_text(header + source[begin:end] + "\n$state | ConvertTo-Json -Compress\n")
    result = subprocess.run([PWSH, "-NoProfile", "-File", str(fixture)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)
    if exists:
        assert state["aiName"] == "prior-account"
        assert not (local / "retired").exists()
    else:
        assert state["aiName"] != "prior-account"
        assert len(list((local / "retired").glob("*.json"))) == 1
