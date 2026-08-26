import os
from xml.etree import ElementTree as ET
import re
import periodictable

def get_atom_label(atom):
    # 1. Prefer explicit node label (e.g., custom labels like 'Me', 'Ph', 'Ar')
    if "NodeLabel" in atom.attrib:
        return atom.attrib["NodeLabel"].strip()

    # 2. Resolve using Element number if available
    if "Element" in atom.attrib:
        try:
            atomic_number = int(atom.attrib["Element"])
            element = periodictable.elements[atomic_number]
            return element.symbol if element else f"E{atomic_number}"
        except (ValueError, IndexError):
            return f"E{atom.attrib['Element']}"

    # 3. Fallback
    return ""

def is_noise(frag):
    atoms = frag.findall(".//n")
    labels = [get_atom_label(a).upper() for a in atoms]
    types = set(labels)
    has_marker = any(
        a.attrib.get("NodeType", "") == "FragmentID" or get_atom_label(a) == "1"
        for a in atoms
    )
    return has_marker or ("C" not in types and len(types) <= 3 and len(atoms) <= 4)

def extract_top_level_molecules_with_text(root):
    nested = set()
    for node in root.findall(".//fragment") + root.findall(".//group"):
        for inner in node.findall(".//fragment") + node.findall(".//group"):
            nested.add(inner.attrib.get("id"))

    all_text_nodes = root.findall(".//t")
    all_bond_nodes = root.findall(".//b")
    all_graphics = root.findall(".//graphic")

    molecule_blocks = []

    for node in root.findall(".//fragment") + root.findall(".//group"):
        node_id = node.attrib.get("id")
        if node_id in nested or is_noise(node):
            continue

        box = node.attrib.get("BoundingBox")
        if not box:
            continue
        l, b, r, t = map(float, box.split())
        center_x, bottom_y = (l + r) / 2, b

        # === 1. Find meaningful caption text <t> below molecule ===
        min_dist = float("inf")
        closest_text = None
        for tnode in all_text_nodes:
            if tnode in node.findall(".//t"):
                continue

            tbox = tnode.attrib.get("BoundingBox")
            if not tbox:
                continue
            tl, tb, tr, tt = map(float, tbox.split())
            tcenter_x, ttop_y = (tl + tr) / 2, tt

            # Only look for text horizontally aligned and vertically below
            if abs(center_x - tcenter_x) < 40 and ttop_y > bottom_y:
                distance = ttop_y - bottom_y
                width = tr - tl
                if width > 30 and distance < min_dist:
                    min_dist = distance
                    closest_text = tnode

        matched_texts = [closest_text] if closest_text is not None else []

        # === 2. Find extra wedge/crossing bonds ===
        crossing_bond_ids = set()
        for bond in node.findall(".//b"):
            if "CrossingBonds" in bond.attrib:
                crossing_bond_ids.update(bond.attrib["CrossingBonds"].split())

        extra_bonds = [
            b for b in all_bond_nodes
            if b.attrib.get("id") in crossing_bond_ids
        ]

        # === 3. Find associated graphics (e.g., ellipses) inside or touching bounding box ===
        margin = 5
        graphics = []
        for g in all_graphics:
            gbox = g.attrib.get("BoundingBox")
            if not gbox:
                continue
            gl, gb, gr, gt = map(float, gbox.split())
            if (l - margin < gl < r + margin or l - margin < gr < r + margin) and \
               (b - margin < gb < t + margin or b - margin < gt < t + margin):
                graphics.append(g)

        molecule_blocks.append({
            "main": node,
            "texts": matched_texts,
            "bounding_box": (l, b, r, t),
            "extra_bonds": extra_bonds,
            "graphics": graphics
        })

    return molecule_blocks

def center_elements(elements, bounding_box, target_center=(300, 400)):
    l, b, r, t = bounding_box
    current_center = ((l + r) / 2, (b + t) / 2)
    dx = target_center[0] - current_center[0]
    dy = target_center[1] - current_center[1]

    for el in elements:
        for node in el.iter():
            if "p" in node.attrib:
                try:
                    x, y = map(float, node.attrib["p"].split())
                    node.attrib["p"] = f"{x + dx:.2f} {y + dy:.2f}"
                except:
                    continue
            if "BoundingBox" in node.attrib:
                try:
                    l, b, r, t = map(float, node.attrib["BoundingBox"].split())
                    node.attrib["BoundingBox"] = f"{l + dx:.2f} {b + dy:.2f} {r + dx:.2f} {t + dy:.2f}"
                except:
                    continue

def split_cdxml(cdxml_path, output_dir="split_output"):
    os.makedirs(output_dir, exist_ok=True)
    root = ET.parse(cdxml_path).getroot()
    molecules = extract_top_level_molecules_with_text(root)

    paths = []

    def sanitize_filename(text):
        # Remove illegal filename characters, collapse whitespace
        text = re.sub(r'[\\/*?:"<>|]', "", text)
        text = re.sub(r'\s+', "_", text.strip())
        return text[:50] or "untitled"

    for i, mol in enumerate(molecules, 1):
        doc = ET.Element("CDXML", {"CreationProgram": "ChemDraw", "Name": f"group_{i}"})
        page = ET.SubElement(doc, "page")

        content = [mol["main"]] + mol["texts"] + mol.get("extra_bonds", []) + mol.get("graphics", [])
        center_elements(content, mol["bounding_box"])

        for el in content:
            page.append(el)

        # === Extract label from first <t> node, joined <s> texts ===
        if mol["texts"]:
            first_t = mol["texts"][0]
            label = "".join(s.text or "" for s in first_t.findall("s"))
            fname = sanitize_filename(label)
        else:
            fname = f"group_{i}"

        path = os.path.join(output_dir, f"{fname}.cdxml")
        ET.ElementTree(doc).write(path, encoding="utf-8", xml_declaration=True)
        paths.append(path)

    print(f"[OK] Saved {len(paths)} named molecules to: {output_dir}")
    return paths

# # === MAIN ===
# if __name__ == "__main__":
#     data_dir = "./main_cdxml"
#     os.makedirs(data_dir, exist_ok=True)

#     # Process all .cdxml files in the directory once
#     for filename in os.listdir(data_dir):
#         if filename.lower().endswith(".cdxml"):
#             file_path = os.path.join(data_dir, filename)
#             print(f"Processing: {file_path}")
#             output_files = split_cdxml(file_path)
#             print("🧪 Molecules Extracted from CDXML:", len(output_files))
