"""Fill the official IRS Form W-2 (first copy page) with an INVENTED employee.

    curl -o fw2.pdf https://www.irs.gov/pub/irs-pdf/fw2.pdf
    python demo/w2/make_w2.py fw2.pdf demo/w2/Feldman_Marcus_W2.pdf
"""
import sys
import pymupdf

TOP = {
    "f1_01": "512-48-7391", "f1_02": "84-3127755",
    "f1_03": "Northwind Analytics Inc\n1450 Charleston Rd\nMountain View, CA 94043",
    "f1_04": "A7731-0042", "f1_05": "Marcus T", "f1_06": "Feldman",
    "f1_08": "4817 Larkspur Ridge Dr\nFremont, CA 94539",
    "f1_09": "612450.00", "f1_10": "148920.00", "f1_11": "176100.00", "f1_12": "10918.20",
    "f1_13": "612450.00", "f1_14": "12592.53", "f1_20": "D", "f1_21": "23500.00",
    "f1_31": "CA", "f1_32": "113-4482-9", "f1_35": "612450.00", "f1_37": "58210.44",
}
src = pymupdf.open(sys.argv[1])
pi = next(i for i, p in enumerate(src) if list(p.widgets()))
src.select([pi])
page = src[0]
for w in page.widgets():
    key = w.field_name.split(".")[-1].split("[")[0]
    top_copy = w.rect.y0 < 400
    if w.field_type_string == "Text" and key in TOP and top_copy:
        w.field_value = TOP[key]
        w.update()
    elif w.field_type_string == "Text" and top_copy is False:
        # second copy on the page: same values (the bottom copy repeats the top)
        twin = {"f1_45": "f1_01", "f1_46": "f1_02", "f1_47": "f1_03", "f1_48": "f1_04",
                "f1_49": "f1_05", "f1_50": "f1_06", "f1_52": "f1_08", "f1_53": "f1_09",
                "f1_54": "f1_10"}.get(key)
        if twin:
            w.field_value = TOP[twin]
            w.update()
src.bake()
src.save(sys.argv[2], garbage=3, deflate=True)
