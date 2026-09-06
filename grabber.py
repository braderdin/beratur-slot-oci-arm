#!/usr/bin/env python3
"""
Loop-attempts to launch an Always-Free Ampere A1 (ARM) instance on OCI until
capacity is available. Auto-provisions a VCN/subnet if none exists.

Usage:
    .venv/bin/python3 grabber.py                 # run forever
    .venv/bin/python3 grabber.py --once           # single attempt (test config)
    .venv/bin/python3 grabber.py --dry-run         # resolve resources, no LaunchInstance call
"""
import argparse
import json
import logging
import random
import subprocess
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import oci

BASE_DIR = Path(__file__).resolve().parent
STATUS_FILE = BASE_DIR / "status.json"
LOG_FILE = BASE_DIR / "grabber.log"
SSH_PUB_KEY_FILE = Path.home() / ".ssh" / "oci_arm_grabber.pub"

SHAPE = "VM.Standard.A1.Flex"
OCPUS = 2
MEMORY_IN_GBS = 12
BOOT_VOLUME_GBS = 50
DISPLAY_NAME = "oci-arm-free"
VCN_CIDR = "10.0.0.0/16"
SUBNET_CIDR = "10.0.1.0/24"
MIN_INTERVAL = 45
MAX_INTERVAL = 90

logger = logging.getLogger("grabber")
logger.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
_file_handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3)
_file_handler.setFormatter(_fmt)
logger.addHandler(_file_handler)


def write_status(**kwargs):
    data = {}
    if STATUS_FILE.exists():
        try:
            data = json.loads(STATUS_FILE.read_text())
        except json.JSONDecodeError:
            data = {}
    data.update(kwargs)
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = STATUS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(STATUS_FILE)


def print_status_line(text: str):
    sys.stdout.write("\r\x1b[K" + text)
    sys.stdout.flush()


def notify_windows(title: str, message: str):
    """Best-effort Windows toast/balloon via powershell.exe (WSL interop). Never fatal."""
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$n = New-Object System.Windows.Forms.NotifyIcon; "
        "$n.Icon = [System.Drawing.SystemIcons]::Information; "
        "$n.Visible = $true; "
        f"$n.ShowBalloonTip(15000,'{title}','{message}')"
    )
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps],
            timeout=10, capture_output=True,
        )
    except Exception:
        pass


def ensure_network(vnet: oci.core.VirtualNetworkClient, compartment_id: str):
    vcns = vnet.list_vcns(compartment_id=compartment_id, lifecycle_state="AVAILABLE").data
    if vcns:
        vcn = vcns[0]
        subnets = vnet.list_subnets(compartment_id=compartment_id, vcn_id=vcn.id).data
        public_subnets = [s for s in subnets if not s.prohibit_public_ip_on_vnic and s.lifecycle_state == "AVAILABLE"]
        if public_subnets:
            logger.info(f"Reusing existing VCN {vcn.display_name} / subnet {public_subnets[0].display_name}")
            return public_subnets[0].id
        subnet = public_subnets[0] if public_subnets else None
    else:
        vcn = None
        subnet = None

    composite = oci.core.VirtualNetworkClientCompositeOperations(vnet)

    if vcn is None:
        logger.info("No VCN found, creating one...")
        vcn = composite.create_vcn_and_wait_for_state(
            oci.core.models.CreateVcnDetails(
                cidr_block=VCN_CIDR,
                compartment_id=compartment_id,
                display_name="oci-arm-grabber-vcn",
            ),
            wait_for_states=["AVAILABLE"],
        ).data

    igws = vnet.list_internet_gateways(compartment_id=compartment_id, vcn_id=vcn.id).data
    if igws:
        igw = igws[0]
    else:
        logger.info("Creating Internet Gateway...")
        igw = composite.create_internet_gateway_and_wait_for_state(
            oci.core.models.CreateInternetGatewayDetails(
                compartment_id=compartment_id,
                is_enabled=True,
                vcn_id=vcn.id,
                display_name="oci-arm-grabber-igw",
            ),
            wait_for_states=["AVAILABLE"],
        ).data

    rt = vnet.get_route_table(vcn.default_route_table_id).data
    if not any(r.destination == "0.0.0.0/0" for r in rt.route_rules):
        logger.info("Adding default route to Internet Gateway...")
        vnet.update_route_table(
            rt.id,
            oci.core.models.UpdateRouteTableDetails(
                route_rules=rt.route_rules + [
                    oci.core.models.RouteRule(
                        destination="0.0.0.0/0",
                        destination_type="CIDR_BLOCK",
                        network_entity_id=igw.id,
                    )
                ]
            ),
        )

    sl = vnet.get_security_list(vcn.default_security_list_id).data
    has_ssh = any(
        r.protocol == "6" and r.tcp_options and r.tcp_options.destination_port_range
        and r.tcp_options.destination_port_range.min == 22
        for r in sl.ingress_security_rules
    )
    if not has_ssh:
        logger.info("Adding SSH ingress rule to default security list...")
        vnet.update_security_list(
            sl.id,
            oci.core.models.UpdateSecurityListDetails(
                ingress_security_rules=sl.ingress_security_rules + [
                    oci.core.models.IngressSecurityRule(
                        protocol="6",
                        source="0.0.0.0/0",
                        source_type="CIDR_BLOCK",
                        tcp_options=oci.core.models.TcpOptions(
                            destination_port_range=oci.core.models.PortRange(min=22, max=22)
                        ),
                    )
                ]
            ),
        )

    logger.info("Creating public subnet...")
    subnet = composite.create_subnet_and_wait_for_state(
        oci.core.models.CreateSubnetDetails(
            cidr_block=SUBNET_CIDR,
            compartment_id=compartment_id,
            vcn_id=vcn.id,
            display_name="oci-arm-grabber-subnet",
            prohibit_public_ip_on_vnic=False,
            route_table_id=rt.id,
            security_list_ids=[sl.id],
        ),
        wait_for_states=["AVAILABLE"],
    ).data
    return subnet.id


def latest_ubuntu_image(compute: oci.core.ComputeClient, compartment_id: str) -> str:
    images = compute.list_images(
        compartment_id=compartment_id,
        operating_system="Canonical Ubuntu",
        operating_system_version="24.04",
        shape=SHAPE,
        sort_by="TIMECREATED",
        sort_order="DESC",
    ).data
    images = [i for i in images if "Minimal" not in i.display_name]
    if not images:
        raise RuntimeError("No matching Ubuntu 24.04 aarch64 image found")
    logger.info(f"Using image {images[0].display_name}")
    return images[0].id


def is_capacity_error(e: oci.exceptions.ServiceError) -> bool:
    return e.status == 500 and "Out of host capacity" in (e.message or "")


def is_rate_limited(e: oci.exceptions.ServiceError) -> bool:
    return e.status == 429


ACTIVE_STATES = ("PROVISIONING", "STARTING", "RUNNING")


def find_existing_instance(compute: oci.core.ComputeClient, compartment_id: str):
    for i in compute.list_instances(compartment_id=compartment_id, display_name=DISPLAY_NAME).data:
        if i.lifecycle_state in ACTIVE_STATES:
            return i
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Try exactly one launch attempt then exit")
    parser.add_argument("--dry-run", action="store_true", help="Resolve resources but don't call LaunchInstance")
    parser.add_argument("--compartment-id", default=None, help="Defaults to the tenancy root compartment")
    args = parser.parse_args()

    if not SSH_PUB_KEY_FILE.exists():
        print(f"SSH public key not found at {SSH_PUB_KEY_FILE}", file=sys.stderr)
        sys.exit(1)
    ssh_pub_key = SSH_PUB_KEY_FILE.read_text().strip()

    config = oci.config.from_file()
    oci.config.validate_config(config)
    compartment_id = args.compartment_id or config["tenancy"]

    identity = oci.identity.IdentityClient(config)
    compute = oci.core.ComputeClient(config)
    vnet = oci.core.VirtualNetworkClient(config)

    existing = find_existing_instance(compute, compartment_id)
    if existing:
        msg = f"Already have an instance ({existing.id}, {existing.lifecycle_state}). Nothing to do."
        logger.info(msg)
        print(msg)
        write_status(success=True, instance_id=existing.id, last_result="ALREADY_SECURED")
        return 3

    ads = [ad.name for ad in identity.list_availability_domains(compartment_id=compartment_id).data]
    logger.info(f"Availability domains: {ads}")

    subnet_id = ensure_network(vnet, compartment_id)
    image_id = latest_ubuntu_image(compute, compartment_id)

    logger.info(
        f"Config resolved: compartment={compartment_id} subnet={subnet_id} "
        f"image={image_id} shape={SHAPE} ocpus={OCPUS} mem={MEMORY_IN_GBS}GB ads={ads}"
    )

    if args.dry_run:
        print("Dry run OK, resources resolved. Not launching.")
        return 0

    launch_details_base = dict(
        compartment_id=compartment_id,
        shape=SHAPE,
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=OCPUS, memory_in_gbs=MEMORY_IN_GBS
        ),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=image_id, boot_volume_size_in_gbs=BOOT_VOLUME_GBS
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=subnet_id, assign_public_ip=True
        ),
        metadata={"ssh_authorized_keys": ssh_pub_key},
        display_name=DISPLAY_NAME,
    )

    start_time = time.time()
    attempt = 0
    write_status(started_at=datetime.now(timezone.utc).isoformat(), attempts=0, success=False)

    while True:
        for ad in ads:
            attempt += 1
            elapsed = int(time.time() - start_time)
            print_status_line(
                f"[{datetime.now().strftime('%H:%M:%S')}] attempt #{attempt} | AD={ad} | "
                f"elapsed={elapsed}s | trying LaunchInstance..."
            )
            try:
                resp = compute.launch_instance(
                    oci.core.models.LaunchInstanceDetails(
                        availability_domain=ad, **launch_details_base
                    )
                )
                instance = resp.data
                print()
                logger.info(f"SUCCESS: instance {instance.id} launching in {ad}")
                write_status(attempts=attempt, success=True, instance_id=instance.id,
                             availability_domain=ad, last_result="LAUNCHED")

                for _ in range(120):
                    instance = compute.get_instance(instance.id).data
                    if instance.lifecycle_state == "RUNNING":
                        break
                    time.sleep(5)

                vnics = compute.list_vnic_attachments(
                    compartment_id=compartment_id, instance_id=instance.id
                ).data
                public_ip = None
                if vnics:
                    vnic = vnet.get_vnic(vnics[0].vnic_id).data
                    public_ip = vnic.public_ip

                msg = f"Instance RUNNING. id={instance.id} public_ip={public_ip}"
                logger.info(msg)
                write_status(attempts=attempt, success=True, instance_id=instance.id,
                             public_ip=public_ip, last_result="RUNNING")
                print(msg)
                print(f"SSH: ssh -i ~/.ssh/oci_arm_grabber ubuntu@{public_ip}")
                notify_windows("OCI ARM instance secured!", f"public_ip={public_ip}")
                return 0

            except oci.exceptions.ServiceError as e:
                if is_capacity_error(e):
                    write_status(attempts=attempt, last_result="OUT_OF_CAPACITY", last_ad=ad)
                    logger.info(f"attempt {attempt} AD={ad}: out of host capacity")
                    continue
                if is_rate_limited(e):
                    write_status(attempts=attempt, last_result="RATE_LIMITED")
                    if args.once:
                        logger.warning(f"attempt {attempt}: rate limited (429), exiting (--once)")
                        print()
                        return 2
                    logger.warning(f"attempt {attempt}: rate limited (429), backing off 120s")
                    print()
                    time.sleep(120)
                    continue
                print()
                logger.error(f"attempt {attempt} AD={ad}: unexpected ServiceError "
                             f"status={e.status} code={e.code} message={e.message}")
                write_status(attempts=attempt, last_result=f"ERROR:{e.code}", error=e.message)
                print(f"Unexpected error ({e.code}): {e.message}", file=sys.stderr)
                if e.status in (400, 401, 404):
                    print("This looks like a config/permission/quota problem, not a capacity "
                          "problem. Stopping instead of looping forever.", file=sys.stderr)
                    return 1

        if args.once:
            print()
            print("No capacity on this attempt (--once given, exiting).")
            return 2

        interval = random.uniform(MIN_INTERVAL, MAX_INTERVAL)
        for remaining in range(int(interval), 0, -1):
            print_status_line(
                f"[{datetime.now().strftime('%H:%M:%S')}] attempt #{attempt} done | "
                f"no capacity yet | next try in {remaining:>3}s | log: {LOG_FILE}"
            )
            time.sleep(1)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        sys.exit(130)
