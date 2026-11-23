import re
from pathlib import Path

import yaml

INFRA = Path(__file__).parent.parent / "infra"
EDGE = INFRA / "edge"


def _cloud_init() -> tuple[str, dict]:
    text = (EDGE / "cloud-init.yaml").read_text(encoding="utf-8")
    return text, yaml.safe_load(text)


def test_cloud_init_is_valid_and_installs_iot_edge_1_6_on_ubuntu_24_04():
    text, config = _cloud_init()
    assert text.startswith("#cloud-config\n")
    install = config["runcmd"][0][2]
    assert "packages.microsoft.com/config/ubuntu/24.04/packages-microsoft-prod.deb" in install
    lines = [line.strip() for line in install.splitlines()]
    moby = next(i for i, line in enumerate(lines) if "apt-get install" in line and "moby-engine" in line)
    edge = next(i for i, line in enumerate(lines) if "apt-get install" in line and "aziot-edge" in line)
    # aziot-edge's pre-install script fails unless a container runtime is already installed.
    assert moby < edge and "aziot-edge" not in lines[moby]
    daemon = next(f for f in config["write_files"] if f["path"] == "/etc/docker/daemon.json")
    assert '"log-driver": "local"' in daemon["content"]


def test_device_key_is_generated_on_the_device_and_kept_private():
    _, config = _cloud_init()
    script = config["runcmd"][1][2]
    assert "openssl req -x509 -newkey ec" in script
    assert "chmod 0600 /var/aziot/secrets/device.key.pem" in script
    assert "chown aziotks:aziotks /var/aziot/secrets/device.key.pem" in script
    assert "basicConstraints=critical,CA:FALSE" in script


def test_provisioning_config_uses_manual_x509_with_placeholders_bicep_fills():
    text, config = _cloud_init()
    pending = next(f for f in config["write_files"] if f["path"] == "/etc/aziot/config.toml.pending")
    assert pending["permissions"] == "0600"
    content = pending["content"]
    assert 'source = "manual"' in content and 'method = "x509"' in content
    assert 'identity_pk = "file:///var/aziot/secrets/device.key.pem"' in content
    placeholders = set(re.findall(r"__[A-Z_]+__", text))
    bicep = (INFRA / "main.bicep").read_text(encoding="utf-8")
    assert placeholders == {"__HUB_HOSTNAME__", "__DEVICE_ID__"}
    assert all(f"'{p}'" in bicep for p in placeholders)


def test_vm_scripts_are_strict_bash_and_print_their_markers():
    markers = {
        "device-thumbprint.sh": "THUMBPRINT",
        "apply-config.sh": "CONFIGURED",
        "build-images.sh": "IMAGES",
        "network.sh": "NETWORK",
    }
    common = (INFRA / "common.ps1").read_text(encoding="utf-8") + (INFRA / "deploy.ps1").read_text(encoding="utf-8")
    common += (INFRA / "network.ps1").read_text(encoding="utf-8")
    for script, marker in markers.items():
        text = (EDGE / script).read_text(encoding="utf-8")
        assert text.startswith("#!/usr/bin/env bash\n") and "set -euo pipefail" in text
        assert "\r" not in text
        assert f"{marker} " in text or f'"{marker} ' in text or f"'{marker} " in text
        assert f"'{marker}'" in common, f"no PowerShell caller waits for {marker}"


def test_build_script_logs_in_with_the_managed_identity_and_cleans_up():
    text = (EDGE / "build-images.sh").read_text(encoding="utf-8")
    assert "169.254.169.254/metadata/identity/oauth2/token" in text
    assert "--password-stdin" in text and "trap cleanup EXIT" in text
    assert "docker logout" in text
    # an unchanged module keeps its digest, so a model rollout restarts only the detector
    assert text.index("docker pull") < text.index("docker build")


def test_bicep_never_enables_registry_admin_or_shared_keys():
    bicep = (INFRA / "main.bicep").read_text(encoding="utf-8")
    assert "adminUserEnabled: false" in bicep
    assert "allowSharedKeyAccess: false" in bicep
    assert "disablePasswordAuthentication: true" in bicep
    assert "securityRules: []" in bicep  # no inbound rules
