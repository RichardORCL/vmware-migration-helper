import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Never talk to the real OCI metadata service / instance principals in tests.
os.environ.setdefault("HELPER_INSTANCE_ID", "ocid1.instance.oc1..helper")
os.environ.setdefault("HELPER_COMPARTMENT_ID", "ocid1.compartment.oc1..helper")
os.environ.setdefault("HELPER_AVAILABILITY_DOMAIN", "Uocm:EU-FRANKFURT-1-AD-1")
os.environ.setdefault("HELPER_REGION", "eu-frankfurt-1")
os.environ.setdefault("HELPER_TENANCY_ID", "ocid1.tenancy.oc1..test")
os.environ.setdefault("HELPER_SEED_BUCKET", "vc-oci-seed")
os.environ.setdefault("HELPER_VCENTER_HOST", "vc.test")
