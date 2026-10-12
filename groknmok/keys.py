"""Public keys built into GroknMok (minisign, Ed25519).

DATASET_KEY signs the dataset manifests (an unattended key on the
publishing machine, used for nothing else). RELEASE_KEY signs program
releases (groknmok.pyz); its secret key has a password and is used by
hand. A test build may replace this file (scripts/build_pyz.py
--test-keys); a release build never does."""

DATASET_KEY = "RWTf+mhXZJNVQJmM0ZMOr/F758jF9h0cIZKMd/Mzb5bcKuBW/JbnhXtR"  # key id 405593645768FADF
RELEASE_KEY = "RWT1F5LqVaBmswTuy5Ja33wp9+Em8yaWmW/GVrsmqRzwSaWOPnIIfN9z"  # key id B366A055EA9217F5
