"""A jointly filed 2025 Form 1040 + the taxpayer's W-2, one PDF, on the OFFICIAL IRS
forms, for an INVENTED Taiwanese-American family. Every name, number and address is
fictional.

    curl -o f1040.pdf https://www.irs.gov/pub/irs-pdf/f1040.pdf
    curl -o fw2.pdf   https://www.irs.gov/pub/irs-pdf/fw2.pdf
    python demo/joint_tw/make_joint_tw.py f1040.pdf fw2.pdf demo/joint_tw/Chen_Lin_2025_1040_W2.pdf
"""
import sys

import pymupdf

F1040 = {
    "f1_14": "Wei-Ting", "f1_15": "Chen", "f1_16": "547283916",
    "f1_17": "Yu-Hsuan", "f1_18": "Lin", "f1_19": "562108437",
    "f1_20": "2318 Rosewood Ter", "f1_22": "Cupertino", "f1_23": "CA", "f1_24": "95014",
    "f1_31": "Pin-Yu", "f1_35": "Chen", "f1_39": "618305742", "f1_43": "Daughter",
    "f1_32": "Chia-Hao", "f1_36": "Chen", "f1_40": "627419058", "f1_44": "Son",
    "f1_47": "485,300", "f1_57": "485,300", "f1_59": "12,418", "f1_60": "36,920",
    "f1_61": "44,105", "f1_70": "92,750", "f1_73": "634,573", "f1_74": "0", "f1_75": "634,573",
    "f2_01": "634,573", "f2_02": "31,500", "f2_05": "31,500", "f2_06": "603,073",
    "f2_17": "121,640", "f2_20": "121,640", "f2_21": "40,000",
    "f2_32": "121042882", "f2_33": "7730158294",
    "f2_40": "Semiconductor Engineer", "f2_42": "Pharmacist",
    "f2_44": "(408) 555-0173", "f2_45": "weiting.chen@example.com",
    "f2_46": "Shu-Fen Hsu", "f2_47": "P02468135",
    "f2_48": "Formosa Tax Partners LLC", "f2_49": "(408) 555-0146",
    "f2_50": "1880 S Bascom Ave Ste 210, Campbell, CA 95008", "f2_51": "77-4051836",
}
W2 = {
    "f1_01": "547-28-3916", "f1_02": "94-2716635",
    "f1_03": "Pacific Rim Semiconductor Inc\n88 Innovation Way\nSan Jose, CA 95134",
    "f1_04": "PRS-20419", "f1_05": "Wei-Ting", "f1_06": "Chen",
    "f1_08": "2318 Rosewood Ter\nCupertino, CA 95014",
    "f1_09": "485300.00", "f1_10": "121640.00", "f1_11": "176100.00", "f1_12": "10918.20",
    "f1_13": "485300.00", "f1_14": "9603.85", "f1_20": "D", "f1_21": "23500.00",
    "f1_31": "CA", "f1_32": "348-1176-2", "f1_35": "485300.00", "f1_37": "46120.70",
}


def fill(doc, values, top_only=False):
    for page in doc:
        for w in page.widgets():
            key = w.field_name.split(".")[-1].split("[")[0]
            if top_only and w.rect.y0 >= 400:
                continue
            if w.field_type_string == "Text" and key in values:
                w.field_value = values[key]
                w.update()
            elif (w.field_type_string == "CheckBox" and "Checkbox_ReadOrder" in w.field_name
                  and w.field_name.split(".")[-1] == "c1_8[1]"):
                w.field_value = w.on_state()  # married filing jointly
                w.update()


f1040 = pymupdf.open(sys.argv[1])
fill(f1040, F1040)
f1040.bake()
w2 = pymupdf.open(sys.argv[2])
w2.select([next(i for i, p in enumerate(w2) if list(p.widgets()))])
fill(w2, W2, top_only=True)
w2.bake()
f1040.insert_pdf(w2)
f1040.set_metadata({"title": "Chen & Lin 2025 return", "author": "Formosa Tax Partners LLC"})
f1040.save(sys.argv[3], garbage=3, deflate=True)
