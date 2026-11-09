"""Fill the official IRS 2025 Form 1040 with an INVENTED return and flatten it.

Every name, SSN, address, account and amount below is fictional.
    curl -o f1040.pdf https://www.irs.gov/pub/irs-pdf/f1040.pdf
    python demo/f1040/make_1040.py f1040.pdf demo/f1040/Feldman_Marcus_2025_Form1040.pdf
"""
import sys
import pymupdf

VALUES = {
    # page 1 - identity
    "f1_14": "Marcus T", "f1_15": "Feldman", "f1_16": "512487391",
    "f1_17": "Elena R", "f1_18": "Feldman", "f1_19": "528610943",
    "f1_20": "4817 Larkspur Ridge Dr", "f1_22": "Fremont", "f1_23": "CA", "f1_24": "94539",
    # dependents
    "f1_31": "Sofia", "f1_35": "Feldman", "f1_39": "604183372", "f1_43": "Daughter",
    "f1_32": "Lucas", "f1_36": "Feldman", "f1_40": "611250498", "f1_44": "Son",
    # income
    "f1_47": "612,450", "f1_57": "612,450", "f1_59": "18,322", "f1_60": "41,905",
    "f1_61": "52,110", "f1_70": "128,600", "f1_73": "811,482", "f1_74": "0", "f1_75": "811,482",
    # page 2
    "f2_01": "811,482", "f2_02": "31,500", "f2_05": "31,500", "f2_06": "779,982",
    "f2_17": "148,920", "f2_20": "148,920", "f2_21": "60,000",
    "f2_32": "121000358", "f2_33": "4471982036",
    "f2_40": "Software Architect", "f2_42": "Physician",
    "f2_44": "(510) 555-0142", "f2_45": "mfeldman.home@example.com",
    "f2_46": "Dana K. Whitfield", "f2_47": "P01234567",
    "f2_48": "Whitfield Tax Advisors LLC", "f2_49": "(408) 555-0187",
    "f2_50": "2200 Mission College Blvd, Santa Clara, CA 95054", "f2_51": "84-3127755",
}
CHECK = {"c1_8[1]"}  # filing status: married filing jointly (second box of the first column)

doc = pymupdf.open(sys.argv[1])
for page in doc:
    for w in page.widgets():
        short = w.field_name.split(".")[-1]
        key = short.split("[")[0]
        if w.field_type_string == "Text" and key in VALUES:
            w.field_value = VALUES[key]
            w.update()
        elif w.field_type_string == "CheckBox" and short in CHECK and "Checkbox_ReadOrder" in w.field_name:
            w.field_value = w.on_state()
            w.update()
doc.bake()  # widgets -> ordinary page content, as a printed/e-filed return looks
doc.save(sys.argv[2], garbage=3, deflate=True)
