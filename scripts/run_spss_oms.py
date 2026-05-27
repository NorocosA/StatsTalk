"""Execute SPSS syntax via built-in Python 3.4 interpreter."""

import spss, spssaux, os, sys

data_path = r"D:/Projects/SPSS Natural Language Assistant(SNLA)/data/fixtures/test_data.sav"
outdir = r"D:/Projects/SPSS Natural Language Assistant(SNLA)/p0_output"
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
