import os
import time
from chemdraw_com import connect_chemdraw


def automate_chemdraw_conversion_to_cdxml(input_cdx_path, output_cdxml_path):
    print(f"Opening CDX file: {input_cdx_path}")
    if not os.path.exists(input_cdx_path):
        raise FileNotFoundError(f"File not found at '{input_cdx_path}'")

    chemdraw_app, _ = connect_chemdraw()
    chemdraw_app.Visible = True
    time.sleep(2)

    doc = chemdraw_app.Documents.Open(input_cdx_path)
    doc.Activate()
    print("Document opened successfully.")

    print(f"Saving as CDXML: {output_cdxml_path}")
    doc.SaveAs(output_cdxml_path)
    print("File saved successfully.")
    doc.Close()


def batch_convert_cdx_to_cdxml(input_dir, output_dir):
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    cdx_files = [f for f in os.listdir(input_dir) if f.lower().endswith('.cdx')]
    if not cdx_files:
        print("No .cdx files found in the directory.")
        return

    for cdx_file in cdx_files:
        input_cdx_path = os.path.join(input_dir, cdx_file)
        output_cdxml_path = os.path.join(output_dir, os.path.splitext(cdx_file)[0] + '.cdxml')
        automate_chemdraw_conversion_to_cdxml(input_cdx_path, output_cdxml_path)
