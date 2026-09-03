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
parser.add_argument("--shape", choices=["AMD", "ARM"], default="ARM", help="Jenis shape VM")
args = parser.parse_args()

def check_vm():
    config_path = os.path.expanduser("~/.oci/config")
    config = None

    # UTAMA: BACA DARI FAIL ~/.oci/config JIKA WUJUD
    if os.path.exists(config_path):
        try:
            config = oci.config.from_file(config_path, "DEFAULT")
        except Exception as e:
            print(f"⚠️ Fail ~/.oci/config wujud tetapi gagal dibaca: {e}")

    # ALTERNATIF: BACA DARI ENVIRONMENT VARIABLES
    if not config:
        tenancy = os.getenv("OCI_TENANCY_OCID") or os.getenv("OCI_TENANCY") or os.getenv("TENANCY")
        user = os.getenv("OCI_USER_OCID") or os.getenv("OCI_USER") or os.getenv("USER")
        fingerprint = os.getenv("OCI_FINGERPRINT") or os.getenv("FINGERPRINT")
        region = os.getenv("OCI_REGION", "ap-singapore-1")
        private_key = os.getenv("OCI_PRIVATE_KEY")
        key_file = os.getenv("OCI_KEY_FILE")

        config = {
            "user": user,
            "fingerprint": fingerprint,
            "tenancy": tenancy,
            "region": region,
        }

        if private_key and private_key.strip():
            config["key_content"] = private_key
        elif key_file and os.path.exists(key_file):
            config["key_file"] = key_file

    try:
        oci.config.validate_config(config)
        compartment_id = config.get("tenancy")
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
            # Matikan workflow dengan exit 1 supaya step tembakan tidak dijalankan
            sys.exit(1)
        else:
            print(f"✅ Tiada VM {args.shape} yang aktif dijumpai. Memulakan tembakan...\n")
            sys.exit(0)

    except Exception as e:
        print(f"⚠️ [AMARAN SEMAKAN VM]: Gagal menyemak status VM ({e}). Meneruskan tembakan...")
        sys.exit(0)

if __name__ == "__main__":
    check_vm()