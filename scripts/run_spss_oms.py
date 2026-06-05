"""Execute SPSS syntax via built-in Python 3.4 interpreter."""

import spss, spssaux, os, sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
data_path = str(PROJECT_ROOT / "data" / "fixtures" / "test_data.sav")
outdir = str(PROJECT_ROOT / "p0_output")
os.makedirs(outdir, exist_ok=True)

xml_path = os.path.join(outdir, "frequencies_test.xml").replace("\\", "/")

oms_syntax = "OMS /SELECT TABLES /DESTINATION FORMAT=OXML OUTFILE='{}'.\n".format(xml_path)
oms_syntax += "GET FILE='{}'.\n".format(data_path.replace("\\", "/"))
oms_syntax += "FREQUENCIES VARIABLES=gender.\n"
oms_syntax += "OMSEND."

print("Submitting syntax...")
spss.Submit(oms_syntax)
print("Done.")

if os.path.exists(xml_path):
    size = os.path.getsize(xml_path)
    print("XML output: {} ({} bytes)".format(xml_path, size))
    with open(xml_path, "r") as f:
        print(f.read()[:500])
else:
    print("XML NOT created")
