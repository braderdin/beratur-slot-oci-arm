import os
import sys
import argparse
from dotenv import load_dotenv

if os.path.exists(".env.local"):
    load_dotenv(".env.local", override=True)
elif os.path.exists(".env"):
    load_dotenv(".env", override=True)

try:
    import oci
except ImportError:
    print("❌ [RALAT KRITIKAL] Pustaka 'oci' belum dipasang.")
    sys.exit(1)

parser = argparse.ArgumentParser(description="Semak status VM sedia ada di OCI")
parser.add_argument("--shape", choices=["AMD", "ARM"], default="ARM", help="Jenis shape VM yang hendak disemak")
args = parser.parse_args()

def set_github_output(key: str, value: str):
    """Hantar pembolehubah ke GitHub Actions Step Output"""
    github_output = os.getenv("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"{key}={value}\n")

def check_vm():
    # Padankan pembolehubah persekitaran mengikut GitHub Secrets
    tenancy = os.getenv("OCI_TENANCY_OCID") or os.getenv("OCI_TENANCY") or os.getenv("TENANCY")
    user = os.getenv("OCI_USER_OCID") or os.getenv("OCI_USER") or os.getenv("USER")
    fingerprint = os.getenv("OCI_FINGERPRINT") or os.getenv("FINGERPRINT")
    region = os.getenv("OCI_REGION", "ap-singapore-1")
    private_key = os.getenv("OCI_PRIVATE_KEY")
    key_file = os.getenv("OCI_KEY_FILE")
    compartment_id = os.getenv("OCI_COMPARTMENT_ID") or tenancy

    config = {
        "user": user,
        "fingerprint": fingerprint,
        "tenancy": tenancy,
        "region": region,
    }

    # Mengendalikan kunci Private Key sama ada berbentuk Teks Secret atau File Path
    if private_key and private_key.strip():
        config["key_content"] = private_key
    elif key_file and os.path.exists(key_file):
        config["key_file"] = key_file
    else:
        local_key = "kunci_oci/oci-oracle-api-key/braderdin007@gmail.com-2026-07-26T17_31_09.593Z.pem"
        if os.path.exists(local_key):
            config["key_file"] = local_key

    try:
        oci.config.validate_config(config)
        compute_client = oci.core.ComputeClient(config)
        
        print(f"🔍 [SEMAKAN PINTAR] Memeriksa kewujudan VM {args.shape} di OCI...")
        instances = compute_client.list_instances(compartment_id=compartment_id).data

        target_oci_shape = "VM.Standard.E2.1.Micro" if args.shape == "AMD" else "VM.Standard.A1.Flex"
        active_states = ["RUNNING", "PROVISIONING", "STARTING"]

        existing_vms = [
            inst for inst in instances 
            if inst.shape == target_oci_shape and inst.lifecycle_state in active_states
        ]

        if existing_vms:
            vm = existing_vms[0]
            print("\n" + "=" * 65)
            print(f"🛑 [VM SEDIA ADA DIJUMPAI!] Tembakan dibatalkan.")
            print(f"📌 Nama VM       : {vm.display_name}")
            print(f"📌 Status VM     : {vm.lifecycle_state}")
            print(f"📌 Target Shape  : {vm.shape}")
            print(f"📌 Instance ID   : {vm.id}")
            print("=" * 65 + "\n")
            
            set_github_output("vm_exists", "true")
            sys.exit(0)
        else:
            print(f"✅ Tiada VM {args.shape} yang aktif dijumpai. Memulakan proses menembak slot...\n")
            set_github_output("vm_exists", "false")
            sys.exit(0)

    except Exception as e:
        print(f"⚠️ [AMARAN SEMAKAN VM]: Gagal menyemak status VM ({e}). Meneruskan tembakan sebagai langkah berjaga-jaga...")
        set_github_output("vm_exists", "false")
        sys.exit(0)

if __name__ == "__main__":
    check_vm()