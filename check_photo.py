"""Try the AI photo check on your own images from the command line.

    python check_photo.py my_car_rear.jpg rear_photo
    python check_photo.py some_photo.jpg left_photo
    python check_photo.py vin.jpg chassis_photo

Slots: rear_photo, back_photo, left_photo, right_photo, chassis_photo
"""
import json
import sys

import config  # noqa: F401  (loads .env)
from services.vehicle_image_validator import SLOT_EXPECTED_VIEW, validate_slot, validation_status

if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[2] not in SLOT_EXPECTED_VIEW:
        print(__doc__)
        sys.exit(2)
    print("AI photo check:", validation_status()["summary"])
    with open(sys.argv[1], "rb") as fh:
        result = validate_slot(sys.argv[2], fh.read())
    print(json.dumps(result, indent=2, ensure_ascii=False))
    sys.exit(0 if result["valid"] else 1)
